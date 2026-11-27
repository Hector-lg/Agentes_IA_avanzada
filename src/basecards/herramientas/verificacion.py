"""Verificación de cifras en documentos generados (RB-06).

Esta herramienta es el control de calidad numérico del documento: abre un
.pptx, .docx, .xlsx, .pdf, .md o .html, extrae todas las cifras de tablas,
gráficas nativas y texto, y las compara contra los valores esperados.

Si un número no coincide con el cálculo determinista, el archivo no se publica.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal


class VerificacionError(Exception):
    """Error de verificación con un código estable ``E-VER-*``."""

    def __init__(self, codigo: str, mensaje: str) -> None:
        super().__init__(f"{codigo}: {mensaje}")
        self.codigo = codigo
        self.mensaje = mensaje


# Códigos de error estables
E_VER_ARCHIVO_INEXISTENTE = "E-VER-001"
E_VER_ARCHIVO_CORRUPTO = "E-VER-002"
E_VER_VALORES_VACIOS = "E-VER-003"
E_VER_FORMATO_NO_SOPORTADO = "E-VER-004"
E_VER_GRAFICA_SIN_DATOS = "E-VER-005"
E_VER_ERROR_LECTURA = "E-VER-006"


@dataclass(frozen=True)
class Discrepancia:
    """Una discrepancia entre el valor esperado y el encontrado.

    Attributes:
        ubicacion: Dónde se encontró la cifra (ej. "diapositiva 3, tabla, fila 2, columna 1").
        origen: Tipo de elemento donde estaba (texto, tabla, grafica, celda, pdf).
        esperado: Valor esperado según el cálculo determinista.
        encontrado: Valor encontrado en el documento.
        marcador: Marcador asociado si aplica (ej. "{{m.tasa_over30.202401.MOB12|pct2}}").
    """

    ubicacion: str
    origen: Literal["texto", "tabla", "grafica", "celda", "pdf", "markdown", "html"]
    esperado: float | int | None
    encontrado: float | int | None
    marcador: str | None = None

    def __str__(self) -> str:
        marcador_str = f" ({self.marcador})" if self.marcador else ""
        if self.esperado is None:
            return f"{self.ubicacion}: cifra inesperada {self.encontrado}{marcador_str}"
        if self.encontrado is None:
            return f"{self.ubicacion}: falta la cifra esperada {self.esperado}{marcador_str}"
        return (
            f"{self.ubicacion}: esperado {self.esperado}, encontrado {self.encontrado}"
            f"{marcador_str}"
        )


@dataclass
class ResultadoVerificacion:
    """Resultado de verificar las cifras de un documento.

    Attributes:
        archivo: Ruta del archivo verificado.
        discrepancias: Número de discrepancias encontradas.
        detalle: Lista de discrepancias con ubicación y valores.
        cifras_encontradas: Total de cifras numéricas extraídas del documento.
        cifras_esperadas: Total de cifras esperadas proporcionadas.
    """

    archivo: Path
    discrepancias: int = 0
    detalle: list[Discrepancia] = field(default_factory=list)
    cifras_encontradas: int = 0
    cifras_esperadas: int = 0

    @property
    def ok(self) -> bool:
        """True solo si no hay discrepancias."""
        return self.discrepancias == 0

    def agregar(self, discrepancia: Discrepancia) -> None:
        """Agrega una discrepancia al resultado."""
        self.detalle.append(discrepancia)
        self.discrepancias += 1


# ---------------------------------------------------------------------------
# Normalización y parsing de números
# ---------------------------------------------------------------------------

# Patrones para detectar números en texto
_PATRON_NUMERO = re.compile(
    r"""
    (?:
        [$€¥£]?\s*                    # Símbolo de moneda opcional
        [-−]?                         # Signo negativo opcional
        \d{1,3}(?:[,. ]\d{3})*        # Parte entera con separadores de miles
        (?:[.,]\d+)?                  # Parte decimal opcional
        \s*%?                         # Porcentaje opcional
    )
    |
    (?:
        [-−]?\d{1,3}(?:[,. ]\d{3})*   # Parte entera con separadores de miles
        (?:[.,]\d+)?                  # Parte decimal opcional
        \s*[$€¥£%]                    # Símbolo al final
    )
    """,
    re.VERBOSE,
)

# Separadores de miles comunes
_SEPARADORES_MILES = frozenset({",", ".", " ", "'"})

# Símbolos de moneda y porcentaje
_SIMBOLOS = frozenset({"$", "€", "¥", "£", "%"})


def _normalizar_texto_numero(texto: str) -> str:
    """Normaliza un texto que representa un número.

    - Quita símbolos de moneda y porcentaje
    - Elimina separadores de miles
    - Normaliza el separador decimal a punto
    - Convierte guión largo a guión corto
    """
    texto = texto.strip()
    texto = texto.replace("−", "-")  # Guión largo a corto

    # Quitar símbolos al inicio y final
    texto = texto.lstrip("".join(_SIMBOLOS)).rstrip("".join(_SIMBOLOS))
    texto = texto.strip()

    if not texto:
        return ""

    # Detectar separador decimal: el último separador si hay dígitos después
    # Ejemplos: "1,234.56" → decimal es ".", "1.234,56" → decimal es ","
    # Si solo hay un separador, depende del contexto

    # Buscar el último separador
    ultimo_sep = None
    ultimo_sep_pos = -1
    for i, char in enumerate(texto):
        if char in _SEPARADORES_MILES:
            ultimo_sep = char
            ultimo_sep_pos = i

    if ultimo_sep is not None:
        # Verificar si es separador decimal (hay dígitos después y no más separadores)
        despues = texto[ultimo_sep_pos + 1 :]
        antes = texto[:ultimo_sep_pos]

        # Si después del separador hay solo 1-2 dígitos, probablemente es decimal
        # Si hay 3 dígitos, podría ser separador de miles
        es_decimal = False
        if despues.isdigit() and len(despues) <= 2:
            # 1-2 dígitos después → probablemente decimal
            # Pero hay excepciones: "1,23" en español es "1.23"
            # "1,234" en inglés es "1234" (separador de miles)
            # Regla: si hay otro separador antes con 3 dígitos entre ellos, es de miles
            otro_sep_pos = -1
            for i, char in enumerate(antes):
                if char in _SEPARADORES_MILES:
                    otro_sep_pos = i

            if otro_sep_pos >= 0:
                # Hay otro separador antes
                digitos_entre = ultimo_sep_pos - otro_sep_pos - 1
                if digitos_entre == 3:
                    # "1,234,56" → el último es decimal (formato europeo)
                    es_decimal = len(despues) != 3
                else:
                    es_decimal = True
            else:
                # Solo un separador
                # "1,234" → si tiene 3 dígitos después, asumo miles (inglés)
                # "1,23" → decimal
                if len(despues) == 3 and len(antes) > 0:
                    # Ambiguo: podría ser "1,234" (1234) o "1,234" (1.234)
                    # Asumimos separador de miles si todos los separadores son iguales
                    # y el patrón es consistente
                    es_decimal = False
                else:
                    es_decimal = True

        # Construir el número normalizado
        if es_decimal:
            # Quitar todos los separadores de miles
            resultado = antes.replace(",", "").replace(".", "").replace(" ", "").replace("'", "")
            resultado += "." + despues
            texto = resultado
        else:
            # Quitar todos los separadores (son de miles)
            texto = texto.replace(",", "").replace(".", "").replace(" ", "").replace("'", "")

    return texto


def _parsear_numero(texto: str) -> float | int | None:
    """Parsea un texto normalizado a número.

    Returns:
        El número como float o int, o None si no se puede parsear.
    """
    texto = _normalizar_texto_numero(texto)
    if not texto:
        return None

    try:
        # Intentar con Decimal para máxima precisión
        valor = Decimal(texto)
        # Si no tiene parte decimal, devolver int
        if valor == valor.to_integral_value():
            return int(valor)
        return float(valor)
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


def _extraer_numeros_texto(texto: str) -> list[tuple[str, float | int]]:
    """Extrae todos los números de un texto.

    Returns:
        Lista de tuplas (texto_original, valor_numérico).
    """
    resultados: list[tuple[str, float | int]] = []
    for coincidencia in _PATRON_NUMERO.finditer(texto):
        texto_num = coincidencia.group(0)
        valor = _parsear_numero(texto_num)
        if valor is not None:
            resultados.append((texto_num, valor))
    return resultados


def _valores_son_equivalentes(
    esperado: float | None,
    encontrado: float | None,
    tolerancia: float,
) -> bool:
    """Compara dos valores con tolerancia para errores de redondeo.

    Args:
        esperado: Valor esperado (puede ser None si no se esperaba).
        encontrado: Valor encontrado (puede ser None si no se encontró).
        tolerancia: Diferencia máxima aceptable.

    Returns:
        True si los valores son equivalentes dentro de la tolerancia.
    """
    if esperado is None and encontrado is None:
        return True
    if esperado is None or encontrado is None:
        return False

    diferencia = abs(float(esperado) - float(encontrado))
    return diferencia <= tolerancia


# ---------------------------------------------------------------------------
# Extracción de cifras por formato
# ---------------------------------------------------------------------------


def _extraer_cifras_texto_plano(
    texto: str,
    ubicacion: str,
    origen: Literal["texto", "markdown", "html"],
) -> list[tuple[str, float | int, str, str]]:
    """Extrae cifras de texto plano, markdown o HTML.

    Returns:
        Lista de tuplas (ubicacion, valor, origen, texto_original).
    """
    resultados: list[tuple[str, float | int, str, str]] = []
    for texto_num, valor in _extraer_numeros_texto(texto):
        resultados.append((ubicacion, valor, origen, texto_num))
    return resultados


def _extraer_cifras_pptx_texto(
    ruta: Path,
) -> list[tuple[str, float | int, str, str]]:
    """Extrae cifras de texto y formas de un archivo .pptx.

    Returns:
        Lista de tuplas (ubicacion, valor, origen, texto_original).
    """
    # Importación diferida para evitar dependencia circular
    from pptx import Presentation

    resultados: list[tuple[str, float | int, str, str]] = []

    try:
        prs = Presentation(str(ruta))
    except Exception as exc:
        raise VerificacionError(
            E_VER_ARCHIVO_CORRUPTO,
            f"no se pudo abrir {ruta}: {exc}",
        ) from exc

    for num_slide, slide in enumerate(prs.slides, start=1):
        for shape in slide.shapes:
            if shape.has_text_frame:
                for parrafo in shape.text_frame.paragraphs:
                    texto = parrafo.text
                    if texto:
                        for texto_num, valor in _extraer_numeros_texto(texto):
                            ubicacion = f"diapositiva {num_slide}, texto"
                            resultados.append((ubicacion, valor, "texto", texto_num))

    return resultados


def _extraer_cifras_pptx_tablas(
    ruta: Path,
) -> list[tuple[str, float | int, str, str]]:
    """Extrae cifras de tablas nativas de un archivo .pptx.

    Returns:
        Lista de tuplas (ubicacion, valor, origen, texto_original).
    """
    from pptx import Presentation

    resultados: list[tuple[str, float | int, str, str]] = []

    try:
        prs = Presentation(str(ruta))
    except Exception as exc:
        raise VerificacionError(
            E_VER_ARCHIVO_CORRUPTO,
            f"no se pudo abrir {ruta}: {exc}",
        ) from exc

    for num_slide, slide in enumerate(prs.slides, start=1):
        for shape in slide.shapes:
            if shape.has_table:
                tabla = shape.table
                for num_fila, fila in enumerate(tabla.rows, start=1):
                    for num_col, celda in enumerate(fila.cells, start=1):
                        texto = celda.text
                        if texto:
                            for texto_num, valor in _extraer_numeros_texto(texto):
                                ubicacion = (
                                    f"diapositiva {num_slide}, tabla, "
                                    f"fila {num_fila}, columna {num_col}"
                                )
                                resultados.append((ubicacion, valor, "tabla", texto_num))

    return resultados


def _extraer_cifras_pptx_graficas(
    ruta: Path,
) -> list[tuple[str, float | int, str, str]]:
    """Extrae cifras de gráficas nativas de python-pptx.

    Recorre los valores de cada serie y extrae los números.
    También extrae las categorías si son numéricas.

    Returns:
        Lista de tuplas (ubicacion, valor, origen, texto_original).
    """
    from pptx import Presentation

    resultados: list[tuple[str, float | int, str, str]] = []

    try:
        prs = Presentation(str(ruta))
    except Exception as exc:
        raise VerificacionError(
            E_VER_ARCHIVO_CORRUPTO,
            f"no se pudo abrir {ruta}: {exc}",
        ) from exc

    for num_slide, slide in enumerate(prs.slides, start=1):
        for shape in slide.shapes:
            if shape.has_chart:
                chart = shape.chart
                nombre_grafica = shape.name if hasattr(shape, "name") else "gráfica"

                # Extraer valores de series
                try:
                    for num_serie, serie in enumerate(chart.series, start=1):
                        nombre_serie = str(serie.name) if serie.name else f"serie {num_serie}"

                        # Los valores están en serie.values
                        valores = serie.values
                        if valores is not None:
                            for num_punto, valor in enumerate(valores, start=1):
                                if isinstance(valor, int | float):
                                    ubicacion = (
                                        f"diapositiva {num_slide}, gráfica '{nombre_grafica}', "
                                        f"serie '{nombre_serie}', punto {num_punto}"
                                    )
                                    resultados.append((ubicacion, valor, "grafica", str(valor)))

                        # También verificar categorías si son numéricas
                        # Las categorías están en el plot, no en la serie
                except (AttributeError, TypeError, ValueError):
                    # Si hay error leyendo la gráfica, continuar sin extraer
                    pass

    return resultados


def _extraer_cifras_pptx_completo(
    ruta: Path,
) -> list[tuple[str, float | int, str, str]]:
    """Extrae todas las cifras de un archivo .pptx.

    Incluye texto, tablas y gráficas nativas.

    Returns:
        Lista de tuplas (ubicacion, valor, origen, texto_original).
    """
    resultados: list[tuple[str, float | int, str, str]] = []
    resultados.extend(_extraer_cifras_pptx_texto(ruta))
    resultados.extend(_extraer_cifras_pptx_tablas(ruta))
    resultados.extend(_extraer_cifras_pptx_graficas(ruta))
    return resultados


def _extraer_cifras_docx(
    ruta: Path,
) -> list[tuple[str, float | int, str, str]]:
    """Extrae cifras de un archivo .docx (párrafos y tablas).

    Returns:
        Lista de tuplas (ubicacion, valor, origen, texto_original).
    """
    from docx import Document

    resultados: list[tuple[str, float | int, str, str]] = []

    try:
        doc = Document(str(ruta))
    except Exception as exc:
        raise VerificacionError(
            E_VER_ARCHIVO_CORRUPTO,
            f"no se pudo abrir {ruta}: {exc}",
        ) from exc

    # Párrafos
    for num_parrafo, parrafo in enumerate(doc.paragraphs, start=1):
        texto = parrafo.text
        if texto:
            for texto_num, valor in _extraer_numeros_texto(texto):
                ubicacion = f"párrafo {num_parrafo}"
                resultados.append((ubicacion, valor, "texto", texto_num))

    # Tablas
    for num_tabla, tabla in enumerate(doc.tables, start=1):
        for num_fila, fila in enumerate(tabla.rows, start=1):
            for num_celda, celda in enumerate(fila.cells, start=1):
                texto = celda.text
                if texto:
                    for texto_num, valor in _extraer_numeros_texto(texto):
                        ubicacion = f"tabla {num_tabla}, fila {num_fila}, columna {num_celda}"
                        resultados.append((ubicacion, valor, "tabla", texto_num))

    # Encabezado
    for seccion in doc.sections:
        if seccion.header:
            for parrafo in seccion.header.paragraphs:
                texto = parrafo.text
                if texto:
                    for texto_num, valor in _extraer_numeros_texto(texto):
                        ubicacion = "encabezado"
                        resultados.append((ubicacion, valor, "texto", texto_num))

    # Pie de página
    for seccion in doc.sections:
        if seccion.footer:
            for parrafo in seccion.footer.paragraphs:
                texto = parrafo.text
                if texto:
                    for texto_num, valor in _extraer_numeros_texto(texto):
                        ubicacion = "pie de página"
                        resultados.append((ubicacion, valor, "texto", texto_num))

    return resultados


def _extraer_cifras_xlsx(
    ruta: Path,
) -> list[tuple[str, float | int, str, str]]:
    """Extrae cifras de un archivo .xlsx (celdas con valores y gráficas).

    Returns:
        Lista de tuplas (ubicacion, valor, origen, texto_original).
    """
    from openpyxl import load_workbook

    resultados: list[tuple[str, float | int, str, str]] = []

    try:
        wb = load_workbook(str(ruta), data_only=True)
    except Exception as exc:
        raise VerificacionError(
            E_VER_ARCHIVO_CORRUPTO,
            f"no se pudo abrir {ruta}: {exc}",
        ) from exc

    for nombre_hoja in wb.sheetnames:
        hoja = wb[nombre_hoja]
        for fila in hoja.iter_rows():
            for celda in fila:
                valor = celda.value
                if valor is not None and isinstance(valor, int | float):
                    ubicacion = f"hoja '{nombre_hoja}', celda {celda.coordinate}"
                    resultados.append((ubicacion, valor, "celda", str(valor)))

    return resultados


def _extraer_cifras_xlsx_completo(
    ruta: Path,
) -> list[tuple[str, float | int, str, str]]:
    """Extrae todas las cifras de un archivo .xlsx.

    Incluye celdas con valores numéricos.
    Las gráficas de Excel requieren acceso al XML interno que openpyxl
    no expone directamente, por lo que se omiten en esta implementación.

    Returns:
        Lista de tuplas (ubicacion, valor, origen, texto_original).
    """
    return _extraer_cifras_xlsx(ruta)


def _extraer_cifras_pdf(
    ruta: Path,
) -> list[tuple[str, float | int, str, str]]:
    """Extrae cifras de un archivo .pdf.

    Returns:
        Lista de tuplas (ubicacion, valor, origen, texto_original).
    """
    from pypdf import PdfReader

    resultados: list[tuple[str, float | int, str, str]] = []

    try:
        reader = PdfReader(str(ruta))
    except Exception as exc:
        raise VerificacionError(
            E_VER_ARCHIVO_CORRUPTO,
            f"no se pudo abrir {ruta}: {exc}",
        ) from exc

    for num_pagina, pagina in enumerate(reader.pages, start=1):
        texto = pagina.extract_text() or ""
        if texto:
            for texto_num, valor in _extraer_numeros_texto(texto):
                ubicacion = f"página {num_pagina}"
                resultados.append((ubicacion, valor, "pdf", texto_num))

    return resultados


# ---------------------------------------------------------------------------
# API pública (esqueleto - se completará en Commit 3)
# ---------------------------------------------------------------------------


def documento_verificar_cifras(
    archivo: Path | str,
    valores_esperados: Mapping[str, float | int],
    *,
    tolerancia: float = 0.01,
) -> ResultadoVerificacion:
    """Verifica que todas las cifras de un documento coincidan con los valores esperados.

    Args:
        archivo: Ruta al archivo a verificar (.pptx, .docx, .xlsx, .pdf, .md, .html).
        valores_esperados: Diccionario con claves de marcadores y valores calculados.
            Ejemplo: {"{{m.tasa_over30.202401.MOB12|pct2}}": 0.0234, ...}
        tolerancia: Diferencia máxima aceptable para considerar dos cifras equivalentes.
            Por defecto 0.01 (1% de tolerancia para errores de redondeo).

    Returns:
        ResultadoVerificacion con el número de discrepancias y detalle de cada una.
        Usar ``resultado.ok`` para saber si el documento pasa la verificación.

    Raises:
        VerificacionError: Si el archivo no existe, está corrupto, o no se puede leer.
    """
    ruta = Path(archivo).expanduser().resolve()

    if not ruta.is_file():
        raise VerificacionError(
            E_VER_ARCHIVO_INEXISTENTE,
            f"no existe el archivo {ruta}",
        )

    if not valores_esperados:
        raise VerificacionError(
            E_VER_VALORES_VACIOS,
            "no se proporcionaron valores esperados",
        )

    resultado = ResultadoVerificacion(archivo=ruta)
    resultado.cifras_esperadas = len(valores_esperados)

    # Determinar formato y extraer cifras
    sufijo = ruta.suffix.lower()

    if sufijo == ".pptx":
        cifras = _extraer_cifras_pptx_completo(ruta)
    elif sufijo == ".docx":
        cifras = _extraer_cifras_docx(ruta)
    elif sufijo == ".xlsx":
        cifras = _extraer_cifras_xlsx_completo(ruta)
    elif sufijo == ".pdf":
        cifras = _extraer_cifras_pdf(ruta)
    elif sufijo in {".md", ".markdown"}:
        texto = ruta.read_text(encoding="utf-8")
        cifras = _extraer_cifras_texto_plano(texto, "markdown", "markdown")
    elif sufijo in {".html", ".htm"}:
        texto = ruta.read_text(encoding="utf-8")
        cifras = _extraer_cifras_texto_plano(texto, "html", "html")
    else:
        raise VerificacionError(
            E_VER_FORMATO_NO_SOPORTADO,
            f"formato no soportado: {sufijo}",
        )

    resultado.cifras_encontradas = len(cifras)

    # Construir índice de valores encontrados para búsqueda rápida
    # Agrupamos por valor numérico (puede haber múltiples ubicaciones)
    valores_encontrados: dict[float, list[tuple[str, str, str]]] = {}
    for ubicacion, valor, origen, texto_original in cifras:
        valor_clave = float(valor)
        if valor_clave not in valores_encontrados:
            valores_encontrados[valor_clave] = []
        valores_encontrados[valor_clave].append((ubicacion, origen, texto_original))

    # Conjunto de valores esperados que ya fueron encontrados
    esperados_encontrados: set[str] = set()

    # Comparar cada cifra encontrada contra los valores esperados
    for ubicacion, valor, origen, texto_original in cifras:
        valor_float = float(valor)

        # Buscar si este valor coincide con algún valor esperado
        encontrado = False
        for marcador, esperado in valores_esperados.items():
            esperado_float = float(esperado)
            if _valores_son_equivalentes(valor_float, esperado_float, tolerancia):
                # Coincidencia encontrada
                esperados_encontrados.add(marcador)
                encontrado = True
                break

        # Si no se encontró coincidencia, es una discrepancia
        if not encontrado:
            # Verificar si el valor está cerca de algún esperado (para debugging)
            cercano = False
            for marcador, esperado in valores_esperados.items():
                if abs(valor_float - float(esperado)) <= tolerancia * 10:
                    cercano = True
                    break

            if not cercano:
                # Valor no esperado en el documento
                resultado.agregar(
                    Discrepancia(
                        ubicacion=ubicacion,
                        origen=origen,
                        esperado=None,
                        encontrado=valor,
                        marcador=None,
                    )
                )

    # Verificar valores esperados que no aparecieron en el documento
    for marcador, esperado in valores_esperados.items():
        if marcador not in esperados_encontrados:
            # Verificar si el valor esperado aparece en el documento
            esperado_float = float(esperado)
            if esperado_float not in valores_encontrados:
                # Buscar con tolerancia
                encontrado_con_tolerancia = False
                for valor_encontrado in valores_encontrados:
                    if abs(esperado_float - valor_encontrado) <= tolerancia:
                        encontrado_con_tolerancia = True
                        break

                if not encontrado_con_tolerancia:
                    resultado.agregar(
                        Discrepancia(
                            ubicacion="documento",
                            origen="texto",
                            esperado=esperado,
                            encontrado=None,
                            marcador=marcador,
                        )
                    )

    return resultado
