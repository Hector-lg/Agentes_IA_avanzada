"""Traducción del DSL cerrado de ``metrics.yaml`` a SQL de DuckDB.

Ninguna fórmula se ejecuta en Python: se tokeniza, se analiza con una gramática
recursiva y se emite SQL. Solo se admiten columnas del ``schema.json`` de la
skill, ``sum``, ``count``, números, paréntesis y los operadores ``+ - * /``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from basecards.herramientas.skills import Agregado, Metrica

FUNCIONES_DSL = frozenset({"sum", "count"})

_TOKEN_RE = re.compile(
    r"(?P<espacio>\s+)"
    r"|(?P<numero>\d+(?:\.\d+)?)"
    r"|(?P<identificador>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<operador>[-+*/(),])"
)


class MetricaError(Exception):
    """Error del DSL de métricas con un código estable."""

    def __init__(self, codigo: str, mensaje: str, ruta: str = "") -> None:
        super().__init__(f"{codigo}: {mensaje}")
        self.codigo = codigo
        self.mensaje = mensaje
        self.ruta = ruta


@dataclass(frozen=True)
class _Token:
    tipo: str
    valor: str


@dataclass(frozen=True)
class _Numero:
    valor: float


@dataclass(frozen=True)
class _Columna:
    nombre: str
    agregacion: str


@dataclass(frozen=True)
class _Binaria:
    operador: str
    izquierda: _Nodo
    derecha: _Nodo


@dataclass(frozen=True)
class _Negativo:
    operando: _Nodo


_Nodo = _Numero | _Columna | _Binaria | _Negativo


def _tokenizar(expresion: str) -> list[_Token]:
    tokens: list[_Token] = []
    posicion = 0
    while posicion < len(expresion):
        coincidencia = _TOKEN_RE.match(expresion, posicion)
        if coincidencia is None:
            raise MetricaError(
                "E-MET-010",
                f"carácter no permitido {expresion[posicion]!r} en la posición {posicion}",
                "metrics.yaml",
            )
        posicion = coincidencia.end()
        if coincidencia.lastgroup == "espacio":
            continue
        tokens.append(_Token(coincidencia.lastgroup or "", coincidencia.group()))
    if not tokens:
        raise MetricaError("E-MET-011", "la expresión está vacía", "metrics.yaml")
    return tokens


class _Parser:
    def __init__(self, tokens: list[_Token], columnas: set[str], ruta: str) -> None:
        self._tokens = tokens
        self._columnas = columnas
        self._ruta = ruta
        self._posicion = 0

    def analizar(self) -> _Nodo:
        nodo = self._expresion()
        if self._posicion != len(self._tokens):
            token = self._tokens[self._posicion]
            raise MetricaError(
                "E-MET-011",
                f"token inesperado {token.valor!r}",
                self._ruta,
            )
        return nodo

    def _mirar(self) -> _Token | None:
        if self._posicion < len(self._tokens):
            return self._tokens[self._posicion]
        return None

    def _es_operador(self, valor: str) -> bool:
        token = self._mirar()
        return token is not None and token.tipo == "operador" and token.valor == valor

    def _consumir(self) -> _Token:
        token = self._mirar()
        if token is None:
            raise MetricaError(
                "E-MET-011",
                "se esperaba un token y la expresión terminó",
                self._ruta,
            )
        self._posicion += 1
        return token

    def _esperar(self, valor: str) -> _Token:
        token = self._mirar()
        if token is None or token.valor != valor:
            encontrado = "fin de la expresión" if token is None else repr(token.valor)
            raise MetricaError(
                "E-MET-011",
                f"se esperaba {valor!r} y se encontró {encontrado}",
                self._ruta,
            )
        self._posicion += 1
        return token

    def _expresion(self) -> _Nodo:
        nodo = self._termino()
        while self._es_operador("+") or self._es_operador("-"):
            operador = self._consumir().valor
            nodo = _Binaria(operador, nodo, self._termino())
        return nodo

    def _termino(self) -> _Nodo:
        nodo = self._factor()
        while self._es_operador("*") or self._es_operador("/"):
            operador = self._consumir().valor
            nodo = _Binaria(operador, nodo, self._factor())
        return nodo

    def _factor(self) -> _Nodo:
        token = self._mirar()
        if token is None:
            raise MetricaError(
                "E-MET-011",
                "se esperaba un número, una función o un paréntesis",
                self._ruta,
            )
        if token.tipo == "numero":
            self._posicion += 1
            return _Numero(float(token.valor))
        if token.tipo == "operador" and token.valor in {"+", "-"}:
            self._posicion += 1
            operando = self._factor()
            return operando if token.valor == "+" else _Negativo(operando)
        if token.tipo == "operador" and token.valor == "(":
            self._posicion += 1
            nodo = self._expresion()
            self._esperar(")")
            return nodo
        if token.tipo == "identificador":
            return self._funcion()
        raise MetricaError(
            "E-MET-011",
            f"token inesperado {token.valor!r}",
            self._ruta,
        )

    def _funcion(self) -> _Nodo:
        nombre = self._consumir().valor
        if nombre not in FUNCIONES_DSL:
            raise MetricaError(
                "E-MET-013",
                f"función no permitida {nombre!r}; solo se admite sum y count",
                self._ruta,
            )
        self._esperar("(")
        columna = self._mirar()
        if columna is None or columna.tipo != "identificador":
            raise MetricaError(
                "E-MET-011",
                f"{nombre} requiere un nombre de columna",
                self._ruta,
            )
        self._posicion += 1
        self._esperar(")")
        if columna.valor not in self._columnas:
            raise MetricaError(
                "E-MET-012",
                f"columna inexistente {columna.valor!r} en el schema de la skill",
                self._ruta,
            )
        return _Columna(columna.valor, nombre)


def _sql(nodo: _Nodo) -> str:
    if isinstance(nodo, _Numero):
        return repr(nodo.valor)
    if isinstance(nodo, _Columna):
        return f'{nodo.agregacion.upper()}("{nodo.nombre}")'
    if isinstance(nodo, _Negativo):
        return f"-({_sql(nodo.operando)})"
    izquierda = _sql(nodo.izquierda)
    derecha = _sql(nodo.derecha)
    if nodo.operador == "/":
        return f"({izquierda}) / NULLIF(({derecha}), 0)"
    return f"({izquierda}) {nodo.operador} ({derecha})"


def _agregado_sql(agregado: Agregado, columnas: set[str]) -> str:
    columna = agregado.sum if agregado.sum is not None else agregado.count
    if columna is None or columna not in columnas:
        raise MetricaError(
            "E-MET-012",
            f"columna inexistente {columna!r} en el schema de la skill",
            "metrics.yaml",
        )
    funcion = "SUM" if agregado.sum is not None else "COUNT"
    return f'{funcion}("{columna}")'


def compilar_expresion(expresion: str, columnas: Iterable[str]) -> str:
    """Convierte una expresión del DSL en SQL de DuckDB, sin ejecutarla."""
    permitidas = set(columnas)
    if not permitidas:
        raise MetricaError(
            "E-MET-002",
            "el schema de la skill no declara columnas",
            "schema.json",
        )
    nodo = _Parser(_tokenizar(expresion), permitidas, "metrics.yaml").analizar()
    return _sql(nodo)


def compilar_metrica(metrica: Metrica, columnas: Iterable[str]) -> str:
    """Compila una métrica completa: expresión o razón de sumas."""
    permitidas = set(columnas)
    if metrica.expresion is not None:
        return compilar_expresion(metrica.expresion, permitidas)
    if metrica.numerador is None or metrica.denominador is None:
        raise MetricaError(
            "E-MET-002",
            "la métrica no declara expresion ni numerador/denominador",
            "metrics.yaml",
        )
    numerador = _agregado_sql(metrica.numerador, permitidas)
    denominador = _agregado_sql(metrica.denominador, permitidas)
    return f"({numerador}) / NULLIF(({denominador}), 0)"
