"""Genera las plantillas Banamex a partir de ``banamex.yaml``.

Uso:
    .\\.venv\\Scripts\\python.exe config\\branding\\generar_template.py

Produce ``template_banamex.pptx`` y ``template_banamex.docx``. El resultado es
la plantilla base compartida por las skills de negocio. Solo se ejecuta cuando
cambia el branding, no en runtime.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from docx import Document
from docx.enum.text import WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn as docx_qn
from docx.shared import Inches as DocxInches
from docx.shared import Pt as DocxPt
from docx.shared import RGBColor
from lxml import etree
from pptx import Presentation
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.oxml import parse_xml
from pptx.oxml.ns import qn
from pptx.util import Inches

AQUI = Path(__file__).resolve().parent

# Layouts del master de PowerPoint que se renombran con los nombres del contrato.
LAYOUTS = {
    0: "Portada",
    1: "Titulo_y_Grafica",
    3: "Dos_Columnas",
    5: "Tabla",
    6: "Cierre",
}

COLORES_TEMA = {
    "dk1": "texto",
    "lt1": "fondo",
    "dk2": "primario",
    "lt2": "neutro_claro",
    "accent1": "primario",
    "accent2": "acento",
    "accent3": "positivo",
    "accent4": "negativo",
}


def _cargar_branding() -> dict:
    with (AQUI / "banamex.yaml").open(encoding="utf-8") as archivo:
        return yaml.safe_load(archivo)


def _escalar_formas(shapes, factor_x: float, factor_y: float) -> None:
    """Ajusta la geometría del master y los layouts al tamaño de la diapositiva.

    Lee las cuatro coordenadas antes de escribir: si un placeholder hereda su
    posición del master, asignar solo una dimensión crea un ``xfrm`` incompleto
    y pierde las demás.
    """
    for forma in shapes:
        izquierda = forma.left
        superior = forma.top
        ancho = forma.width
        alto = forma.height
        if izquierda is None and superior is None and ancho is None and alto is None:
            continue
        if izquierda is not None:
            forma.left = int(izquierda * factor_x)
        if superior is not None:
            forma.top = int(superior * factor_y)
        if ancho is not None:
            forma.width = int(ancho * factor_x)
        if alto is not None:
            forma.height = int(alto * factor_y)


def _pintar_tema(prs: Presentation, branding: dict) -> None:
    tema = prs.slide_masters[0].part.part_related_by(RT.THEME)
    raiz = parse_xml(tema.blob)

    esquema = raiz.find(f"{qn('a:themeElements')}/{qn('a:clrScheme')}")
    for etiqueta, clave in COLORES_TEMA.items():
        nodo = esquema.find(qn(f"a:{etiqueta}"))
        if nodo is None:
            continue
        for hijo in list(nodo):
            nodo.remove(hijo)
        color = etree.SubElement(nodo, qn("a:srgbClr"))
        color.set("val", branding["paleta"][clave].lstrip("#").upper())

    fuentes = raiz.find(f"{qn('a:themeElements')}/{qn('a:fontScheme')}")
    for nombre in ("majorFont", "minorFont"):
        latin = fuentes.find(f"{qn(f'a:{nombre}')}/{qn('a:latin')}")
        latin.set("typeface", branding["tipografia"]["familia"])
        latin.attrib.pop("panose", None)

    tema._blob = etree.tostring(raiz, xml_declaration=True, encoding="UTF-8", standalone=True)


def _docx_color(valor: str) -> RGBColor:
    return RGBColor.from_string(valor.lstrip("#").upper())


def _docx_fuente(
    estilo,
    familia: str,
    tamano: float,
    color: RGBColor,
    *,
    negrita: bool | None = None,
    cursiva: bool | None = None,
) -> None:
    estilo.font.name = familia
    estilo.font.size = DocxPt(tamano)
    estilo.font.color.rgb = color
    if negrita is not None:
        estilo.font.bold = negrita
    if cursiva is not None:
        estilo.font.italic = cursiva
    rpr = estilo.element.get_or_add_rPr()
    rfonts = rpr.find(docx_qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert_element_before(
            rfonts,
            "w:b",
            "w:bCs",
            "w:i",
            "w:iCs",
            "w:caps",
            "w:smallCaps",
            "w:strike",
            "w:dstrike",
            "w:outline",
            "w:shadow",
            "w:emboss",
            "w:imprint",
            "w:noProof",
            "w:snapToGrid",
            "w:vanish",
            "w:webHidden",
            "w:color",
            "w:spacing",
            "w:w",
            "w:kern",
            "w:position",
            "w:sz",
            "w:szCs",
            "w:highlight",
            "w:u",
            "w:effect",
            "w:bdr",
            "w:shd",
            "w:fitText",
            "w:vertAlign",
            "w:rtl",
            "w:cs",
            "w:em",
            "w:lang",
            "w:eastAsianLayout",
            "w:specVanish",
            "w:oMath",
        )
    for atributo in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        rfonts.set(docx_qn(atributo), familia)


def _docx_borde_inferior(parrafo, color: str) -> None:
    ppr = parrafo._p.get_or_add_pPr()
    bordes = OxmlElement("w:pBdr")
    inferior = OxmlElement("w:bottom")
    inferior.set(docx_qn("w:val"), "single")
    inferior.set(docx_qn("w:sz"), "6")
    inferior.set(docx_qn("w:space"), "4")
    inferior.set(docx_qn("w:color"), color.lstrip("#").upper())
    bordes.append(inferior)
    ppr.insert_element_before(
        bordes,
        "w:shd",
        "w:tabs",
        "w:spacing",
        "w:ind",
        "w:contextualSpacing",
        "w:jc",
        "w:textDirection",
        "w:textAlignment",
        "w:outlineLvl",
        "w:rPr",
        "w:sectPr",
        "w:pPrChange",
    )


def _docx_campo(parrafo, instruccion: str, familia: str, tamano: float, color: RGBColor) -> None:
    """Inserta un campo de Word (``PAGE``, ``NUMPAGES``) con el estilo del pie."""
    partes: list = []
    inicio = parrafo.add_run()
    fld = OxmlElement("w:fldChar")
    fld.set(docx_qn("w:fldCharType"), "begin")
    inicio._r.append(fld)
    partes.append(inicio)

    instruccion_run = parrafo.add_run()
    texto = OxmlElement("w:instrText")
    texto.set(docx_qn("xml:space"), "preserve")
    texto.text = f" {instruccion} "
    instruccion_run._r.append(texto)
    partes.append(instruccion_run)

    separador = parrafo.add_run()
    fld = OxmlElement("w:fldChar")
    fld.set(docx_qn("w:fldCharType"), "separate")
    separador._r.append(fld)
    partes.append(separador)

    valor = parrafo.add_run("1")
    partes.append(valor)

    fin = parrafo.add_run()
    fld = OxmlElement("w:fldChar")
    fld.set(docx_qn("w:fldCharType"), "end")
    fin._r.append(fld)
    partes.append(fin)

    for run in partes:
        run.font.name = familia
        run.font.size = DocxPt(tamano)
        run.font.color.rgb = color


def _docx_estilos(doc: Document, branding: dict) -> None:
    documento = branding["documento"]
    paleta = branding["paleta"]
    familia = branding["tipografia"]["familia"]
    tipos = documento["tipografia"]
    texto = _docx_color(paleta["texto"])
    primario = _docx_color(paleta["primario"])

    normal = doc.styles["Normal"]
    _docx_fuente(normal, familia, tipos["cuerpo"], texto)
    normal.paragraph_format.space_after = DocxPt(6)
    normal.paragraph_format.line_spacing = 1.15

    titulo = doc.styles["Title"]
    _docx_fuente(titulo, familia, tipos["titulo"], primario, negrita=True)
    titulo.paragraph_format.space_before = DocxPt(0)
    titulo.paragraph_format.space_after = DocxPt(2)
    titulo.paragraph_format.keep_with_next = True

    subtitulo = doc.styles["Subtitle"]
    _docx_fuente(subtitulo, familia, tipos["subtitulo"], texto, negrita=False, cursiva=True)
    subtitulo.paragraph_format.space_before = DocxPt(0)
    subtitulo.paragraph_format.space_after = DocxPt(14)

    for nombre, tamano in (("Heading 1", 14), ("Heading 2", 12), ("Heading 3", 11)):
        estilo = doc.styles[nombre]
        _docx_fuente(estilo, familia, tamano, primario, negrita=True, cursiva=False)
        estilo.paragraph_format.space_before = DocxPt(12)
        estilo.paragraph_format.space_after = DocxPt(5)
        estilo.paragraph_format.keep_with_next = True

    for nombre in ("List Bullet", "List Number"):
        estilo = doc.styles[nombre]
        _docx_fuente(estilo, familia, tipos["cuerpo"], texto)
        estilo.paragraph_format.space_after = DocxPt(3)
        estilo.paragraph_format.left_indent = DocxInches(0.3)
        estilo.paragraph_format.first_line_indent = DocxInches(-0.18)

    tabla = doc.styles["Table Grid"]
    _docx_fuente(tabla, familia, tipos["tabla"], texto)

    leyenda = doc.styles["Caption"]
    _docx_fuente(leyenda, familia, tipos["pie"], texto, negrita=False, cursiva=True)
    leyenda.paragraph_format.space_before = DocxPt(2)
    leyenda.paragraph_format.space_after = DocxPt(8)


def _docx_encabezado_pie(seccion, branding: dict) -> None:
    documento = branding["documento"]
    paleta = branding["paleta"]
    familia = branding["tipografia"]["familia"]
    tipos = documento["tipografia"]
    primario = _docx_color(paleta["primario"])
    tenue = _docx_color("#667085")
    ancho_util = (
        documento["pagina"]["ancho_pulgadas"]
        - documento["margenes_pulgadas"]["izquierda"]
        - documento["margenes_pulgadas"]["derecha"]
    )

    encabezado = seccion.header.paragraphs[0]
    encabezado.text = ""
    encabezado.paragraph_format.tab_stops.add_tab_stop(
        DocxInches(ancho_util), WD_TAB_ALIGNMENT.RIGHT
    )
    marca = encabezado.add_run("Banamex · Base Cards")
    marca.font.name = familia
    marca.font.size = DocxPt(tipos["encabezado"])
    marca.font.bold = True
    marca.font.color.rgb = primario
    confidencial = encabezado.add_run("\tConfidencial — uso interno")
    confidencial.font.name = familia
    confidencial.font.size = DocxPt(tipos["encabezado"])
    confidencial.font.color.rgb = tenue
    _docx_borde_inferior(encabezado, paleta["primario"])

    pie = seccion.footer.paragraphs[0]
    pie.text = ""
    pie.paragraph_format.tab_stops.add_tab_stop(DocxInches(ancho_util), WD_TAB_ALIGNMENT.RIGHT)
    izquierda = pie.add_run("Memo RC-05")
    izquierda.font.name = familia
    izquierda.font.size = DocxPt(tipos["pie"])
    izquierda.font.color.rgb = tenue
    etiqueta = pie.add_run("\tPágina ")
    etiqueta.font.name = familia
    etiqueta.font.size = DocxPt(tipos["pie"])
    etiqueta.font.color.rgb = tenue
    _docx_campo(pie, "PAGE", familia, tipos["pie"], tenue)
    de = pie.add_run(" de ")
    de.font.name = familia
    de.font.size = DocxPt(tipos["pie"])
    de.font.color.rgb = tenue
    _docx_campo(pie, "NUMPAGES", familia, tipos["pie"], tenue)
    _docx_borde_inferior(pie, paleta["neutro_claro"])


def _generar_docx(branding: dict) -> Path:
    documento = branding["documento"]
    doc = Document()
    seccion = doc.sections[0]
    seccion.page_width = DocxInches(documento["pagina"]["ancho_pulgadas"])
    seccion.page_height = DocxInches(documento["pagina"]["alto_pulgadas"])
    margenes = documento["margenes_pulgadas"]
    seccion.left_margin = DocxInches(margenes["izquierda"])
    seccion.right_margin = DocxInches(margenes["derecha"])
    seccion.top_margin = DocxInches(margenes["superior"])
    seccion.bottom_margin = DocxInches(margenes["inferior"])
    seccion.header_distance = DocxInches(0.35)
    seccion.footer_distance = DocxInches(0.35)
    _docx_estilos(doc, branding)
    _docx_encabezado_pie(seccion, branding)

    doc.core_properties.title = "Plantilla base Banamex (documento)"
    doc.core_properties.author = "basecards"
    doc.core_properties.comments = "Generada desde config/branding/banamex.yaml"

    destino = AQUI / documento["plantilla_base"]
    doc.save(str(destino))
    print(f"Plantilla generada: {destino}")
    return destino


def main() -> int:
    branding = _cargar_branding()
    prs = Presentation()
    ancho_original = prs.slide_width
    alto_original = prs.slide_height
    prs.slide_width = Inches(branding["diapositiva"]["ancho_pulgadas"])
    prs.slide_height = Inches(branding["diapositiva"]["alto_pulgadas"])

    factor_x = prs.slide_width / ancho_original
    factor_y = prs.slide_height / alto_original
    # Primero los layouts (heredan la geometría original del master) y después
    # el master, para no escalar dos veces los valores heredados.
    for layout in prs.slide_layouts:
        _escalar_formas(layout.shapes, factor_x, factor_y)
    _escalar_formas(prs.slide_master.shapes, factor_x, factor_y)

    layouts = prs.slide_masters[0].slide_layouts
    for indice, nombre in LAYOUTS.items():
        layouts[indice].name = nombre

    _pintar_tema(prs, branding)

    prs.core_properties.title = "Plantilla base Banamex"
    prs.core_properties.author = "basecards"
    prs.core_properties.comments = "Generada desde config/branding/banamex.yaml"

    destino = AQUI / branding["plantilla_base"]
    prs.save(destino)
    print(f"Plantilla generada: {destino}")
    _generar_docx(branding)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
