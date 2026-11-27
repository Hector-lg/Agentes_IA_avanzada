"""Esquema contractual de predicciones_cohorte (PRD §6.5 / Manual §5.8)."""

from __future__ import annotations

import pandas as pd

from basecards.soporte.config import cargar_predicciones_cfg

COLUMNAS_REQUERIDAS = [
    "VINTAGE",
    "STRATEGY",
    "STRATEGY_SEGMENT",
    "PRODUCT",
    "CHANNEL",
    "AGE_TAG",
    "SCORE_TAG",
    "MOB",
    "indicador",
    "q05",
    "q50",
    "q95",
    "observado",
    "desviacion",
    "fuera_de_intervalo",
    "enraccts",
    "nclamts",
    "driver_1",
    "driver_2",
    "driver_3",
    "modelo_nombre",
    "modelo_version",
]


def validar_esquema_predicciones(df: pd.DataFrame) -> None:
    """Lanza ValueError si faltan columnas del contrato E2→E3."""
    cfg = cargar_predicciones_cfg()
    requeridas = list(cfg.get("columnas_requeridas", COLUMNAS_REQUERIDAS))
    faltantes = [c for c in requeridas if c not in df.columns]
    if faltantes:
        raise ValueError(
            "predicciones_cohorte no cumple el contrato; faltan columnas: " + ", ".join(faltantes)
        )
