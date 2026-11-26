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
    "DocxError": "docx_ensamblar",
    "docx_ensamblar": "docx_ensamblar",
}


class _ModuloHerramientas(types.ModuleType):
    """Resuelve el choque entre el nombre del módulo y el de la función."""

    def __getattribute__(self, nombre: str) -> Any:
        if nombre in {"pptx_ensamblar", "docx_ensamblar"}:
            from importlib import import_module

            return getattr(import_module(f"{__name__}.{nombre}"), nombre)
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
