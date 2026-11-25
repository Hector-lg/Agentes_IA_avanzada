"""Carga del CSV del corte y normalización N-01."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from basecards.soporte.config import cargar_mapeo, cargar_reglas

COLUMNAS_CATALOGO = [
    "STRATEGY",
    "STRATEGY_SEGMENT",
    "PRODUCT",
    "CHANNEL",
    "AGE_TAG",
    "SCORE_TAG",
]


def _renombrar_dummi(df: pd.DataFrame, mapeo: dict[str, Any]) -> pd.DataFrame:
    traducir = mapeo.get("dummi_a_variable", {})
    presentes = {c: traducir[c] for c in df.columns if c in traducir}
    if presentes:
        df = df.rename(columns=presentes)
    return df


def normalizar_catalogos(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """N-01: recorta espacios finales de columnas de catálogo.

    Returns
    -------
    df_normalizado, filas_tocadas
    """
    out = df.copy()
    mask_total = pd.Series(False, index=out.index)
    for col in COLUMNAS_CATALOGO:
        if col not in out.columns:
            continue
        serie = out[col]
        if not pd.api.types.is_string_dtype(serie) and serie.dtype != object:
            serie = serie.astype("string")
        stripped = serie.str.strip()
        cambió = serie.notna() & stripped.notna() & (serie != stripped)
        mask_total |= cambió.fillna(False)
        out[col] = stripped
    return out, int(mask_total.sum())


def cargar_csv(ruta: str | Path) -> pd.DataFrame:
    """Lee el CSV del corte (UTF-8, coma, punto decimal) y aplica mapeo dummi."""
    mapeo = cargar_mapeo()
    csv_cfg = mapeo.get("csv", {})
    df = pd.read_csv(
        ruta,
        sep=csv_cfg.get("separador", ","),
        encoding=csv_cfg.get("codificacion", "utf-8"),
        dtype="string",
        keep_default_na=True,
    )
    return _renombrar_dummi(df, mapeo)


def castear_tipos(df: pd.DataFrame, reglas: dict[str, Any] | None = None) -> pd.DataFrame:
    """Convierte columnas a tipos del diccionario (Int64 / float64 / string)."""
    cfg = reglas or cargar_reglas()
    out = df.copy()
    for col in cfg["tipos"]["enteros"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").astype("Int64")
    for col in cfg["tipos"]["reales"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").astype("float64")
    for col in cfg["tipos"]["texto"]:
        if col in out.columns:
            out[col] = out[col].astype("string")
    return out


def perf_month(vintage: int, mob: int) -> int:
    """PERF_MONTH = VINTAGE + MOB meses, en formato AAAAMM."""
    anio, mes = divmod(int(vintage), 100)
    mes += int(mob)
    anio += (mes - 1) // 12
    mes = (mes - 1) % 12 + 1
    return anio * 100 + mes


def mob_max(vintage: int, fecha_corte: int) -> int:
    """Meses entre VINTAGE y la fecha de corte (R-12)."""
    ay, am = divmod(int(fecha_corte), 100)
    vy, vm = divmod(int(vintage), 100)
    return (ay - vy) * 12 + (am - vm)


def corte_anterior_derivado(df: pd.DataFrame, fecha_corte: int) -> pd.DataFrame:
    """Mismo CSV sin filas cuyo PERF_MONTH == fecha_corte (Manual §2.6.5.7)."""
    v = df["VINTAGE"].astype(int)
    m = df["MOB"].astype(int)
    pm = [perf_month(vi, mi) for vi, mi in zip(v, m, strict=True)]
    mask = pd.Series(pm, index=df.index) != int(fecha_corte)
    return df.loc[mask].copy()
