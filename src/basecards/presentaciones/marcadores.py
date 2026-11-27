"""Parseo, renderizado y guardrails para marcadores de presentaciones.

La narrativa del LLM usa marcadores ``{{m.*}}``. Este módulo no calcula
métricas: únicamente valida su forma, renderiza valores ya calculados y
rechaza cifras de negocio escritas literalmente.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FORMATOS_VALOR = frozenset({"pct1", "pct2", "mxn", "mxn_millones", "int"})

PATRON_MARCADOR = re.compile(
    r"\{\{m\.(?P<metrica>[A-Za-z_][A-Za-z0-9_]*)"
    r"\.(?P<cohorte>\d{6}|todas)"
    r"\.MOB(?P<mob>\d+)"
    r"\|(?P<formato>pct1|pct2|mxn|mxn_millones|int)\}\}"
)
PREFIJO_MARCADOR = "{{m."
PATRON_CUALQUIER_MARCADOR = re.compile(r"\{\{.*?\}\}", re.DOTALL)
PATRON_NUMERO = re.compile(r"\d+(?:[.,]\d+)?")

# Valores que describen estructura o categorías, no cifras calculadas.
PATRONES_PERMITIDOS = (
    re.compile(r"\bMOB\s*\d+\b", re.IGNORECASE),
    re.compile(r"\b\d{4}-Q[1-4]\b", re.IGNORECASE),
    re.compile(
        r"\b(?:ene|feb|mar|abr|may|jun|jul|ago|sep|oct|nov|dic|enero|febrero|"
        r"marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)"
        r"-\d{4}\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:30|60|90)\+(?!\d)"),
    re.compile(r"\b(?:diapositiva|slide|secci[oó]n|figura|tabla)\s+\d+\b", re.IGNORECASE),
)
PATRON_LISTA = re.compile(r"(?m)^\s*(?:\d+[.)]|[-*•])\s*")


class MarcadorError(Exception):
    """Error de formato o sustitución de marcador."""

    codigo = "E-MAR-030"

    def __init__(self, mensaje: str) -> None:
        super().__init__(f"{self.codigo}: {mensaje}")
        self.mensaje = mensaje


@dataclass(frozen=True)
class Marcador:
    """Representa un marcador válido encontrado en un texto."""

    metrica: str
    cohorte: str
    mob: int
    formato: str
    texto: str


def parsear(texto: str) -> list[Marcador]:
    """Extrae y valida todos los marcadores de ``texto``.

    Un prefijo ``{{m.`` que no corresponde a la gramática cerrada es un error.
    """
    if not isinstance(texto, str):
        raise MarcadorError("el texto debe ser una cadena")
    encontrados = list(PATRON_MARCADOR.finditer(texto))
    if texto.count(PREFIJO_MARCADOR) != len(encontrados):
        raise MarcadorError("hay un marcador inválido; usa {{m.metrica.cohorte.MOBn|formato}}")
    return [
        Marcador(
            metrica=match.group("metrica"),
            cohorte=match.group("cohorte"),
            mob=int(match.group("mob")),
            formato=match.group("formato"),
            texto=match.group(0),
        )
        for match in encontrados
    ]


def render(valor: float, formato: str) -> str:
    """Renderiza un valor calculado según el formato del marcador."""
    if formato not in FORMATOS_VALOR:
        raise MarcadorError(f"formato desconocido {formato!r}")
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


def render_marcadores(texto: str, metricas: dict[str, float | int]) -> str:
    """Sustituye marcadores por valores calculados y formateados."""
    parsear(texto)

    def reemplazar(match: re.Match[str]) -> str:
        clave = match.group(0)
        if clave not in metricas:
            raise MarcadorError(f"el marcador {clave} no tiene valor calculado")
        return render(metricas[clave], match.group("formato"))

    return PATRON_MARCADOR.sub(reemplazar, texto)


def _enmascarar_marcadores(texto: str) -> str:
    return PATRON_CUALQUIER_MARCADOR.sub(" ", texto)


def _quitar_permitidos(texto: str) -> str:
    restante = texto
    for patron in PATRONES_PERMITIDOS:
        restante = patron.sub(" ", restante)
    # La numeración al comienzo de una viñeta es estructural.
    return PATRON_LISTA.sub(" ", restante)


def contiene_cifras_literales(texto: str) -> bool:
    """Indica si ``texto`` contiene cifras de negocio fuera de marcadores.

    Se ignoran los números dentro de cualquier marcador, además de MOB,
    periodos, meses, categorías fijas de mora y referencias estructurales.
    """
    if not isinstance(texto, str):
        return True
    sin_marcadores = _enmascarar_marcadores(texto)
    restante = _quitar_permitidos(sin_marcadores)
    return PATRON_NUMERO.search(restante) is not None


# Alias compatibles con la capa de herramientas existente.
formatear_valor = render
sustituir_marcadores = render_marcadores

__all__ = [
    "FORMATOS_VALOR",
    "PATRON_MARCADOR",
    "Marcador",
    "MarcadorError",
    "contiene_cifras_literales",
    "formatear_valor",
    "parsear",
    "render",
    "render_marcadores",
    "sustituir_marcadores",
]
