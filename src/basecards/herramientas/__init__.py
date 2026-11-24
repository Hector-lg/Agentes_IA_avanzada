"""Herramientas compartidas por los agentes de Base Cards.

La carga es perezosa para que ``python -m basecards.herramientas.skills`` no
importe el módulo dos veces.
"""

from __future__ import annotations

from typing import Any

_EXPORTADOS = {
    "Hallazgo",
    "MetadataSkill",
    "ResultadoValidacion",
    "ResumenSkill",
    "SkillError",
    "raiz_skills",
    "skill_hash_contenido",
    "skill_validar",
    "skills_leer",
    "skills_listar",
}


def __getattr__(nombre: str) -> Any:
    if nombre not in _EXPORTADOS:
        raise AttributeError(f"module {__name__!r} has no attribute {nombre!r}")
    from basecards.herramientas import skills

    return getattr(skills, nombre)


def __dir__() -> list[str]:
    return sorted([*globals(), *_EXPORTADOS])
