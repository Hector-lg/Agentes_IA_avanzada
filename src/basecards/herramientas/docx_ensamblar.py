"""Ensamblado determinista del memo de riesgo en ``.docx`` (RC-05).

La herramienta recibe el JSON que produce el Copiloto de análisis
(``nivel_riesgo``, ``evidencia``, ``drivers``, ``accion_sugerida`` y
``borrador_memo``), abre la plantilla institucional y arma el memo con sus
estilos. El texto narrativo se sustituye con los mismos marcadores
``{{m.<metrica>.<cohorte>.<MOBn>|<formato>}}`` que usa la presentación.

Este módulo no calcula métricas ni redacta: valida el JSON, formatea las cifras
y coloca el contenido. La verificación de cifras del archivo final la hace
``documento_verificar_cifras`` (RB-06).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from docx.table import Table
from docx.text.paragraph import Paragraph

from basecards.herramientas.marcadores import PATRON_MARCADOR, MarcadorError
from basecards.herramientas.marcadores import formatear_valor as _formatear_valor
from basecards.herramientas.marcadores import sustituir_marcadores as _sustituir_marcadores

ROOT = Path(__file__).resolve().parents[3]
BRANDING_RUTA = ROOT / "config" / "branding" / "banamex.yaml"
PLANTILLA_FALLBACK = ROOT / "config" / "branding" / "template_banamex.docx"

ESTILOS_REQUERIDOS = ("Normal", "Title", "Subtitle", "Heading 1", "List Bullet", "Table Grid")

PATRON_TITULO = re.compile(r"^(#{1,6})\s+(.*)$")
PATRON_VINETA = re.compile(r"^[-*•]\s+(.*)$")
PATRON_NUMERO = re.compile(r"^\d+[.)]\s+(.*)$")
PATRON_CITA = re.compile(r"^>\s+(.*)$")
PATRON_SEPARADOR = re.compile(r"^[-*_]{3,}$")
# Solo ``**negritas**`` y ``*cursivas*``. No se interpreta ``_`` porque los
# nombres de columnas y métricas (``SCORE_ORD``, ``cubo_metricas_cohorte``) lo
# usan y perderían sus guiones bajos.
PATRON_FORMATO_INLINE = re.compile(r"(\*\*.+?\*\*|\*.+?\*)")


class DocxError(Exception):
    """Error de ensamblado con un código estable ``E-DOC-*``."""

    def __init__(self, codigo: str, mensaje: str, ruta: str = "") -> None:
        super().__init__(f"{codigo}: {mensaje}")
        self.codigo = codigo
        self.mensaje = mensaje
        self.ruta = ruta


# ---------------------------------------------------------------------------
# Carga de branding y plantilla
# ---------------------------------------------------------------------------


def _cargar_branding() -> dict[str, Any]:
    if not BRANDING_RUTA.is_file():
        raise DocxError("E-DOC-002", f"falta el branding {BRANDING_RUTA}", "branding")
    try:
        datos = yaml.safe_load(BRANDING_RUTA.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise DocxError("E-DOC-002", f"no se pudo leer el branding: {exc}", "branding") from exc
    if not isinstance(datos, dict):
        raise DocxError("E-DOC-002", "el branding debe ser un mapa YAML", "branding")
    if "documento" not in datos:
        raise DocxError("E-DOC-002", "el branding no declara la sección 'documento'", "branding")
    return datos


def _resolver_plantilla(plantilla: Path | str | None) -> Path:
    if plantilla is not None:
        ruta = Path(plantilla).expanduser()
        if not ruta.is_file():
            raise DocxError("E-DOC-002", f"no existe la plantilla {ruta}", "plantilla")
        return ruta.resolve()
    if not PLANTILLA_FALLBACK.is_file():
        raise DocxError(
            "E-DOC-002",
            f"falta la plantilla base {PLANTILLA_FALLBACK}; ejecuta config/branding/generar_template.py",
            "plantilla",
        )
    return PLANTILLA_FALLBACK


def _abrir_plantilla(ruta: Path) -> Document:
    try:
        doc = Document(str(ruta))
    except Exception as exc:  # python-docx lanza distintos tipos según el daño
        raise DocxError(
            "E-DOC-002", f"no se pudo abrir la plantilla {ruta}: {exc}", "plantilla"
        ) from exc
    faltantes = [nombre for nombre in ESTILOS_REQUERIDOS if nombre not in doc.styles]
    if faltantes:
        raise DocxError(
            "E-DOC-003",
            f"la plantilla {ruta} no tiene los estilos {faltantes}",
            "plantilla",
        )
    return doc


def _limpiar_cuerpo(doc: Document) -> None:
    cuerpo = doc.element.body
    for hijo in list(cuerpo):
        if hijo.tag == qn("w:sectPr"):
            continue
        cuerpo.remove(hijo)


def _configurar_pagina(doc: Document, branding: Mapping[str, Any]) -> None:
    documento = branding["documento"]
    seccion = doc.sections[0]
    seccion.page_width = Inches(documento["pagina"]["ancho_pulgadas"])
    seccion.page_height = Inches(documento["pagina"]["alto_pulgadas"])
    margenes = documento["margenes_pulgadas"]
    seccion.left_margin = Inches(margenes["izquierda"])
    seccion.right_margin = Inches(margenes["derecha"])
    seccion.top_margin = Inches(margenes["superior"])
    seccion.bottom_margin = Inches(margenes["inferior"])


# ---------------------------------------------------------------------------
# Marcadores, colores y utilidades de escritura
# ---------------------------------------------------------------------------


def _sustituir(texto: str, metricas: Mapping[str, float | int], campo: str) -> str:
    try:
        return _sustituir_marcadores(texto, metricas)
    except MarcadorError as exc:
        raise DocxError("E-DOC-030", f"{campo}: {exc.mensaje}", campo) from exc


def _formatear(valor: float, formato: str, campo: str) -> str:
    try:
        return _formatear_valor(valor, formato)
    except MarcadorError as exc:
        raise DocxError("E-DOC-030", f"{campo}: {exc.mensaje}", campo) from exc


def _color(branding: Mapping[str, Any], nombre: str) -> RGBColor:
    paleta = branding.get("paleta", {})
    valor = paleta.get(nombre)
    if not isinstance(valor, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", valor):
        raise DocxError("E-DOC-002", f"color inválido {nombre!r} en el branding", "branding")
    return RGBColor.from_string(valor.lstrip("#").upper())


def _familia(branding: Mapping[str, Any]) -> str:
    return str(branding.get("tipografia", {}).get("familia", "Arial"))


def _tamano(branding: Mapping[str, Any], clave: str, defecto: float) -> float:
    tipos = branding.get("documento", {}).get("tipografia", {})
    valor = tipos.get(clave, defecto)
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        raise DocxError("E-DOC-002", f"tamaño inválido {clave!r} en el branding", "branding")
    return float(valor)


def _aplicar_fuente(
    run: Any,
    branding: Mapping[str, Any],
    tamano: float,
    color: RGBColor,
    *,
    negrita: bool = False,
    cursiva: bool = False,
) -> None:
    run.font.name = _familia(branding)
    run.font.size = Pt(tamano)
    run.font.color.rgb = color
    run.font.bold = negrita
    run.font.italic = cursiva
    rpr = run._r.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    for atributo in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        rfonts.set(qn(atributo), _familia(branding))


def _agregar_runs(
    parrafo: Paragraph,
    texto: str,
    branding: Mapping[str, Any],
    tamano: float,
    color: RGBColor,
    *,
    negrita: bool = False,
    cursiva: bool = False,
) -> None:
    """Agrega texto con negritas y cursivas markdown sencillas."""
    posicion = 0
    for coincidencia in PATRON_FORMATO_INLINE.finditer(texto):
        if coincidencia.start() > posicion:
            run = parrafo.add_run(texto[posicion : coincidencia.start()])
            _aplicar_fuente(run, branding, tamano, color, negrita=negrita, cursiva=cursiva)
        token = coincidencia.group(0)
        if token.startswith("**"):
            contenido = token[2:-2]
            run = parrafo.add_run(contenido)
            _aplicar_fuente(run, branding, tamano, color, negrita=True, cursiva=cursiva)
        else:
            contenido = token[1:-1]
            run = parrafo.add_run(contenido)
            _aplicar_fuente(run, branding, tamano, color, negrita=negrita, cursiva=True)
        posicion = coincidencia.end()
    if posicion < len(texto):
        run = parrafo.add_run(texto[posicion:])
        _aplicar_fuente(run, branding, tamano, color, negrita=negrita, cursiva=cursiva)


def _parrafo(
    doc: Document,
    texto: str,
    estilo: str,
    branding: Mapping[str, Any],
    *,
    tamano: float | None = None,
    color: RGBColor | None = None,
    negrita: bool = False,
    cursiva: bool = False,
    alineacion: Any = None,
) -> Paragraph:
    parrafo = doc.add_paragraph(style=estilo)
    if alineacion is not None:
        parrafo.alignment = alineacion
    _agregar_runs(
        parrafo,
        texto,
        branding,
        tamano or _tamano(branding, "cuerpo", 11),
        color or _color(branding, "texto"),
        negrita=negrita,
        cursiva=cursiva,
    )
    return parrafo


def _sombra(celda: Any, color: RGBColor) -> None:
    tcpr = celda._tc.get_or_add_tcPr()
    for anterior in tcpr.findall(qn("w:shd")):
        tcpr.remove(anterior)
    sombra = OxmlElement("w:shd")
    sombra.set(qn("w:val"), "clear")
    sombra.set(qn("w:color"), "auto")
    sombra.set(qn("w:fill"), str(color))
    tcpr.insert_element_before(
        sombra,
        "w:noWrap",
        "w:tcMar",
        "w:textDirection",
        "w:tcFitText",
        "w:vAlign",
        "w:hideMark",
        "w:headers",
        "w:cellIns",
        "w:cellDel",
        "w:cellMerge",
        "w:tcPrChange",
    )


def _sin_bordes(tabla: Table) -> None:
    tblpr = tabla._tbl.tblPr
    for anterior in tblpr.findall(qn("w:tblBorders")):
        tblpr.remove(anterior)
    bordes = OxmlElement("w:tblBorders")
    for lado in ("top", "left", "bottom", "right", "insideH", "insideV"):
        borde = OxmlElement(f"w:{lado}")
        borde.set(qn("w:val"), "nil")
        bordes.append(borde)
    tblpr.insert_element_before(
        bordes,
        "w:shd",
        "w:tblLayout",
        "w:tblCellMar",
        "w:tblLook",
        "w:tblCaption",
        "w:tblDescription",
        "w:tblPrChange",
    )


def _borde_izquierdo(celda: Any, color: RGBColor, grosor: int = 18) -> None:
    tcpr = celda._tc.get_or_add_tcPr()
    bordes = OxmlElement("w:tcBorders")
    izquierdo = OxmlElement("w:left")
    izquierdo.set(qn("w:val"), "single")
    izquierdo.set(qn("w:sz"), str(grosor))
    izquierdo.set(qn("w:space"), "0")
    izquierdo.set(qn("w:color"), str(color))
    bordes.append(izquierdo)
    tcpr.insert_element_before(
        bordes,
        "w:shd",
        "w:noWrap",
        "w:tcMar",
        "w:textDirection",
        "w:tcFitText",
        "w:vAlign",
        "w:hideMark",
        "w:headers",
        "w:cellIns",
        "w:cellDel",
        "w:cellMerge",
        "w:tcPrChange",
    )


def _ajustar_anchos(tabla: Table, anchos: Sequence[float]) -> None:
    for indice, ancho in enumerate(anchos):
        tabla.columns[indice].width = Inches(ancho)
        for celda in tabla.columns[indice].cells:
            celda.width = Inches(ancho)


def _celda(
    celda: Any,
    texto: str,
    branding: Mapping[str, Any],
    tamano: float,
    color: RGBColor,
    *,
    negrita: bool = False,
    cursiva: bool = False,
    alineacion: Any = None,
    relleno: RGBColor | None = None,
) -> None:
    celda.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    if relleno is not None:
        _sombra(celda, relleno)
    parrafo = celda.paragraphs[0]
    parrafo.text = ""
    if alineacion is not None:
        parrafo.alignment = alineacion
    parrafo.paragraph_format.space_before = Pt(2)
    parrafo.paragraph_format.space_after = Pt(2)
    _agregar_runs(
        parrafo,
        texto,
        branding,
        tamano,
        color,
        negrita=negrita,
        cursiva=cursiva,
    )


def _tabla(
    doc: Document,
    filas: int,
    columnas: int,
    *,
    estilo: str = "Table Grid",
) -> Table:
    tabla = doc.add_table(rows=filas, cols=columnas)
    tabla.style = doc.styles[estilo]
    tabla.alignment = WD_TABLE_ALIGNMENT.CENTER
    tabla.autofit = False
    return tabla


def _espacio(doc: Document, puntos: float = 6) -> None:
    parrafo = doc.add_paragraph()
    parrafo.paragraph_format.space_before = Pt(0)
    parrafo.paragraph_format.space_after = Pt(puntos)
    parrafo.paragraph_format.line_spacing = 1


def _linea_horizontal(doc: Document, branding: Mapping[str, Any]) -> None:
    parrafo = doc.add_paragraph()
    ppr = parrafo._p.get_or_add_pPr()
    bordes = OxmlElement("w:pBdr")
    inferior = OxmlElement("w:bottom")
    inferior.set(qn("w:val"), "single")
    inferior.set(qn("w:sz"), "6")
    inferior.set(qn("w:space"), "1")
    inferior.set(qn("w:color"), str(_color(branding, "neutro_claro")))
    bordes.append(inferior)
    ppr.append(bordes)
    parrafo.paragraph_format.space_before = Pt(2)
    parrafo.paragraph_format.space_after = Pt(6)


# ---------------------------------------------------------------------------
# Lectura y validación del JSON del Copiloto
# ---------------------------------------------------------------------------


def _texto_obligatorio(valor: Any, campo: str) -> str:
    if not isinstance(valor, str) or not valor.strip():
        raise DocxError("E-DOC-020", f"{campo} debe ser texto no vacío")
    return valor.strip()


def _texto_opcional(valor: Any, campo: str) -> str | None:
    if valor is None:
        return None
    if isinstance(valor, date):
        return valor.strftime("%d/%m/%Y")
    if isinstance(valor, bool) or not isinstance(valor, (str, int, float)):
        raise DocxError("E-DOC-020", f"{campo} debe ser texto")
    texto = str(valor).strip()
    return texto or None


def _numero_opcional(
    valor: Any,
    campo: str,
    *,
    minimo: float | None = None,
    maximo: float | None = None,
) -> float | None:
    if valor is None:
        return None
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        raise DocxError("E-DOC-020", f"{campo} debe ser numérico")
    numero = float(valor)
    if minimo is not None and numero < minimo:
        raise DocxError("E-DOC-020", f"{campo} debe ser mayor o igual a {minimo}")
    if maximo is not None and numero > maximo:
        raise DocxError("E-DOC-020", f"{campo} debe ser menor o igual a {maximo}")
    return numero


def _leer_memo(analisis: Mapping[str, Any]) -> str:
    memo = analisis.get("borrador_memo")
    if isinstance(memo, Sequence) and not isinstance(memo, (str, bytes)):
        memo = "\n\n".join(str(parte) for parte in memo)
    return _texto_obligatorio(memo, "analisis.borrador_memo")


def _corte_desde_valor(valor: Any) -> str | None:
    """Extrae el corte de un escalar, una cohorte o una lista de cohortes."""
    if isinstance(valor, Mapping):
        for clave in ("VINTAGE", "vintage", "corte", "CORTE", "cohorte"):
            if clave in valor:
                return _texto_opcional(valor[clave], "analisis.metadatos.corte")
        return None
    if isinstance(valor, Sequence) and not isinstance(valor, (str, bytes)):
        for item in valor:
            resultado = _corte_desde_valor(item)
            if resultado is not None:
                return resultado
        return None
    return _texto_opcional(valor, "analisis.metadatos.corte")


def _leer_metadatos(analisis: Mapping[str, Any], corte: str | None) -> dict[str, str]:
    datos: dict[str, Any] = {}
    bruto = analisis.get("metadatos")
    if bruto is not None:
        if not isinstance(bruto, Mapping):
            raise DocxError("E-DOC-020", "analisis.metadatos debe ser un mapa")
        datos.update(bruto)
    for clave in ("titulo", "para", "de", "fecha", "asunto", "corte", "cohorte"):
        if clave in analisis and analisis[clave] is not None:
            datos[clave] = analisis[clave]

    metadatos: dict[str, str] = {}
    for clave in ("titulo", "para", "de", "fecha", "asunto"):
        valor = _texto_opcional(datos.get(clave), f"analisis.metadatos.{clave}")
        if valor is not None:
            metadatos[clave] = valor

    corte_final = _corte_desde_valor(datos.get("corte"))
    if corte_final is None:
        corte_final = _corte_desde_valor(datos.get("cohorte"))
    if corte_final is None and corte is not None:
        corte_final = _corte_desde_valor(corte)
    if corte_final is not None:
        metadatos["corte"] = corte_final
    if "fecha" not in metadatos:
        metadatos["fecha"] = date.today().strftime("%d/%m/%Y")
    if "para" not in metadatos:
        metadatos["para"] = "Analista de riesgo"
    if "de" not in metadatos:
        metadatos["de"] = "Copiloto de análisis"
    if "titulo" not in metadatos:
        metadatos["titulo"] = "Memo de riesgo"
    if "asunto" not in metadatos:
        metadatos["asunto"] = metadatos["titulo"]
    return metadatos


def _inferir_corte(metricas: Mapping[str, float | int]) -> str | None:
    for clave in metricas:
        coincidencia = PATRON_MARCADOR.fullmatch(str(clave))
        if coincidencia is not None and coincidencia.group("cohorte") != "todas":
            return coincidencia.group("cohorte")
    return None


def _leer_evidencia(analisis: Mapping[str, Any]) -> list[Any]:
    evidencia = analisis.get("evidencia")
    if evidencia is None:
        return []
    if not isinstance(evidencia, Sequence) or isinstance(evidencia, (str, bytes)):
        raise DocxError("E-DOC-040", "analisis.evidencia debe ser una lista")
    for indice, item in enumerate(evidencia):
        if not isinstance(item, (Mapping, str)):
            raise DocxError(
                "E-DOC-040",
                f"analisis.evidencia[{indice}] debe ser un mapa o texto",
            )
    return list(evidencia)


def _leer_drivers(analisis: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    drivers = analisis.get("drivers")
    if drivers is None:
        return []
    if not isinstance(drivers, Sequence) or isinstance(drivers, (str, bytes)):
        raise DocxError("E-DOC-040", "analisis.drivers debe ser una lista")
    resultado: list[Mapping[str, Any]] = []
    for indice, item in enumerate(drivers):
        if not isinstance(item, Mapping):
            raise DocxError("E-DOC-040", f"analisis.drivers[{indice}] debe ser un mapa")
        if "feature" not in item and "driver" not in item:
            raise DocxError("E-DOC-040", f"analisis.drivers[{indice}] necesita 'feature'")
        resultado.append(item)
    return resultado


# ---------------------------------------------------------------------------
# Secciones del documento
# ---------------------------------------------------------------------------


def _encabezado(
    doc: Document,
    branding: Mapping[str, Any],
    metadatos: Mapping[str, str],
    metricas: Mapping[str, float | int],
) -> None:
    titulo = _sustituir(metadatos["titulo"], metricas, "título")
    parrafo = doc.add_paragraph(style="Title")
    _agregar_runs(
        parrafo,
        titulo,
        branding,
        _tamano(branding, "titulo", 20),
        _color(branding, "primario"),
        negrita=True,
    )

    partes = []
    if "corte" in metadatos:
        partes.append(f"Corte {metadatos['corte']}")
    if "fecha" in metadatos:
        partes.append(metadatos["fecha"])
    if partes:
        subtitulo = doc.add_paragraph(style="Subtitle")
        _agregar_runs(
            subtitulo,
            " · ".join(partes),
            branding,
            _tamano(branding, "subtitulo", 12),
            _color(branding, "texto"),
            cursiva=True,
        )


def _tabla_metadatos(
    doc: Document,
    branding: Mapping[str, Any],
    metadatos: Mapping[str, str],
    metricas: Mapping[str, float | int],
) -> None:
    filas = [
        ("Para", metadatos["para"]),
        ("De", metadatos["de"]),
        ("Fecha", metadatos["fecha"]),
        ("Asunto", metadatos["asunto"]),
    ]
    tabla = _tabla(doc, len(filas), 2, estilo="Table Grid")
    _sin_bordes(tabla)
    _ajustar_anchos(tabla, (1.1, 5.6))
    for indice, (etiqueta, valor) in enumerate(filas):
        _celda(
            tabla.cell(indice, 0),
            etiqueta,
            branding,
            _tamano(branding, "tabla", 9.5),
            _color(branding, "primario"),
            negrita=True,
        )
        _celda(
            tabla.cell(indice, 1),
            _sustituir(valor, metricas, f"metadatos.{etiqueta.lower()}"),
            branding,
            _tamano(branding, "tabla", 9.5),
            _color(branding, "texto"),
        )
    _espacio(doc, 4)


def _color_riesgo(branding: Mapping[str, Any], nivel: str) -> RGBColor:
    normalizado = nivel.strip().lower()
    if normalizado in {"bajo", "baja"}:
        return _color(branding, "positivo")
    if normalizado in {"medio", "media"}:
        return _color(branding, "acento")
    if normalizado in {"alto", "alta", "critico", "crítico", "critica", "crítica"}:
        return _color(branding, "negativo")
    return _color(branding, "primario")


def _valor_rentabilidad(
    analisis: Mapping[str, Any],
    metricas: Mapping[str, float | int],
) -> str:
    valor = analisis.get("rentabilidad_esperada")
    if valor is None:
        return "—"
    if isinstance(valor, bool) or not isinstance(valor, (int, float, str)):
        raise DocxError("E-DOC-020", "analisis.rentabilidad_esperada debe ser numérico o texto")
    if isinstance(valor, str):
        return _sustituir(valor, metricas, "rentabilidad_esperada")
    return _formatear(float(valor), "pct2", "rentabilidad_esperada")


def _valor_confianza(
    analisis: Mapping[str, Any],
    branding: Mapping[str, Any],
) -> str:
    valor = _numero_opcional(
        analisis.get("confianza"), "analisis.confianza", minimo=0.0, maximo=1.0
    )
    if valor is None:
        return "—"
    return _formatear(valor, "pct1", "confianza")


def _tabla_resumen(
    doc: Document,
    branding: Mapping[str, Any],
    analisis: Mapping[str, Any],
    nivel: str,
    metricas: Mapping[str, float | int],
) -> None:
    encabezados = ("Nivel de riesgo", "Rentabilidad esperada", "Confianza", "Acción sugerida")
    valores = (
        nivel.strip().capitalize(),
        _valor_rentabilidad(analisis, metricas),
        _valor_confianza(analisis, branding),
        _texto_opcional(analisis.get("accion_sugerida"), "analisis.accion_sugerida") or "—",
    )
    tabla = _tabla(doc, 2, 4)
    _ajustar_anchos(tabla, (1.75, 1.75, 1.35, 1.85))
    for indice, encabezado in enumerate(encabezados):
        _celda(
            tabla.cell(0, indice),
            encabezado,
            branding,
            _tamano(branding, "tabla", 8.5),
            _color(branding, "fondo"),
            negrita=True,
            alineacion=WD_ALIGN_PARAGRAPH.CENTER,
            relleno=_color(branding, "primario"),
        )
    for indice, valor in enumerate(valores):
        relleno = _color(branding, "neutro_claro") if indice > 0 else _color_riesgo(branding, nivel)
        color = _color(branding, "fondo") if indice == 0 else _color(branding, "texto")
        _celda(
            tabla.cell(1, indice),
            valor,
            branding,
            _tamano(branding, "tabla", 10.5) if indice == 0 else _tamano(branding, "tabla", 9.5),
            color,
            negrita=indice == 0,
            alineacion=WD_ALIGN_PARAGRAPH.CENTER,
            relleno=relleno,
        )
    _espacio(doc, 8)


def _texto_evidencia(
    item: Any, metricas: Mapping[str, float | int], indice: int
) -> tuple[str, str, str]:
    if isinstance(item, str):
        return ("—", _sustituir(item, metricas, f"evidencia[{indice}]"), "—")
    herramienta = (
        _texto_opcional(item.get("herramienta"), f"evidencia[{indice}].herramienta") or "—"
    )
    llamada = _texto_opcional(item.get("llamada_id"), f"evidencia[{indice}].llamada_id") or "—"
    hallazgo = item.get("hallazgo", item.get("descripcion", item.get("cifra")))
    if hallazgo is None or isinstance(hallazgo, bool):
        raise DocxError("E-DOC-040", f"analisis.evidencia[{indice}] no tiene hallazgo")
    texto = str(hallazgo).strip()
    if not texto:
        raise DocxError("E-DOC-040", f"analisis.evidencia[{indice}] no tiene hallazgo")
    return (
        _sustituir(herramienta, metricas, f"evidencia[{indice}].herramienta"),
        _sustituir(texto, metricas, f"evidencia[{indice}].hallazgo"),
        _sustituir(llamada, metricas, f"evidencia[{indice}].llamada_id"),
    )


def _tabla_evidencia(
    doc: Document,
    branding: Mapping[str, Any],
    evidencia: Sequence[Any],
    metricas: Mapping[str, float | int],
) -> None:
    if not evidencia:
        return
    _parrafo(
        doc,
        "Evidencia",
        "Heading 1",
        branding,
        tamano=14,
        color=_color(branding, "primario"),
        negrita=True,
    )
    tabla = _tabla(doc, len(evidencia) + 1, 3)
    _ajustar_anchos(tabla, (1.45, 3.55, 1.7))
    for indice, encabezado in enumerate(("Herramienta", "Evidencia", "Llamada")):
        _celda(
            tabla.cell(0, indice),
            encabezado,
            branding,
            _tamano(branding, "tabla", 8.5),
            _color(branding, "fondo"),
            negrita=True,
            relleno=_color(branding, "primario"),
        )
    for indice, item in enumerate(evidencia, start=1):
        herramienta, hallazgo, llamada = _texto_evidencia(item, metricas, indice - 1)
        relleno = _color(branding, "neutro_claro") if indice % 2 == 1 else None
        for columna, valor in enumerate((herramienta, hallazgo, llamada)):
            _celda(
                tabla.cell(indice, columna),
                valor,
                branding,
                _tamano(branding, "tabla", 9),
                _color(branding, "texto"),
                relleno=relleno,
            )
    _espacio(doc, 8)


def _contribucion(valor: Any, metricas: Mapping[str, float | int], indice: int) -> str:
    if valor is None:
        return "—"
    if isinstance(valor, bool):
        raise DocxError("E-DOC-040", f"drivers[{indice}].contribucion debe ser numérico")
    if isinstance(valor, (int, float)):
        return f"{float(valor) * 100:+.2f}%"
    if isinstance(valor, str):
        return _sustituir(valor, metricas, f"drivers[{indice}].contribucion")
    raise DocxError("E-DOC-040", f"drivers[{indice}].contribucion debe ser numérico o texto")


def _tabla_drivers(
    doc: Document,
    branding: Mapping[str, Any],
    drivers: Sequence[Mapping[str, Any]],
    metricas: Mapping[str, float | int],
) -> None:
    if not drivers:
        return
    _parrafo(
        doc,
        "Drivers",
        "Heading 1",
        branding,
        tamano=14,
        color=_color(branding, "primario"),
        negrita=True,
    )
    tabla = _tabla(doc, len(drivers) + 1, 2)
    _ajustar_anchos(tabla, (4.9, 1.8))
    for indice, encabezado in enumerate(("Driver", "Contribución")):
        _celda(
            tabla.cell(0, indice),
            encabezado,
            branding,
            _tamano(branding, "tabla", 8.5),
            _color(branding, "fondo"),
            negrita=True,
            relleno=_color(branding, "primario"),
        )
    for indice, item in enumerate(drivers, start=1):
        driver = _texto_obligatorio(
            item.get("feature", item.get("driver")),
            f"analisis.drivers[{indice - 1}].feature",
        )
        relleno = _color(branding, "neutro_claro") if indice % 2 == 1 else None
        _celda(
            tabla.cell(indice, 0),
            _sustituir(driver, metricas, f"drivers[{indice - 1}].feature"),
            branding,
            _tamano(branding, "tabla", 9),
            _color(branding, "texto"),
            relleno=relleno,
        )
        _celda(
            tabla.cell(indice, 1),
            _contribucion(item.get("contribucion"), metricas, indice - 1),
            branding,
            _tamano(branding, "tabla", 9),
            _color(branding, "texto"),
            alineacion=WD_ALIGN_PARAGRAPH.RIGHT,
            relleno=relleno,
        )
    _espacio(doc, 8)


def _cuerpo_memo(
    doc: Document,
    branding: Mapping[str, Any],
    memo: str,
    metricas: Mapping[str, float | int],
) -> None:
    _parrafo(
        doc,
        "Análisis",
        "Heading 1",
        branding,
        tamano=14,
        color=_color(branding, "primario"),
        negrita=True,
    )
    tamano = _tamano(branding, "cuerpo", 11)
    for numero, linea in enumerate(memo.splitlines(), start=1):
        limpia = linea.rstrip()
        if not limpia.strip():
            continue
        campo = f"borrador_memo.línea {numero}"

        titulo = PATRON_TITULO.match(limpia.strip())
        if titulo is not None:
            nivel = len(titulo.group(1))
            estilo = "Heading 1" if nivel <= 2 else "Heading 2" if nivel <= 4 else "Heading 3"
            texto = _sustituir(titulo.group(2).strip(), metricas, campo)
            _parrafo(
                doc,
                texto,
                estilo,
                branding,
                tamano=14 if estilo == "Heading 1" else 12,
                color=_color(branding, "primario"),
                negrita=True,
            )
            continue

        if PATRON_SEPARADOR.match(limpia.strip()):
            _linea_horizontal(doc, branding)
            continue

        vineta = PATRON_VINETA.match(limpia.strip())
        if vineta is not None:
            texto = _sustituir(vineta.group(1).strip(), metricas, campo)
            _parrafo(doc, texto, "List Bullet", branding, tamano=tamano)
            continue

        numerada = PATRON_NUMERO.match(limpia.strip())
        if numerada is not None:
            texto = _sustituir(numerada.group(1).strip(), metricas, campo)
            _parrafo(doc, texto, "List Number", branding, tamano=tamano)
            continue

        cita = PATRON_CITA.match(limpia.strip())
        if cita is not None:
            texto = _sustituir(cita.group(1).strip(), metricas, campo)
            parrafo = _parrafo(doc, texto, "Normal", branding, tamano=tamano, cursiva=True)
            parrafo.paragraph_format.left_indent = Inches(0.3)
            continue

        texto = _sustituir(limpia.strip(), metricas, campo)
        _parrafo(
            doc, texto, "Normal", branding, tamano=tamano, alineacion=WD_ALIGN_PARAGRAPH.JUSTIFY
        )


def _callout_accion(
    doc: Document,
    branding: Mapping[str, Any],
    analisis: Mapping[str, Any],
    metricas: Mapping[str, float | int],
) -> None:
    accion = _texto_opcional(analisis.get("accion_sugerida"), "analisis.accion_sugerida")
    revision = analisis.get("requiere_revision_humana", False)
    if not isinstance(revision, bool):
        raise DocxError("E-DOC-020", "analisis.requiere_revision_humana debe ser booleano")
    if accion is None and not revision:
        return

    _parrafo(
        doc,
        "Acción sugerida",
        "Heading 1",
        branding,
        tamano=14,
        color=_color(branding, "primario"),
        negrita=True,
    )
    if accion is not None:
        tabla = _tabla(doc, 1, 1, estilo="Table Grid")
        _sin_bordes(tabla)
        _ajustar_anchos(tabla, (6.7,))
        celda = tabla.cell(0, 0)
        _sombra(celda, _color(branding, "neutro_claro"))
        _borde_izquierdo(celda, _color(branding, "acento"), grosor=24)
        _celda(
            celda,
            _sustituir(accion, metricas, "accion_sugerida"),
            branding,
            _tamano(branding, "cuerpo", 11),
            _color(branding, "texto"),
            negrita=True,
        )
    if revision:
        _espacio(doc, 2)
        _parrafo(
            doc,
            "Requiere revisión humana antes de distribuirse.",
            "Normal",
            branding,
            tamano=10,
            color=_color(branding, "negativo"),
            negrita=True,
        )
    _espacio(doc, 6)


def _nota_generacion(doc: Document, branding: Mapping[str, Any]) -> None:
    _linea_horizontal(doc, branding)
    _parrafo(
        doc,
        "Documento generado por Base Cards a partir del JSON del Copiloto. "
        "Las cifras provienen de las herramientas citadas en la evidencia.",
        "Normal",
        branding,
        tamano=_tamano(branding, "pie", 8),
        color=_color(branding, "texto"),
        cursiva=True,
    )


# ---------------------------------------------------------------------------
# Barrido final de marcadores
# ---------------------------------------------------------------------------


def _iter_tabla(tabla: Table):
    for fila in tabla.rows:
        for celda in fila.cells:
            yield from celda.paragraphs
            for anidada in celda.tables:
                yield from _iter_tabla(anidada)


def _iter_parrafos(doc: Document):
    yield from doc.paragraphs
    for tabla in doc.tables:
        yield from _iter_tabla(tabla)
    for seccion in doc.sections:
        for contenedor in (seccion.header, seccion.footer):
            yield from contenedor.paragraphs
            for tabla in contenedor.tables:
                yield from _iter_tabla(tabla)


def _sustituir_parrafo(
    parrafo: Paragraph,
    metricas: Mapping[str, float | int],
    campo: str,
) -> None:
    if "{{m." not in parrafo.text:
        return
    for run in parrafo.runs:
        if "{{m." in run.text:
            run.text = _sustituir(run.text, metricas, campo)
    if "{{m." in parrafo.text:
        texto = _sustituir(parrafo.text, metricas, campo)
        for run in list(parrafo.runs):
            run._r.getparent().remove(run._r)
        parrafo.add_run(texto)


def _sustituir_en_documento(doc: Document, metricas: Mapping[str, float | int]) -> None:
    for parrafo in _iter_parrafos(doc):
        _sustituir_parrafo(parrafo, metricas, "documento")


def _verificar_sin_marcadores(doc: Document) -> None:
    restos = [parrafo.text for parrafo in _iter_parrafos(doc) if "{{m." in parrafo.text]
    if restos:
        raise DocxError(
            "E-DOC-030",
            f"quedaron {len(restos)} marcadores sin sustituir: {restos[:3]}",
        )


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def docx_ensamblar(
    salida: Path | str,
    *,
    analisis: Mapping[str, Any],
    metricas: Mapping[str, float | int] | None = None,
    plantilla: Path | str | None = None,
    corte: str | None = None,
) -> Path:
    """Ensambla el memo de riesgo RC-05 y devuelve la ruta del archivo.

    Parameters
    ----------
    salida:
        Ruta del ``.docx`` a generar.
    analisis:
        JSON del Copiloto de análisis. Claves usadas: ``borrador_memo`` (texto
        markdown sencillo), ``nivel_riesgo``, ``rentabilidad_esperada``,
        ``confianza``, ``evidencia``, ``drivers``, ``accion_sugerida``,
        ``requiere_revision_humana`` y, opcionalmente, ``titulo``, ``para``,
        ``de``, ``fecha``, ``asunto``, ``corte`` y ``metadatos``.
    metricas:
        Diccionario ``{{m.<metrica>.<cohorte>.<MOBn>|<formato>}}`` → valor.
    plantilla:
        Plantilla explícita. Si se omite, se usa
        ``config/branding/template_banamex.docx``.
    corte:
        Corte en formato ``AAAAMM``. Si se omite, se busca en ``metadatos`` o
        en las claves de ``metricas``.

    Returns
    -------
    Path
        Ruta absoluta del archivo generado.
    """
    destino = Path(salida).expanduser()
    if destino.suffix.lower() != ".docx":
        raise DocxError("E-DOC-050", f"la salida debe terminar en .docx: {destino}", "salida")
    destino = destino.resolve()
    if not isinstance(analisis, Mapping):
        raise DocxError("E-DOC-010", "analisis debe ser un mapa con el JSON del Copiloto")
    metricas = metricas or {}
    if not isinstance(metricas, Mapping):
        raise DocxError("E-DOC-030", "metricas debe ser un mapa de marcador a valor")

    memo = _leer_memo(analisis)
    nivel = _texto_obligatorio(analisis.get("nivel_riesgo"), "analisis.nivel_riesgo")
    if corte is None:
        corte = _inferir_corte(metricas)
    metadatos = _leer_metadatos(analisis, corte)
    evidencia = _leer_evidencia(analisis)
    drivers = _leer_drivers(analisis)

    branding = _cargar_branding()
    ruta_plantilla = _resolver_plantilla(plantilla)
    doc = _abrir_plantilla(ruta_plantilla)
    _limpiar_cuerpo(doc)
    _configurar_pagina(doc, branding)

    _encabezado(doc, branding, metadatos, metricas)
    _tabla_resumen(doc, branding, analisis, nivel, metricas)
    _tabla_metadatos(doc, branding, metadatos, metricas)
    _cuerpo_memo(doc, branding, memo, metricas)
    _tabla_evidencia(doc, branding, evidencia, metricas)
    _tabla_drivers(doc, branding, drivers, metricas)
    _callout_accion(doc, branding, analisis, metricas)
    _nota_generacion(doc, branding)

    _sustituir_en_documento(doc, metricas)
    _verificar_sin_marcadores(doc)

    doc.core_properties.title = _sustituir(metadatos["titulo"], metricas, "título")
    doc.core_properties.author = "basecards"
    doc.core_properties.subject = _sustituir(metadatos["asunto"], metricas, "asunto")
    doc.core_properties.comments = "Generado desde el JSON del Copiloto de análisis"

    destino.parent.mkdir(parents=True, exist_ok=True)
    try:
        doc.save(str(destino))
    except Exception as exc:
        raise DocxError("E-DOC-050", f"no se pudo guardar {destino}: {exc}", "salida") from exc

    try:
        Document(str(destino))
    except Exception as exc:
        raise DocxError(
            "E-DOC-051", f"el archivo generado no se puede abrir con python-docx: {exc}", "salida"
        ) from exc
    return destino
