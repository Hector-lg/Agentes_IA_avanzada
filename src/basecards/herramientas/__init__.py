"""Herramientas compartidas por los agentes de Base Cards.

La carga es perezosa para que ``python -m basecards.herramientas.skills`` no
importe el módulo dos veces.
"""

from __future__ import annotations

import sys
import types
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
    "PptxError": "pptx_ensamblar",
    "formatear_valor": "pptx_ensamblar",
    "pptx_ensamblar": "pptx_ensamblar",
    "sustituir_marcadores": "pptx_ensamblar",
}


class _ModuloHerramientas(types.ModuleType):
    """Resuelve el choque entre el módulo y la función ``pptx_ensamblar``."""

    def __getattribute__(self, nombre: str) -> Any:
        if nombre == "pptx_ensamblar":
            from basecards.herramientas.pptx_ensamblar import pptx_ensamblar

            return pptx_ensamblar
        return super().__getattribute__(nombre)


def __getattr__(nombre: str) -> Any:
    modulo = _EXPORTADOS.get(nombre)
    if modulo is None:
        raise AttributeError(f"module {__name__!r} has no attribute {nombre!r}")
    from importlib import import_module

    return getattr(import_module(f"{__name__}.{modulo}"), nombre)


def __dir__() -> list[str]:
    return sorted([*globals(), *_EXPORTADOS])


sys.modules[__name__].__class__ = _ModuloHerramientas
