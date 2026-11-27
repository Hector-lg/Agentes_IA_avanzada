"""Utilidades compartidas del agente de presentaciones."""

from .marcadores import (
    FORMATOS_VALOR,
    Marcador,
    MarcadorError,
    contiene_cifras_literales,
    parsear,
    render,
    render_marcadores,
)

__all__ = [
    "FORMATOS_VALOR",
    "Marcador",
    "MarcadorError",
    "contiene_cifras_literales",
    "parsear",
    "render",
    "render_marcadores",
]
