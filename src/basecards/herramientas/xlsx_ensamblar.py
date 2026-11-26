"""Ensamblado determinista de anexos de datos en ``.xlsx``.

El libro se crea con ``xlsxwriter`` (formatos, fórmulas y valores cacheados) y
se verifica con ``openpyxl``. Toda agregación del resumen se escribe como razón
de sumas (Σ numerador / Σ denominador), nunca como promedio de razones (RB-03).

El módulo no calcula métricas de negocio ni redacta: recibe los datos ya
curados, valida tipos y columnas, escribe las fórmulas y comprueba que el valor
cacheado coincida con el cálculo de Python.
"""

from __future__ import annotations

import contextlib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from numbers import Integral, Real
from pathlib import Path
from typing import Any

import xlsxwriter
import yaml
from openpyxl import load_workbook
from xlsxwriter.utility import xl_col_to_name

from basecards.herramientas.marcadores import PATRON_MARCADOR, MarcadorError
from basecards.herramientas.marcadores import sustituir_marcadores as _sustituir_marcadores

ROOT = Path(__file__).resolve().parents[3]
BRANDING_RUTA = ROOT / "config" / "branding" / "banamex.yaml"

HOJAS = ("Datos", "Resumen")
BORDE = "#D0D5DD"
GRIS = "#667085"

ALIAS_FORMATOS = {
    "general": "General",
    "pct": "0.00%",
    "pct2": "0.00%",
    "pct1": "0.0%",
    "mxn": "$#,##0",
    "int": "#,##0",
    "num": "#,##0.00",
    "texto": "@",
    "fecha": "yyyy-mm-dd",
}

COLUMNAS_IDENTIFICADOR = {"VINTAGE", "MOB", "ID", "CLAVE", "LLAVE"}
PISTAS_MONETARIAS = (
    "saldo",
    "monto",
    "importe",
    "mxn",
    "revenue",
    "expense",
    "margen",
    "ingreso",
    "egreso",
    "costo",
    "balance",
    "nclamt",
    "enramt",
)


class XlsxError(Exception):
    """Error de ensamblado con un código estable ``E-XLS-*``."""

    def __init__(self, codigo: str, mensaje: str, ruta: str = "") -> None:
        super().__init__(f"{codigo}: {mensaje}")
        self.codigo = codigo
        self.mensaje = mensaje
        self.ruta = ruta


@dataclass(frozen=True)
class _Resumen:
    nombre: str
    numerador: str
    denominador: str | None
    formato: str
    descripcion: str
    formula: str
    valor: float


# ---------------------------------------------------------------------------
# Branding y utilidades
# ---------------------------------------------------------------------------


def _cargar_branding() -> dict[str, Any]:
    if not BRANDING_RUTA.is_file():
        raise XlsxError("E-XLS-002", f"falta el branding {BRANDING_RUTA}", "branding")
    try:
        datos = yaml.safe_load(BRANDING_RUTA.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise XlsxError("E-XLS-002", f"no se pudo leer el branding: {exc}", "branding") from exc
    if not isinstance(datos, dict):
        raise XlsxError("E-XLS-002", "el branding debe ser un mapa YAML", "branding")
    return datos


def _color_hex(branding: Mapping[str, Any], nombre: str) -> str:
    paleta = branding.get("paleta", {})
    valor = paleta.get(nombre)
    if not isinstance(valor, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", valor):
        raise XlsxError("E-XLS-002", f"color inválido {nombre!r} en el branding", "branding")
    return valor.lstrip("#").upper()


def _familia(branding: Mapping[str, Any]) -> str:
    return str(branding.get("tipografia", {}).get("familia", "Arial"))


def _sustituir(texto: str, metricas: Mapping[str, float | int], campo: str) -> str:
    try:
        return _sustituir_marcadores(texto, metricas)
    except MarcadorError as exc:
        raise XlsxError("E-XLS-030", f"{campo}: {exc.mensaje}", campo) from exc


def _texto_obligatorio(valor: Any, campo: str) -> str:
    if not isinstance(valor, str) or not valor.strip():
        raise XlsxError("E-XLS-020", f"{campo} debe ser texto no vacío")
    return valor.strip()


def _texto_opcional(valor: Any, campo: str) -> str | None:
    if valor is None:
        return None
    if not isinstance(valor, str):
        raise XlsxError("E-XLS-020", f"{campo} debe ser texto")
    texto = valor.strip()
    return texto or None


def _formato_excel(valor: Any, defecto: str) -> str:
    if valor is None:
        return defecto
    if not isinstance(valor, str) or not valor.strip():
        raise XlsxError("E-XLS-020", "el formato de celda debe ser texto no vacío")
    texto = valor.strip()
    alias = texto.lower()
    if alias in ALIAS_FORMATOS:
        return ALIAS_FORMATOS[alias]
    if any(caracter in texto for caracter in "%$#0"):
        return texto
    raise XlsxError(
        "E-XLS-020",
        f"formato desconocido {valor!r}; usa {sorted(ALIAS_FORMATOS)} o un formato de Excel",
    )


def _es_numero(valor: Any) -> bool:
    return isinstance(valor, (Integral, Real, Decimal)) and not isinstance(valor, bool)


def _pista_monetaria(columna: str) -> bool:
    nombre = columna.lower()
    return any(pista in nombre for pista in PISTAS_MONETARIAS)


def _inferir_corte(metricas: Mapping[str, float | int]) -> str | None:
    for clave in metricas:
        coincidencia = PATRON_MARCADOR.fullmatch(str(clave))
        if coincidencia is not None and coincidencia.group("cohorte") != "todas":
            return coincidencia.group("cohorte")
    return None


# ---------------------------------------------------------------------------
# Normalización de datos y resumen
# ---------------------------------------------------------------------------


def _normalizar_datos(
    datos: Any,
    formatos: Mapping[str, Any] | None,
) -> tuple[list[str], list[list[Any]], dict[str, str]]:
    formatos_datos: dict[str, Any] = {}
    if isinstance(datos, Mapping):
        columnas_brutas = datos.get("columnas")
        filas_brutas = datos.get("filas")
        if datos.get("formatos") is not None:
            if not isinstance(datos["formatos"], Mapping):
                raise XlsxError("E-XLS-020", "datos.formatos debe ser un mapa")
            formatos_datos.update(datos["formatos"])
    elif isinstance(datos, Sequence) and not isinstance(datos, (str, bytes)):
        filas_brutas = list(datos)
        if not filas_brutas:
            raise XlsxError("E-XLS-010", "datos no puede estar vacío")
        if not all(isinstance(fila, Mapping) for fila in filas_brutas):
            raise XlsxError("E-XLS-010", "datos debe ser una lista de mapas")
        columnas_brutas = list(filas_brutas[0])
    else:
        raise XlsxError("E-XLS-010", "datos debe ser un mapa o una lista de mapas")

    if columnas_brutas is None and isinstance(filas_brutas, Sequence) and filas_brutas:
        primera = filas_brutas[0]
        if isinstance(primera, Mapping):
            columnas_brutas = list(primera)
    if (
        not isinstance(columnas_brutas, Sequence)
        or isinstance(columnas_brutas, (str, bytes))
        or not columnas_brutas
        or not all(isinstance(columna, str) and columna.strip() for columna in columnas_brutas)
    ):
        raise XlsxError("E-XLS-010", "datos.columnas debe ser una lista de nombres")
    columnas = [columna.strip() for columna in columnas_brutas]
    if len(set(columnas)) != len(columnas):
        raise XlsxError("E-XLS-010", "datos.columnas no puede repetir nombres")

    if not isinstance(filas_brutas, Sequence) or isinstance(filas_brutas, (str, bytes)):
        raise XlsxError("E-XLS-010", "datos.filas debe ser una lista")
    if not filas_brutas:
        raise XlsxError("E-XLS-010", "datos.filas necesita al menos una fila")

    filas: list[list[Any]] = []
    for indice, fila in enumerate(filas_brutas):
        if isinstance(fila, Mapping):
            extra = set(fila) - set(columnas)
            if extra:
                raise XlsxError(
                    "E-XLS-010",
                    f"datos.filas[{indice}] tiene columnas no declaradas: {sorted(extra)}",
                )
            valores = [fila.get(columna) for columna in columnas]
        elif isinstance(fila, Sequence) and not isinstance(fila, (str, bytes)):
            valores = list(fila)
            if len(valores) != len(columnas):
                raise XlsxError(
                    "E-XLS-010",
                    f"datos.filas[{indice}] tiene {len(valores)} valores y hay {len(columnas)} columnas",
                )
        else:
            raise XlsxError("E-XLS-010", f"datos.filas[{indice}] debe ser una lista o un mapa")
        filas.append(valores)

    if formatos is not None:
        if not isinstance(formatos, Mapping):
            raise XlsxError("E-XLS-020", "formatos debe ser un mapa de columna a formato")
        formatos_datos.update(formatos)

    formatos_finales: dict[str, str] = {}
    for columna, valor in formatos_datos.items():
        if columna not in columnas:
            raise XlsxError("E-XLS-020", f"la columna {columna!r} de formatos no existe en datos")
        if valor is None:
            continue
        formatos_finales[columna] = _formato_excel(valor, "General")
    return columnas, filas, formatos_finales


def _valores_columna(filas: Sequence[Sequence[Any]], indice: int) -> list[Any]:
    return [fila[indice] for fila in filas if fila[indice] is not None]


def _validar_numerica(columna: str, valores: Sequence[Any]) -> None:
    for valor in valores:
        if not _es_numero(valor):
            raise XlsxError(
                "E-XLS-040",
                f"la columna {columna!r} debe ser numérica para el resumen",
            )


def _suma(valores: Sequence[Any]) -> float:
    return float(sum(Decimal(str(valor)) for valor in valores))


def _rango(indice: int, n_filas: int) -> str:
    letra = xl_col_to_name(indice)
    return f"Datos!{letra}2:{letra}{n_filas}"


def _formato_suma_defecto(columna: str, valores: Sequence[Any]) -> str:
    if _pista_monetaria(columna):
        return "$#,##0"
    if valores and all(
        isinstance(valor, Integral) and not isinstance(valor, bool) for valor in valores
    ):
        return "#,##0"
    return "#,##0.00"


def _resumen_automatico(
    columnas: Sequence[str],
    filas: Sequence[Sequence[Any]],
) -> list[dict[str, Any]]:
    resultado: list[dict[str, Any]] = []
    for indice, columna in enumerate(columnas):
        if columna.upper() in COLUMNAS_IDENTIFICADOR or columna.upper().endswith("_ID"):
            continue
        valores = _valores_columna(filas, indice)
        if not valores or not all(_es_numero(valor) for valor in valores):
            continue
        resultado.append(
            {
                "nombre": f"Total {columna}",
                "numerador": columna,
                "descripcion": f"Suma de {columna}",
                "formato": "mxn" if _pista_monetaria(columna) else None,
            }
        )
    return resultado


def _leer_resumen(
    resumen: Sequence[Mapping[str, Any]] | None,
    columnas: Sequence[str],
    filas: Sequence[Sequence[Any]],
) -> list[_Resumen]:
    if resumen is None:
        resumen = _resumen_automatico(columnas, filas)
    if not isinstance(resumen, Sequence) or isinstance(resumen, (str, bytes)):
        raise XlsxError("E-XLS-020", "resumen debe ser una lista de mapas")

    indices = {columna: indice for indice, columna in enumerate(columnas)}
    n_filas = len(filas) + 1
    resultado: list[_Resumen] = []
    for posicion, item in enumerate(resumen):
        if not isinstance(item, Mapping):
            raise XlsxError("E-XLS-020", f"resumen[{posicion}] debe ser un mapa")
        nombre = _texto_obligatorio(
            item.get("nombre", item.get("metrica")),
            f"resumen[{posicion}].nombre",
        )
        numerador = item.get("numerador", item.get("columna"))
        if not isinstance(numerador, str) or numerador not in indices:
            raise XlsxError(
                "E-XLS-020",
                f"resumen[{posicion}].numerador debe ser una columna de Datos",
            )
        denominador = item.get("denominador")
        if denominador is not None and (
            not isinstance(denominador, str) or denominador not in indices
        ):
            raise XlsxError(
                "E-XLS-020",
                f"resumen[{posicion}].denominador debe ser una columna de Datos",
            )

        valores_num = _valores_columna(filas, indices[numerador])
        _validar_numerica(numerador, valores_num)
        total_num = _suma(valores_num)

        if denominador is not None:
            valores_den = _valores_columna(filas, indices[denominador])
            _validar_numerica(denominador, valores_den)
            total_den = _suma(valores_den)
            if total_den == 0:
                raise XlsxError(
                    "E-XLS-040",
                    f"la razón {nombre!r} tiene denominador cero en Datos",
                )
            valor = total_num / total_den
            formula = (
                f"=SUM({_rango(indices[numerador], n_filas)})"
                f"/SUM({_rango(indices[denominador], n_filas)})"
            )
            defecto = "0.00%"
            descripcion_defecto = f"Razón de sumas: Σ {numerador} / Σ {denominador}"
        else:
            valor = total_num
            formula = f"=SUM({_rango(indices[numerador], n_filas)})"
            defecto = _formato_suma_defecto(numerador, valores_num)
            descripcion_defecto = f"Suma de {numerador}"

        formato = _formato_excel(item.get("formato"), defecto)
        descripcion = (
            _texto_opcional(item.get("descripcion"), f"resumen[{posicion}].descripcion")
            or descripcion_defecto
        )
        resultado.append(
            _Resumen(
                nombre=nombre,
                numerador=numerador,
                denominador=denominador,
                formato=formato,
                descripcion=descripcion,
                formula=formula,
                valor=valor,
            )
        )
    return resultado


# ---------------------------------------------------------------------------
# Formatos de xlsxwriter
# ---------------------------------------------------------------------------


class _Formatos:
    """Caché de formatos de xlsxwriter con la identidad Banamex."""

    def __init__(self, libro: Any, branding: Mapping[str, Any]) -> None:
        self.libro = libro
        self.familia = _familia(branding)
        self.fondo = _color_hex(branding, "fondo")
        self.texto = _color_hex(branding, "texto")
        self.primario = _color_hex(branding, "primario")
        self.acento = _color_hex(branding, "acento")
        self.neutro = _color_hex(branding, "neutro_claro")
        self._cache: dict[tuple[Any, ...], Any] = {}

        self.encabezado = libro.add_format(
            {
                "font_name": self.familia,
                "font_size": 9.5,
                "bold": True,
                "font_color": self.fondo,
                "bg_color": self.primario,
                "border": 1,
                "border_color": self.primario,
                "align": "center",
                "valign": "vcenter",
                "text_wrap": True,
            }
        )
        self.titulo = libro.add_format(
            {
                "font_name": self.familia,
                "font_size": 16,
                "bold": True,
                "font_color": self.primario,
                "align": "left",
                "valign": "vcenter",
            }
        )
        self.subtitulo = libro.add_format(
            {
                "font_name": self.familia,
                "font_size": 10,
                "italic": True,
                "font_color": GRIS,
                "align": "left",
                "valign": "vcenter",
            }
        )
        self.nota = libro.add_format(
            {
                "font_name": self.familia,
                "font_size": 9,
                "italic": True,
                "font_color": GRIS,
                "text_wrap": True,
                "valign": "top",
            }
        )
        self.metrica = libro.add_format(
            {
                "font_name": self.familia,
                "font_size": 9.5,
                "bold": True,
                "font_color": self.primario,
                "bg_color": self.neutro,
                "border": 1,
                "border_color": BORDE,
                "valign": "vcenter",
                "text_wrap": True,
            }
        )

    def _base(self, alterna: bool) -> dict[str, Any]:
        return {
            "font_name": self.familia,
            "font_size": 9.5,
            "font_color": self.texto,
            "bg_color": self.neutro if alterna else self.fondo,
            "border": 1,
            "border_color": BORDE,
            "valign": "vcenter",
        }

    def celda(self, tipo: str, alterna: bool = False) -> Any:
        clave = ("celda", tipo, alterna)
        if clave in self._cache:
            return self._cache[clave]
        propiedades = self._base(alterna)
        if tipo == "texto":
            propiedades.update({"align": "left"})
        elif tipo == "entero":
            propiedades.update({"num_format": "#,##0", "align": "right"})
        elif tipo == "decimal":
            propiedades.update({"num_format": "#,##0.00", "align": "right"})
        elif tipo == "pct":
            propiedades.update({"num_format": "0.00%", "align": "right"})
        elif tipo == "mxn":
            propiedades.update({"num_format": "$#,##0", "align": "right"})
        elif tipo == "fecha":
            propiedades.update({"num_format": "yyyy-mm-dd", "align": "center"})
        else:
            propiedades.update({"align": "center"})
        formato = self.libro.add_format(propiedades)
        self._cache[clave] = formato
        return formato

    def numero(self, formato_excel: str, alterna: bool = False) -> Any:
        if formato_excel == "General":
            return self.celda("general", alterna)
        if formato_excel == "@":
            return self.celda("texto", alterna)
        if formato_excel == "0.00%":
            return self.celda("pct", alterna)
        if formato_excel == "$#,##0":
            return self.celda("mxn", alterna)
        clave = ("numero", formato_excel, alterna)
        if clave in self._cache:
            return self._cache[clave]
        propiedades = self._base(alterna)
        propiedades.update(
            {
                "num_format": formato_excel,
                "align": "center" if formato_excel.startswith("yy") else "right",
            }
        )
        formato = self.libro.add_format(propiedades)
        self._cache[clave] = formato
        return formato


# ---------------------------------------------------------------------------
# Escritura de hojas
# ---------------------------------------------------------------------------


def _sustituir_celda(
    valor: Any,
    metricas: Mapping[str, float | int],
    fila: int,
    columna: int,
) -> Any:
    if isinstance(valor, str) and "{{m." in valor:
        return _sustituir(valor, metricas, f"Datos[{fila + 1},{columna + 1}]")
    return valor


def _escribir_celda(
    hoja: Any,
    fila: int,
    columna: int,
    valor: Any,
    formato_columna: str | None,
    formatos: _Formatos,
    alterna: bool,
) -> None:
    if valor is None:
        hoja.write_blank(fila, columna, None, formatos.celda("general", alterna))
        return
    if isinstance(valor, bool):
        hoja.write_boolean(fila, columna, valor, formatos.celda("general", alterna))
        return
    if isinstance(valor, datetime):
        hoja.write_datetime(
            fila,
            columna,
            valor,
            formatos.numero(formato_columna or "yyyy-mm-dd", alterna),
        )
        return
    if isinstance(valor, date):
        hoja.write_datetime(
            fila,
            columna,
            datetime(valor.year, valor.month, valor.day),
            formatos.numero(formato_columna or "yyyy-mm-dd", alterna),
        )
        return
    if isinstance(valor, (Integral, Real, Decimal)) and not isinstance(valor, bool):
        if formato_columna is not None:
            formato = formatos.numero(formato_columna, alterna)
        else:
            formato = formatos.celda(
                "entero" if isinstance(valor, Integral) else "decimal",
                alterna,
            )
        hoja.write_number(fila, columna, float(valor), formato)
        return
    if isinstance(valor, str):
        hoja.write_string(fila, columna, valor, formatos.celda("texto", alterna))
        return
    raise XlsxError(
        "E-XLS-010",
        f"tipo de dato no soportado en Datos[{fila + 1},{columna + 1}]: {type(valor).__name__}",
    )


def _ancho_columna(columna: str, valores: Sequence[Any]) -> float:
    largos = [len(columna)]
    for valor in valores:
        if valor is None:
            continue
        if isinstance(valor, (datetime, date)):
            largos.append(10)
        else:
            largos.append(len(str(valor)))
    return float(min(max(max(largos) + 2, 10), 40))


def _encabezado_pie(hoja: Any) -> None:
    hoja.set_header('&L&"Liberation Sans,Bold"Banamex · Base Cards&RConfidencial — uso interno')
    hoja.set_footer("&LGenerado por Base Cards&RPágina &P de &N")


def _escribir_datos(
    hoja: Any,
    columnas: Sequence[str],
    filas: Sequence[Sequence[Any]],
    formatos_columnas: Mapping[str, str],
    formatos: _Formatos,
    metricas: Mapping[str, float | int],
) -> None:
    for indice, columna in enumerate(columnas):
        hoja.set_column(
            indice,
            indice,
            _ancho_columna(columna, [fila[indice] for fila in filas]),
        )
    hoja.set_row(0, 28)
    hoja.write_row(0, 0, list(columnas), formatos.encabezado)

    for numero_fila, fila in enumerate(filas, start=1):
        alterna = numero_fila % 2 == 0
        for indice, valor in enumerate(fila):
            valor = _sustituir_celda(valor, metricas, numero_fila, indice)
            _escribir_celda(
                hoja,
                numero_fila,
                indice,
                valor,
                formatos_columnas.get(columnas[indice]),
                formatos,
                alterna,
            )

    hoja.freeze_panes(1, 0)
    hoja.autofilter(0, 0, len(filas), len(columnas) - 1)
    hoja.set_tab_color(formatos.primario)
    hoja.hide_gridlines(2)
    hoja.set_landscape()
    hoja.fit_to_pages(1, 0)
    hoja.set_paper(1)
    hoja.repeat_rows(0)
    _encabezado_pie(hoja)


def _escribir_resumen(
    hoja: Any,
    resumen: Sequence[_Resumen],
    formatos: _Formatos,
    metricas: Mapping[str, float | int],
    titulo: str,
    corte: str | None,
) -> None:
    hoja.hide_gridlines(2)
    for indice, ancho in enumerate((34, 22, 22, 18, 55)):
        hoja.set_column(indice, indice, ancho)
    hoja.merge_range(0, 0, 0, 4, titulo, formatos.titulo)
    hoja.set_row(0, 28)
    subtitulo = f"Corte {corte} · Base Cards" if corte else "Base Cards"
    hoja.merge_range(1, 0, 1, 4, subtitulo, formatos.subtitulo)

    encabezados = ("Métrica", "Numerador", "Denominador", "Valor", "Descripción")
    hoja.write_row(3, 0, encabezados, formatos.encabezado)
    hoja.set_row(3, 28)

    for posicion, item in enumerate(resumen):
        fila = 4 + posicion
        alterna = (fila + 1) % 2 == 0
        hoja.write_string(
            fila,
            0,
            _sustituir(item.nombre, metricas, f"resumen[{posicion}].nombre"),
            formatos.metrica,
        )
        hoja.write_string(
            fila,
            1,
            item.numerador,
            formatos.celda("texto", alterna),
        )
        hoja.write_string(
            fila,
            2,
            item.denominador or "—",
            formatos.celda("texto", alterna),
        )
        hoja.write_formula(
            fila,
            3,
            item.formula,
            formatos.numero(item.formato, alterna),
            item.valor,
        )
        hoja.write_string(
            fila,
            4,
            _sustituir(item.descripcion, metricas, f"resumen[{posicion}].descripcion"),
            formatos.celda("texto", alterna),
        )

    nota_fila = 4 + len(resumen) + 1
    hoja.merge_range(
        nota_fila,
        0,
        nota_fila,
        4,
        "Las razones se calculan como razón de sumas (Σ numerador / Σ denominador), "
        "nunca como promedio de razones. Fuente: Cubo Base Cards.",
        formatos.nota,
    )
    hoja.set_row(nota_fila, 32)
    hoja.freeze_panes(4, 0)
    hoja.set_tab_color(formatos.acento)
    hoja.set_portrait()
    hoja.fit_to_pages(1, 0)
    hoja.set_paper(1)
    _encabezado_pie(hoja)


# ---------------------------------------------------------------------------
# Verificación con openpyxl
# ---------------------------------------------------------------------------


def _verificar(
    ruta: Path,
    columnas: Sequence[str],
    filas: Sequence[Sequence[Any]],
    formatos_columnas: Mapping[str, str],
    resumen: Sequence[_Resumen],
) -> None:
    try:
        libro_formulas = load_workbook(str(ruta), data_only=False)
        libro_valores = load_workbook(str(ruta), data_only=True)
    except Exception as exc:
        raise XlsxError(
            "E-XLS-051", f"el archivo generado no se puede abrir con openpyxl: {exc}", "salida"
        ) from exc

    if libro_formulas.sheetnames != list(HOJAS):
        raise XlsxError(
            "E-XLS-060",
            f"el libro debe tener las hojas {list(HOJAS)}; tiene {libro_formulas.sheetnames}",
        )

    hoja_datos = libro_formulas["Datos"]
    if hoja_datos.freeze_panes != "A2":
        raise XlsxError("E-XLS-060", "la hoja Datos no tiene congelados los encabezados")
    rango_esperado = f"A1:{xl_col_to_name(len(columnas) - 1)}{len(filas) + 1}"
    if hoja_datos.auto_filter.ref != rango_esperado:
        raise XlsxError(
            "E-XLS-060",
            f"el autofiltro de Datos es {hoja_datos.auto_filter.ref!r}; se esperaba {rango_esperado!r}",
        )
    for indice, columna in enumerate(columnas, start=1):
        if hoja_datos.cell(1, indice).value != columna:
            raise XlsxError("E-XLS-060", f"falta el encabezado {columna!r} en Datos")

    for indice, columna in enumerate(columnas, start=1):
        formato = formatos_columnas.get(columna)
        if formato is None:
            continue
        for fila in range(2, len(filas) + 2):
            celda = hoja_datos.cell(fila, indice)
            if celda.value is None:
                continue
            if not isinstance(celda.value, (datetime, date, int, float)):
                break
            if isinstance(celda.value, bool):
                break
            if celda.number_format != formato:
                raise XlsxError(
                    "E-XLS-060",
                    f"la columna {columna!r} usa el formato {celda.number_format!r}; "
                    f"se esperaba {formato!r}",
                )
            break

    hoja_resumen = libro_formulas["Resumen"]
    if hoja_resumen.freeze_panes != "A5":
        raise XlsxError("E-XLS-060", "la hoja Resumen no tiene congelados los encabezados")
    encabezados = ("Métrica", "Numerador", "Denominador", "Valor", "Descripción")
    for indice, esperado in enumerate(encabezados, start=1):
        if hoja_resumen.cell(4, indice).value != esperado:
            raise XlsxError("E-XLS-060", f"falta el encabezado {esperado!r} en Resumen")

    hoja_valores = libro_valores["Resumen"]
    for posicion, item in enumerate(resumen):
        fila = 5 + posicion
        celda_formula = hoja_resumen.cell(fila, 4)
        if celda_formula.data_type != "f" or celda_formula.value != item.formula:
            raise XlsxError(
                "E-XLS-060",
                f"la fórmula de {item.nombre!r} es {celda_formula.value!r}; "
                f"se esperaba {item.formula!r}",
            )
        cache = hoja_valores.cell(fila, 4).value
        if isinstance(cache, bool) or not isinstance(cache, (int, float)):
            raise XlsxError("E-XLS-060", f"la fórmula de {item.nombre!r} no tiene valor cacheado")
        if not math.isclose(float(cache), item.valor, rel_tol=1e-9, abs_tol=1e-9):
            raise XlsxError(
                "E-XLS-060",
                f"el valor cacheado de {item.nombre!r} es {cache!r}; Python calculó {item.valor!r}",
            )
        if celda_formula.number_format != item.formato:
            raise XlsxError(
                "E-XLS-060",
                f"el formato de {item.nombre!r} es {celda_formula.number_format!r}; "
                f"se esperaba {item.formato!r}",
            )

    for hoja in libro_formulas.worksheets:
        for fila in hoja.iter_rows():
            for celda in fila:
                if isinstance(celda.value, str) and "{{m." in celda.value:
                    raise XlsxError(
                        "E-XLS-030",
                        f"quedó un marcador sin sustituir en {hoja.title}!{celda.coordinate}",
                    )


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def xlsx_ensamblar(
    salida: Path | str,
    *,
    datos: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    resumen: Sequence[Mapping[str, Any]] | None = None,
    metricas: Mapping[str, float | int] | None = None,
    formatos: Mapping[str, Any] | None = None,
    titulo: str | None = None,
    corte: str | None = None,
) -> Path:
    """Ensambla el anexo ``.xlsx`` con las hojas ``Datos`` y ``Resumen``.

    Parameters
    ----------
    salida:
        Ruta del ``.xlsx`` a generar.
    datos:
        Mapa con ``columnas`` y ``filas`` (listas o mapas), o una lista de
        mapas. Puede incluir ``formatos`` por columna.
    resumen:
        Lista de métricas del resumen. Cada una declara ``nombre``,
        ``numerador`` y, para una razón, ``denominador``; opcionalmente
        ``formato`` y ``descripcion``. Si se omite, se suman las columnas
        numéricas que no sean identificadores.
    metricas:
        Diccionario ``{{m.<metrica>.<cohorte>.<MOBn>|<formato>}}`` → valor para
        sustituir marcadores en textos.
    formatos:
        Formatos de celda por columna de ``Datos`` (``pct``, ``mxn``, ``int``,
        ``num``, ``fecha`` o un formato de Excel).
    titulo:
        Título de la hoja ``Resumen``.
    corte:
        Corte en formato ``AAAAMM``. Si se omite, se infiere de ``metricas``.

    Returns
    -------
    Path
        Ruta absoluta del archivo generado.
    """
    destino = Path(salida).expanduser()
    if destino.suffix.lower() != ".xlsx":
        raise XlsxError("E-XLS-050", f"la salida debe terminar en .xlsx: {destino}", "salida")
    destino = destino.resolve()
    metricas = metricas or {}
    if not isinstance(metricas, Mapping):
        raise XlsxError("E-XLS-030", "metricas debe ser un mapa de marcador a valor")

    branding = _cargar_branding()
    columnas, filas, formatos_columnas = _normalizar_datos(datos, formatos)
    filas_resumen = _leer_resumen(resumen, columnas, filas)
    if corte is None:
        corte = _inferir_corte(metricas)
    titulo_final = _sustituir(titulo or "Resumen", metricas, "titulo")
    corte_final = _sustituir(corte, metricas, "corte") if isinstance(corte, str) else None

    destino.parent.mkdir(parents=True, exist_ok=True)
    try:
        libro = xlsxwriter.Workbook(
            str(destino),
            {
                "strings_to_numbers": False,
                "strings_to_formulas": False,
                "strings_to_urls": False,
            },
        )
    except Exception as exc:
        raise XlsxError("E-XLS-050", f"no se pudo crear {destino}: {exc}", "salida") from exc

    try:
        formatos_libro = _Formatos(libro, branding)
        hoja_datos = libro.add_worksheet("Datos")
        hoja_resumen = libro.add_worksheet("Resumen")
        _escribir_datos(
            hoja_datos,
            columnas,
            filas,
            formatos_columnas,
            formatos_libro,
            metricas,
        )
        _escribir_resumen(
            hoja_resumen,
            filas_resumen,
            formatos_libro,
            metricas,
            titulo_final,
            corte_final,
        )
        libro.set_properties(
            {
                "title": titulo_final,
                "subject": "Anexo de datos Base Cards",
                "author": "basecards",
                "company": "Banamex",
                "comments": "Generado con xlsxwriter y verificado con openpyxl",
            }
        )
        libro.close()
    except XlsxError:
        _cerrar_sin_error(libro)
        destino.unlink(missing_ok=True)
        raise
    except Exception as exc:
        _cerrar_sin_error(libro)
        destino.unlink(missing_ok=True)
        raise XlsxError("E-XLS-050", f"no se pudo generar {destino}: {exc}", "salida") from exc

    try:
        _verificar(destino, columnas, filas, formatos_columnas, filas_resumen)
    except XlsxError:
        destino.unlink(missing_ok=True)
        raise
    return destino


def _cerrar_sin_error(libro: Any) -> None:
    """Cierra el libro ignorando errores para limpiar el archivo parcial."""
    with contextlib.suppress(Exception):
        libro.close()
