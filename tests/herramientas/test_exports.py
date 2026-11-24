"""Pruebas de la API pública del paquete ``basecards.herramientas``."""

from __future__ import annotations

from basecards import herramientas
from basecards.herramientas import (
    MetricaError,
    compilar_expresion,
    compilar_metrica,
    metricas_calcular,
    skills_leer,
    skills_listar,
)


def test_exporta_la_herramienta_de_metricas() -> None:
    assert callable(metricas_calcular)
    assert callable(compilar_expresion)
    assert callable(compilar_metrica)
    assert issubclass(MetricaError, Exception)


def test_exporta_el_catalogo_de_skills() -> None:
    assert callable(skills_leer)
    assert callable(skills_listar)


def test_dir_incluye_los_simbolos_publicos() -> None:
    visibles = set(dir(herramientas))
    assert {"metricas_calcular", "MetricaError", "skills_leer"} <= visibles
