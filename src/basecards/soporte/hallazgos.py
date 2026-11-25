"""Modelo de hallazgos agrupados (un ticket por regla/corte)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


SEVERIDAD_ORDEN = {"info": 0, "baja": 1, "media": 2, "alta": 3, "critica": 4}

COLUMNAS_MUESTRA = [
    "VINTAGE",
    "MOB",
    "STRATEGY",
    "STRATEGY_SEGMENT",
    "PRODUCT",
    "CHANNEL",
    "AGE_TAG",
    "SCORE_TAG",
    "ACCOUNTS",
    "NCLAMTS",
    "ENRAMTS",
]


@dataclass
class Hallazgo:
    regla_id: str
    severidad: str
    descripcion: str
    filas_afectadas: int
    total_filas: int
    pct_filas: float
    monto_involucrado: float = 0.0
    muestra: list[dict[str, Any]] = field(default_factory=list)
    detalle: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def pct(filas_afectadas: int, total_filas: int) -> float:
    if total_filas <= 0:
        return 0.0
    return round(100.0 * filas_afectadas / total_filas, 4)


def muestra_filas(df, indices, n: int = 20) -> list[dict[str, Any]]:
    """Devuelve hasta n filas afectadas como dicts serializables."""
    if df is None or len(indices) == 0:
        return []
    cols = [c for c in COLUMNAS_MUESTRA if c in df.columns]
    sub = df.loc[list(indices)[:n], cols]
    return sub.astype(object).where(sub.notna(), None).to_dict(orient="records")


def es_critico(severidad: str) -> bool:
    return SEVERIDAD_ORDEN.get(severidad, 0) >= SEVERIDAD_ORDEN["critica"]
