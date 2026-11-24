"""Genera ``template_banamex.pptx`` a partir de ``banamex.yaml``.

Uso:
    .\\.venv\\Scripts\\python.exe config\\branding\\generar_template.py

El archivo resultante es la plantilla base compartida por las skills de negocio.
Solo se ejecuta cuando cambie el branding, no en runtime.
"""

from __future__ import annotations

from pathlib import Path

import yaml
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


def main() -> int:
    branding = _cargar_branding()
    prs = Presentation()
    prs.slide_width = Inches(branding["diapositiva"]["ancho_pulgadas"])
    prs.slide_height = Inches(branding["diapositiva"]["alto_pulgadas"])

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
