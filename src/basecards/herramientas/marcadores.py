"""Sustitución de marcadores ``{{m.*}}`` compartida por los ensambladores.

El LLM redacta con marcadores, nunca con cifras literales (RB-04). Este módulo
es el único lugar donde se define la gramática y el formato de esos valores;
``pptx_ensamblar``, ``docx_ensamblar`` y ``xlsx_ensamblar`` lo reutilizan y
traducen ``MarcadorError`` a su propio código ``E-*``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

FORMATOS_VALOR = frozenset({"pct1", "pct2", "mxn", "mxn_millones", "int"})

PATRON_MARCADOR = re.compile(
    r"\{\{m\.(?P<metrica>[A-Za-z_][A-Za-z0-9_]*)"
    r"\.(?P<cohorte>\d{6}|todas)"
    r"\.MOB(?P<mob>\d+)"
    r"\|(?P<formato>pct1|pct2|mxn|mxn_millones|int)\}\}"
)


class MarcadorError(Exception):
    """Error de marcador con el código estable ``E-MAR-030``."""

    def __init__(self, mensaje: str) -> None:
        super().__init__(f"E-MAR-030: {mensaje}")
        self.codigo = "E-MAR-030"
        self.mensaje = mensaje


def formatear_valor(valor: float, formato: str) -> str:
    """Formatea un valor ya calculado según el formato del marcador."""
    if formato not in FORMATOS_VALOR:
        raise MarcadorError(
            f"formato desconocido {formato!r}; usa {sorted(FORMATOS_VALOR)}",
        )
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        raise MarcadorError(f"el valor {valor!r} no es numérico")
    numero = float(valor)
    if formato == "pct1":
        return f"{numero * 100:,.1f}%"
    if formato == "pct2":
        return f"{numero * 100:,.2f}%"
    if formato == "mxn":
        return f"${numero:,.0f}"
    if formato == "mxn_millones":
        return f"${numero / 1_000_000:,.1f} M"
    return f"{round(numero):,}"


def sustituir_marcadores(texto: str, metricas: Mapping[str, float | int]) -> str:
    """Reemplaza todos los marcadores válidos de ``texto`` por su valor.

    Un marcador sin valor calculado es un error: nunca se deja el texto crudo
    en el documento final.
    """

    def reemplazo(coincidencia: re.Match[str]) -> str:
        clave = coincidencia.group(0)
        if clave not in metricas:
            raise MarcadorError(f"el marcador {clave} no tiene valor calculado")
        return formatear_valor(metricas[clave], coincidencia.group("formato"))

    return PATRON_MARCADOR.sub(reemplazo, texto)
