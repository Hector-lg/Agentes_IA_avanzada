"""Configuración del núcleo de analítica (ingesta E1 y MPDC E2)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = ROOT / "config"

LLAVE_COHORTE = [
    "VINTAGE",
    "STRATEGY",
    "STRATEGY_SEGMENT",
    "PRODUCT",
    "CHANNEL",
    "AGE_TAG",
    "SCORE_TAG",
]
LLAVE_FILA = [*LLAVE_COHORTE, "MOB"]


@lru_cache(maxsize=1)
def cargar_config() -> dict[str, Any]:
    with (CONFIG_DIR / "modelo.yaml").open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise TypeError("config/modelo.yaml inválido: se esperaba un mapping")
    return data


@lru_cache(maxsize=1)
def cargar_indicadores() -> dict[str, Any]:
    with (CONFIG_DIR / "indicadores.yaml").open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolver(ruta: str | Path) -> Path:
    """Las rutas relativas de la configuración se resuelven contra la raíz del repositorio."""
    p = Path(ruta)
    return p if p.is_absolute() else ROOT / p
