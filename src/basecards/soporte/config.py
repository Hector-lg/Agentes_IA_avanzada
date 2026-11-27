"""Carga de configuración de reglas, catálogos e indicadores."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = ROOT / "config"


def _leer_yaml(ruta: Path) -> dict[str, Any]:
    with ruta.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise TypeError(f"YAML inválido en {ruta}: se esperaba un mapping")
    return data


@lru_cache(maxsize=1)
def cargar_reglas() -> dict[str, Any]:
    return _leer_yaml(CONFIG_DIR / "reglas" / "base_cards.yaml")


@lru_cache(maxsize=1)
def cargar_mapeo() -> dict[str, Any]:
    return _leer_yaml(CONFIG_DIR / "mapeo_columnas.yaml")


@lru_cache(maxsize=1)
def cargar_indicadores() -> dict[str, Any]:
    return _leer_yaml(CONFIG_DIR / "indicadores.yaml")


@lru_cache(maxsize=1)
def cargar_predicciones_cfg() -> dict[str, Any]:
    return _leer_yaml(CONFIG_DIR / "predicciones.yaml")


def familia_de_strategy(strategy: str, reglas: dict[str, Any] | None = None) -> str | None:
    cfg = reglas or cargar_reglas()
    for familia, meta in cfg["familias"].items():
        if strategy in meta["strategies"]:
            return familia
    return None


def catalogo_segmentos(strategy: str, reglas: dict[str, Any] | None = None) -> set[str]:
    cfg = reglas or cargar_reglas()
    familia = familia_de_strategy(strategy, cfg)
    if familia is None:
        return set()
    clave = cfg["familias"][familia]["segmentos"]
    return set(cfg["catalogos"][clave])


def catalogo_scores(strategy: str, reglas: dict[str, Any] | None = None) -> set[str]:
    cfg = reglas or cargar_reglas()
    familia = familia_de_strategy(strategy, cfg)
    if familia is None:
        return set()
    clave = cfg["familias"][familia]["scores"]
    return set(cfg["catalogos"][clave])


def strategies_permitidas(vintage: int, reglas: dict[str, Any] | None = None) -> set[str]:
    cfg = reglas or cargar_reglas()
    for banda in cfg["strategy_por_vintage"]:
        if banda["vintage_min"] <= vintage <= banda["vintage_max"]:
            return set(banda["strategies"])
    return set()
