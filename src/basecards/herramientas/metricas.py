"""Traducción del DSL cerrado de ``metrics.yaml`` a SQL de DuckDB.

Ninguna fórmula se ejecuta en Python: se tokeniza, se analiza con una gramática
recursiva y se emite SQL. Solo se admiten columnas del ``schema.json`` de la
skill, ``sum``, ``count``, números, paréntesis y los operadores ``+ - * /``.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import yaml
from pydantic import ValidationError

from basecards.herramientas.skills import Agregado, Metrica, MetricasDoc

FUNCIONES_DSL = frozenset({"sum", "count"})
FORMATOS_VALOR = frozenset({"pct1", "pct2", "mxn", "mxn_millones", "int"})

_COHORTE_RE = re.compile(r"^(?:\d{6}|todas)$")
_TABLA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SUFIJOS_TABLA = frozenset({".parquet", ".csv"})
_SUFIJOS_DUCKDB = frozenset({".duckdb", ".db"})

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


# ---------------------------------------------------------------------------
# Carga de la definición y de la fuente de datos
# ---------------------------------------------------------------------------


def _cargar_metricas(ruta: Path) -> MetricasDoc:
    archivo = ruta / "metrics.yaml"
    if not archivo.is_file():
        raise MetricaError("E-MET-001", f"falta {archivo}", "metrics.yaml")
    try:
        datos = yaml.safe_load(archivo.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise MetricaError(
            "E-MET-002", f"no se pudo leer metrics.yaml: {exc}", "metrics.yaml"
        ) from exc
    if not isinstance(datos, dict):
        raise MetricaError("E-MET-002", "metrics.yaml debe ser un mapa YAML", "metrics.yaml")
    try:
        return MetricasDoc.model_validate(datos)
    except ValidationError as exc:
        error = exc.errors()[0]
        ubicacion = ".".join(str(parte) for parte in error["loc"])
        raise MetricaError(
            "E-MET-002",
            f"metrics.yaml inválido en {ubicacion}: {error['msg']}",
            "metrics.yaml",
        ) from exc


def _cargar_columnas(ruta: Path) -> dict[str, Any]:
    archivo = ruta / "schema.json"
    if not archivo.is_file():
        raise MetricaError("E-MET-001", f"falta {archivo}", "schema.json")
    try:
        datos = json.loads(archivo.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MetricaError(
            "E-MET-002", f"no se pudo leer schema.json: {exc}", "schema.json"
        ) from exc
    columnas = datos.get("columnas") if isinstance(datos, dict) else None
    if not isinstance(columnas, dict) or not columnas:
        raise MetricaError("E-MET-002", "schema.json no declara columnas", "schema.json")
    return columnas


def _ruta_datos(datos: Path | str | None) -> Path:
    if datos is not None:
        ruta = Path(datos).expanduser()
    else:
        entorno = os.environ.get("BASECARDS_DATOS")
        ruta = (
            Path(entorno).expanduser()
            if entorno
            else Path(__file__).resolve().parents[3] / "data" / "base_cards.parquet"
        )
    ruta = ruta.resolve()
    if not ruta.is_file():
        raise MetricaError("E-MET-020", f"no existe la fuente de datos {ruta}", "datos")
    if ruta.suffix.lower() not in _SUFIJOS_TABLA | _SUFIJOS_DUCKDB:
        raise MetricaError(
            "E-MET-020",
            f"extensión no soportada {ruta.suffix!r}; usa .parquet, .csv, .duckdb o .db",
            "datos",
        )
    return ruta


def _tabla_duckdb(conexion: duckdb.DuckDBPyConnection, tabla: str | None) -> str:
    if tabla is not None:
        if not _TABLA_RE.fullmatch(tabla):
            raise MetricaError("E-MET-020", f"nombre de tabla inválido {tabla!r}", "datos")
        return tabla
    filas = conexion.execute("SHOW TABLES").fetchall()
    if not filas:
        raise MetricaError("E-MET-020", "la base DuckDB no tiene tablas", "datos")
    if len(filas) > 1:
        raise MetricaError(
            "E-MET-020",
            "la base DuckDB tiene varias tablas; indica el parámetro tabla",
            "datos",
        )
    nombre = filas[0][0]
    if not isinstance(nombre, str) or not _TABLA_RE.fullmatch(nombre):
        raise MetricaError("E-MET-020", f"nombre de tabla inválido {nombre!r}", "datos")
    return nombre


def _abrir_fuente(
    ruta: Path, tabla: str | None
) -> tuple[duckdb.DuckDBPyConnection, str, list[object]]:
    sufijo = ruta.suffix.lower()
    if sufijo in _SUFIJOS_DUCKDB:
        try:
            conexion = duckdb.connect(str(ruta), read_only=True)
        except duckdb.Error as exc:
            raise MetricaError("E-MET-020", f"no se pudo abrir {ruta}: {exc}", "datos") from exc
        try:
            nombre = _tabla_duckdb(conexion, tabla)
        except MetricaError:
            conexion.close()
            raise
        return conexion, f'"{nombre}"', []
    conexion = duckdb.connect()
    fuente = "read_parquet(?)" if sufijo == ".parquet" else "read_csv_auto(?)"
    return conexion, fuente, [str(ruta)]


def _validar_rango(mob_min: int, mob_max: int) -> None:
    for valor in (mob_min, mob_max):
        if isinstance(valor, bool) or not isinstance(valor, int):
            raise MetricaError("E-MET-004", "mob_min y mob_max deben ser enteros", "parámetros")
    if mob_min < 0 or mob_max < mob_min:
        raise MetricaError(
            "E-MET-004",
            f"rango MOB inválido: mob_min={mob_min} y mob_max={mob_max}",
            "parámetros",
        )


def _convertir(valor: Any, formato: str) -> float | int:
    if valor is None:
        return 0 if formato == "int" else 0.0
    if formato == "int":
        return int(valor)
    return float(valor)


def _clave(metrica: str, cohorte: str, mob: int, formato: str) -> str:
    return "{{m." + metrica + "." + cohorte + f".MOB{mob}|" + formato + "}}"


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def metricas_calcular(
    skill_ruta: Path,
    cohorte: str,
    mob_min: int,
    mob_max: int,
    *,
    datos: Path | str | None = None,
    tabla: str | None = None,
) -> dict[str, float | int]:
    """Calcula las métricas de ``metrics.yaml`` por cohorte y MOB.

    Devuelve un diccionario con claves ``{{m.<metrica>.<cohorte>.<MOBn>|<formato>}}``
    y valores ya convertidos al tipo del formato. La fuente de datos se toma de
    ``datos``, de ``BASECARDS_DATOS`` o de ``data/base_cards.parquet``.

    Cada métrica se traduce a SQL de DuckDB y se agrega con razón de sumas:
    ``SUM(numerador) / NULLIF(SUM(denominador), 0)``. Las filas se filtran por
    ``MOB``, por ``VINTAGE`` (salvo ``cohorte="todas"``) y por el filtro de
    exposición de ``metrics.yaml``. Solo aparecen las claves de los MOB con datos.
    """
    ruta = Path(skill_ruta).expanduser().resolve()
    if not ruta.is_dir():
        raise MetricaError("E-MET-001", f"no existe la skill {ruta}", "skill")
    if not _COHORTE_RE.fullmatch(cohorte):
        raise MetricaError(
            "E-MET-003",
            f"cohorte inválida {cohorte!r}; usa AAAAMM o 'todas'",
            "parámetros",
        )
    _validar_rango(mob_min, mob_max)

    documento = _cargar_metricas(ruta)
    columnas = _cargar_columnas(ruta)
    nombres = set(columnas)
    if "MOB" not in nombres:
        raise MetricaError("E-MET-002", "schema.json no declara la columna MOB", "schema.json")
    if cohorte != "todas" and "VINTAGE" not in nombres:
        raise MetricaError("E-MET-002", "schema.json no declara la columna VINTAGE", "schema.json")
    tope = columnas["MOB"].get("max") if isinstance(columnas["MOB"], dict) else None
    if isinstance(tope, (int, float)) and not isinstance(tope, bool) and mob_max > int(tope):
        raise MetricaError(
            "E-MET-004",
            f"mob_max={mob_max} supera el máximo del schema ({int(tope)})",
            "parámetros",
        )

    seleccion: list[str] = []
    for nombre, metrica in documento.metricas.items():
        if metrica.formato not in FORMATOS_VALOR:
            raise MetricaError(
                "E-MET-014",
                f"formato desconocido {metrica.formato!r} en la métrica {nombre!r}",
                "metrics.yaml",
            )
        seleccion.append(f'{compilar_metrica(metrica, nombres)} AS "m{len(seleccion)}"')

    filtros = [f'"MOB" BETWEEN {mob_min} AND {mob_max}']
    if cohorte != "todas":
        filtros.append(f'"VINTAGE" = {int(cohorte)}')
    exposicion = documento.filtro_exposicion
    if exposicion is not None:
        if exposicion.columna not in nombres:
            raise MetricaError(
                "E-MET-012",
                f"columna de exposición inexistente {exposicion.columna!r}",
                "metrics.yaml",
            )
        if not math.isfinite(exposicion.min):
            raise MetricaError(
                "E-MET-002", "el mínimo de exposición debe ser finito", "metrics.yaml"
            )
        filtros.append(f'COALESCE("{exposicion.columna}", 0) >= {exposicion.min!r}')

    fuente_ruta = _ruta_datos(datos)
    conexion, fuente, parametros = _abrir_fuente(fuente_ruta, tabla)
    sql = (
        f'SELECT "MOB", {", ".join(seleccion)} FROM {fuente} '
        f'WHERE {" AND ".join(filtros)} GROUP BY "MOB" ORDER BY "MOB"'
    )
    try:
        filas = conexion.execute(sql, parametros).fetchall()
    except duckdb.Error as exc:
        raise MetricaError(
            "E-MET-021", f"error al ejecutar la consulta: {exc}", "metrics.yaml"
        ) from exc
    finally:
        conexion.close()

    resultado: dict[str, float | int] = {}
    for fila in filas:
        mob = int(fila[0])
        for (nombre, metrica), valor in zip(documento.metricas.items(), fila[1:], strict=True):
            resultado[_clave(nombre, cohorte, mob, metrica.formato)] = _convertir(
                valor, metrica.formato
            )
    return resultado
