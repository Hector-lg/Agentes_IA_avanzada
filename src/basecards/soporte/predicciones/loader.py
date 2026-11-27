"""Loader interchangeable mock | parquet (postgres = Día 6)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from basecards.soporte.config import ROOT, cargar_predicciones_cfg
from basecards.soporte.predicciones.esquema import validar_esquema_predicciones
from basecards.soporte.predicciones.mock import leer_predicciones_mock


def cargar_predicciones(
    corte: int | None = None,
    *,
    fuente: str | None = None,
) -> pd.DataFrame:
    """Carga predicciones_cohorte según config o override explícito."""
    cfg = cargar_predicciones_cfg()
    corte_id = int(corte or cfg.get("corte_default", 202609))
    origen = (fuente or cfg.get("fuente", "mock")).lower()

    if origen == "mock":
        df = leer_predicciones_mock(corte_id)
    elif origen == "parquet":
        plantilla = str(cfg.get("ruta_parquet", "data/predicciones/{corte}/predicciones.parquet"))
        ruta = ROOT / plantilla.format(corte=corte_id)
        if not ruta.exists():
            raise FileNotFoundError(
                f"No hay predicciones parquet en {ruta}. "
                "Usa fuente='mock' o genera el artefacto del Integrante 1."
            )
        df = pd.read_parquet(ruta)
    elif origen == "postgres":
        raise NotImplementedError(
            "fuente=postgres se habilita en el Día 6 (adapter predicciones reales)."
        )
    else:
        raise ValueError(f"fuente de predicciones desconocida: {origen!r}")

    validar_esquema_predicciones(df)
    return df


def ruta_mock(corte: int | None = None) -> Path:
    cfg = cargar_predicciones_cfg()
    corte_id = int(corte or cfg.get("corte_default", 202609))
    return ROOT / str(cfg.get("ruta_mock", "data/predicciones/mock")) / f"{corte_id}.parquet"
