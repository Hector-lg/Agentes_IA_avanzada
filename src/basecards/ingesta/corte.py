"""E1 — Ingesta: CSV del corte → Parquet tipado + manifiesto.

Reutiliza la carga del Agente de Soporte (mapeo `dummiN`, normalización N-01 y casteo de
tipos) para que el Parquet y la validación vean exactamente los mismos datos.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from basecards import __version__
from basecards.modelo.config import (
    LLAVE_COHORTE,
    LLAVE_FILA,
    cargar_config,
    cargar_indicadores,
    resolver,
)
from basecards.soporte.carga import cargar_csv, castear_tipos, normalizar_catalogos
from basecards.soporte.config import cargar_reglas

NOMBRE_PARQUET = "base_cards.parquet"
NOMBRE_MANIFIESTO = "manifest.json"
NOMBRE_HALLAZGOS = "hallazgos_e1.json"


class IngestaError(RuntimeError):
    """El CSV no se puede convertir a un Parquet tipado (p. ej. faltan columnas)."""


@dataclass
class ResultadoIngesta:
    corte: int
    ruta_parquet: Path
    ruta_manifiesto: Path
    manifiesto: dict[str, Any]


def sha256_archivo(ruta: Path, bloque: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with ruta.open("rb") as f:
        while chunk := f.read(bloque):
            h.update(chunk)
    return h.hexdigest()


def perf_month(vintage: pd.Series, mob: pd.Series) -> pd.Series:
    """PERF_MONTH = VINTAGE + MOB meses (AAAAMM), vectorizado."""
    v = vintage.astype("int64")
    meses = (v // 100) * 12 + (v % 100 - 1) + mob.astype("int64")
    return ((meses // 12) * 100 + meses % 12 + 1).astype("int32")


def meses_entre(desde: int, hasta: int) -> int:
    dy, dm = divmod(int(desde), 100)
    hy, hm = divmod(int(hasta), 100)
    return (hy - dy) * 12 + (hm - dm)


def sumar_meses(aaaamm: int, n: int) -> int:
    y, m = divmod(int(aaaamm), 100)
    total = y * 12 + (m - 1) + int(n)
    return (total // 12) * 100 + total % 12 + 1


def ruta_corte(corte: int, dir_cortes: str | Path | None = None) -> Path:
    base = resolver(dir_cortes or cargar_config()["ingesta"]["dir_cortes"])
    return base / str(int(corte))


def detectar_series_acumuladas(
    df: pd.DataFrame, columnas: list[str], umbral: float
) -> dict[str, Any]:
    """S-01: una serie se comporta como acumulada si casi nunca decrece con el MOB."""
    ordenado = df.sort_values(LLAVE_FILA)
    grupos = ordenado.groupby(LLAVE_COHORTE, sort=False, observed=True)
    out: dict[str, Any] = {}
    for col in columnas:
        if col not in ordenado.columns:
            continue
        dif = grupos[col].diff().dropna()
        frac = float((dif < 0).mean()) if len(dif) else 0.0
        out[col] = {"fraccion_decrece": round(frac, 6), "acumulada": bool(frac < umbral)}
    return out


def _a_tipos_parquet(df: pd.DataFrame, categoricas: list[str]) -> pd.DataFrame:
    reglas = cargar_reglas()
    out = castear_tipos(df, reglas)
    for col in categoricas:
        if col in out.columns:
            out[col] = out[col].astype("category")
    # Enteros sin nulos → int nativo (más compacto y rápido para DuckDB / LightGBM)
    for col in reglas["tipos"]["enteros"]:
        if col in out.columns and not out[col].isna().any():
            out[col] = out[col].astype("int32" if col in ("VINTAGE", "MOB") else "int64")
    return out


def _no_numericos(crudo: pd.DataFrame, tipado: pd.DataFrame) -> dict[str, int]:
    reglas = cargar_reglas()
    cols = reglas["tipos"]["enteros"] + reglas["tipos"]["reales"]
    out = {}
    for c in cols:
        if c in crudo.columns:
            n = int((crudo[c].notna() & tipado[c].isna()).sum())
            if n:
                out[c] = n
    return out


def ingerir_dataframe(
    crudo: pd.DataFrame,
    corte: int,
    *,
    dir_cortes: str | Path | None = None,
    fuente: dict[str, Any] | None = None,
    validar: bool = False,
) -> ResultadoIngesta:
    """Convierte un DataFrame crudo (todo texto, como sale del CSV) al Parquet del corte."""
    t0 = time.perf_counter()
    cfg = cargar_config()["ingesta"]
    reglas = cargar_reglas()

    faltantes = [c for c in reglas["columnas"] if c not in crudo.columns]
    if faltantes:
        raise IngestaError(f"Faltan columnas del diccionario (R-01): {faltantes}")
    extras = [c for c in crudo.columns if c not in reglas["columnas"]]

    normalizado, n01 = normalizar_catalogos(crudo)
    tipado = _a_tipos_parquet(normalizado[reglas["columnas"]], cfg["categoricas"])

    salida = ruta_corte(corte, dir_cortes)
    salida.mkdir(parents=True, exist_ok=True)
    ruta_pq = salida / NOMBRE_PARQUET
    tipado.to_parquet(
        ruta_pq, engine="pyarrow", compression=cfg.get("compresion", "zstd"), index=False
    )

    pm = perf_month(tipado["VINTAGE"].fillna(0), tipado["MOB"].fillna(0))
    acumuladas = detectar_series_acumuladas(
        tipado, cfg.get("series_riesgo", []), float(cfg.get("umbral_acumulado", 0.001))
    )
    config_acumulado = bool(cargar_indicadores().get("acumulado", False))
    se_ven_acumuladas = bool(acumuladas) and all(v["acumulada"] for v in acumuladas.values())
    avisos = []
    if se_ven_acumuladas != config_acumulado:
        avisos.append(
            "S-01: las series de riesgo se comportan como "
            f"{'acumuladas' if se_ven_acumuladas else 'del mes'}, pero config/indicadores.yaml "
            f"tiene acumulado={str(config_acumulado).lower()}. Revisar el parámetro."
        )

    manifiesto: dict[str, Any] = {
        "corte": int(corte),
        "creado": datetime.now(UTC).isoformat(timespec="seconds"),
        "version_basecards": __version__,
        "fuente": fuente or {},
        "parquet": {
            "ruta": ruta_pq.name,
            "bytes": ruta_pq.stat().st_size,
            "sha256": sha256_archivo(ruta_pq),
            "compresion": cfg.get("compresion", "zstd"),
        },
        "filas": len(tipado),
        "columnas": list(tipado.columns),
        "columnas_extra_descartadas": extras,
        "tipos": {c: str(t) for c, t in tipado.dtypes.items()},
        "n01_filas_normalizadas": int(n01),
        "valores_no_numericos": _no_numericos(normalizado, tipado),
        "cohortes": int(tipado.groupby(LLAVE_COHORTE, observed=True).ngroups),
        "vintage": [int(tipado["VINTAGE"].min()), int(tipado["VINTAGE"].max())],
        "mob": [int(tipado["MOB"].min()), int(tipado["MOB"].max())],
        "perf_month_max": int(pm.max()),
        "series_acumuladas": acumuladas,
        "config_acumulado": config_acumulado,
        "avisos": avisos,
        "validacion": None,
    }

    if validar:
        manifiesto["validacion"] = _validar_e1(crudo, corte, salida)

    manifiesto["duracion_s"] = round(time.perf_counter() - t0, 2)
    ruta_man = salida / NOMBRE_MANIFIESTO
    ruta_man.write_text(json.dumps(manifiesto, ensure_ascii=False, indent=2), encoding="utf-8")
    return ResultadoIngesta(int(corte), ruta_pq, ruta_man, manifiesto)


def _validar_e1(crudo: pd.DataFrame, corte: int, salida: Path) -> dict[str, Any]:
    """Corre la validación determinista del Agente de Soporte y guarda los hallazgos."""
    from basecards.soporte.pipeline_e1 import validar_dataframe

    t0 = time.perf_counter()
    try:
        res = validar_dataframe(crudo, fecha_corte=corte)
    except Exception as e:  # noqa: BLE001 — la ingesta no se cae si falla una regla del Agente de Soporte
        return {"dictamen": None, "error": f"{type(e).__name__}: {e}"}
    ruta = salida / NOMBRE_HALLAZGOS
    ruta.write_text(
        json.dumps(res.to_dict(), ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return {
        "dictamen": res.dictamen,
        "reglas_disparadas": sorted({h.regla_id for h in res.hallazgos}),
        "archivo_hallazgos": ruta.name,
        "duracion_s": round(time.perf_counter() - t0, 2),
    }


def _json_default(o: Any) -> Any:
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (pd.Timestamp, datetime)):
        return o.isoformat()
    if o is pd.NA or o is pd.NaT:
        return None
    return str(o)


def ingerir_csv(
    ruta_csv: str | Path,
    corte: int,
    *,
    dir_cortes: str | Path | None = None,
    validar: bool = False,
) -> ResultadoIngesta:
    """Punto de entrada E1 (ingesta): CSV del corte → `data/cortes/{corte}/base_cards.parquet`."""
    ruta_csv = Path(ruta_csv)
    if not ruta_csv.exists():
        raise FileNotFoundError(ruta_csv)
    fuente = {
        "archivo": ruta_csv.name,
        "bytes": ruta_csv.stat().st_size,
        "sha256": sha256_archivo(ruta_csv),
    }
    crudo = cargar_csv(ruta_csv)
    return ingerir_dataframe(crudo, corte, dir_cortes=dir_cortes, fuente=fuente, validar=validar)


def leer_corte(
    corte: int,
    *,
    dir_cortes: str | Path | None = None,
    columnas: list[str] | None = None,
) -> pd.DataFrame:
    ruta = ruta_corte(corte, dir_cortes) / NOMBRE_PARQUET
    if not ruta.exists():
        raise FileNotFoundError(
            f"No existe {ruta}. Corre primero: python -m basecards.ingesta --csv <archivo> --corte {corte}"
        )
    return pd.read_parquet(ruta, columns=columnas)


def leer_manifiesto(corte: int, *, dir_cortes: str | Path | None = None) -> dict[str, Any]:
    ruta = ruta_corte(corte, dir_cortes) / NOMBRE_MANIFIESTO
    return json.loads(ruta.read_text(encoding="utf-8"))


def corte_anterior_derivado(df: pd.DataFrame, corte: int) -> pd.DataFrame:
    """S-07: el mismo corte sin las filas con PERF_MONTH = corte (vectorizado)."""
    return df.loc[perf_month(df["VINTAGE"], df["MOB"]) != int(corte)].copy()
