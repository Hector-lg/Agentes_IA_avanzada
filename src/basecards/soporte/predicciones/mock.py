"""Generador y persistencia del mock de predicciones_cohorte."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from basecards.soporte.config import ROOT, cargar_predicciones_cfg
from basecards.soporte.sintetico import generar_cubo_sintetico

LLAVE = [
    "VINTAGE",
    "STRATEGY",
    "STRATEGY_SEGMENT",
    "PRODUCT",
    "CHANNEL",
    "AGE_TAG",
    "SCORE_TAG",
    "MOB",
]


def generar_predicciones_mock(
    cubo: pd.DataFrame | None = None,
    *,
    fecha_corte: int = 202609,
    forzar_fuera: bool = True,
) -> pd.DataFrame:
    """Arma predicciones alineadas al cubo; opcionalmente fuerza filas fuera de intervalo."""
    df = cubo if cubo is not None else generar_cubo_sintetico(fecha_corte=fecha_corte)
    cfg = cargar_predicciones_cfg()
    enraccts_min = int(cfg.get("enraccts_min", 200))

    work = df.copy()
    idx_fuera: list[Any] = []
    if forzar_fuera and len(work) > 0:
        mask_daria = work["STRATEGY"].astype(str).str.contains("DARIA", na=False)
        idx_fuera = list(work.index[mask_daria][:3] if mask_daria.any() else work.index[:3])
        work.loc[idx_fuera, "ENRACCTS"] = max(enraccts_min, 250)
        # Error sembrado Manual §8.2 #12: NCL ×3
        work.loc[idx_fuera, "NCLAMTS"] = (
            pd.to_numeric(work.loc[idx_fuera, "NCLAMTS"], errors="coerce").fillna(0) * 3.0
        )

    filas: list[dict[str, Any]] = []
    for idx, row in work.iterrows():
        enramts = float(row["ENRAMTS"]) or 1.0
        enraccts = float(row["ENRACCTS"]) or 1.0
        ncl = float(row["NCLAMTS"])
        o30 = float(row["OVER30ACCTS"])
        obs_ncl = ncl / enramts
        obs_o30 = o30 / enraccts
        es_fuera = idx in idx_fuera

        for indicador, obs in (("tasa_ncl", obs_ncl), ("tasa_over30", obs_o30)):
            if es_fuera and indicador == "tasa_ncl":
                # Curva esperada como si NCL no estuviera inflado → obs queda > q95
                q50 = max(obs / 3.0, 0.001)
                q05 = q50 * 0.6
                q95 = q50 * 1.4
            else:
                # Dentro de intervalo: curva centrada en el observado
                q50 = max(obs, 0.001)
                q05 = q50 * 0.7
                q95 = q50 * 1.3

            if obs > q50:
                desv = (obs - q50) / max(q95 - q50, 1e-9)
            else:
                desv = (q50 - obs) / max(q50 - q05, 1e-9)
            fuera = bool(obs > q95 or obs < q05)

            filas.append(
                {
                    **{k: row[k] for k in LLAVE},
                    "indicador": indicador,
                    "q05": round(q05, 6),
                    "q50": round(q50, 6),
                    "q95": round(q95, 6),
                    "observado": round(obs, 6),
                    "desviacion": round(float(desv), 4),
                    "fuera_de_intervalo": fuera,
                    "enraccts": int(row["ENRACCTS"]),
                    "nclamts": float(row["NCLAMTS"]),
                    "driver_1": "SCORE_ORD",
                    "driver_2": "CHANNEL",
                    "driver_3": "MOB",
                    "modelo_nombre": "curva_esperada_mock",
                    "modelo_version": 1,
                }
            )

    return pd.DataFrame(filas)


def guardar_predicciones_mock(
    df: pd.DataFrame | None = None,
    *,
    corte: int | None = None,
) -> Path:
    """Persiste el mock en data/predicciones/mock/{corte}.parquet."""
    cfg = cargar_predicciones_cfg()
    corte_id = int(corte or cfg.get("corte_default", 202609))
    pred = df if df is not None else generar_predicciones_mock(fecha_corte=corte_id)
    dest_dir = ROOT / str(cfg.get("ruta_mock", "data/predicciones/mock"))
    dest_dir.mkdir(parents=True, exist_ok=True)
    ruta = dest_dir / f"{corte_id}.parquet"
    pred.to_parquet(ruta, index=False)
    pred.to_csv(dest_dir / f"{corte_id}.csv", index=False)
    return ruta


def leer_predicciones_mock(corte: int | None = None) -> pd.DataFrame:
    cfg = cargar_predicciones_cfg()
    corte_id = int(corte or cfg.get("corte_default", 202609))
    ruta = ROOT / str(cfg.get("ruta_mock", "data/predicciones/mock")) / f"{corte_id}.parquet"
    if not ruta.exists():
        guardar_predicciones_mock(corte=corte_id)
    return pd.read_parquet(ruta)
