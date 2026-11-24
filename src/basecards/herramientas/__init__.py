"""Herramientas compartidas por los agentes de Base Cards.

La carga es perezosa para que ``python -m basecards.herramientas.skills`` no
importe el módulo dos veces.
"""

from __future__ import annotations

from typing import Any

_EXPORTADOS: dict[str, str] = {
    "Hallazgo": "skills",
    "MetadataSkill": "skills",
    "ResultadoValidacion": "skills",
    "ResumenSkill": "skills",
    "SkillError": "skills",
    "raiz_skills": "skills",
    "skill_hash_contenido": "skills",
    "skill_validar": "skills",
    "skills_leer": "skills",
    "skills_listar": "skills",
    "MetricaError": "metricas",
    "compilar_expresion": "metricas",
    "compilar_metrica": "metricas",
    "metricas_calcular": "metricas",
}


def __getattr__(nombre: str) -> Any:
    modulo = _EXPORTADOS.get(nombre)
    if modulo is None:
        raise AttributeError(f"module {__name__!r} has no attribute {nombre!r}")
    from importlib import import_module

    return getattr(import_module(f"{__name__}.{modulo}"), nombre)


def __dir__() -> list[str]:
    return sorted([*globals(), *_EXPORTADOS])
