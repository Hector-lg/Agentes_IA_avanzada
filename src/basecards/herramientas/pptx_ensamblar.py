"""Ensamblado determinista de la presentación mensual en ``.pptx``.

La herramienta abre la plantilla institucional (o la de la skill), crea las
siete diapositivas del contrato de ``SKILL.md``, sustituye los marcadores
``{{m.<metrica>.<cohorte>.<MOBn>|<formato>}}`` por valores ya calculados e
inserta las gráficas y tablas declaradas en ``charts.yaml``.

El texto narrativo lo produce el LLM con marcadores; este módulo no calcula
métricas ni redacta. Solo valida, formatea y coloca el contenido.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.enum.shapes import MSO_SHAPE, MSO_SHAPE_TYPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Inches, Pt
from pydantic import ValidationError

from basecards.herramientas.skills import GraficasDoc

ROOT = Path(__file__).resolve().parents[3]
BRANDING_RUTA = ROOT / "config" / "branding" / "banamex.yaml"
PLANTILLA_FALLBACK = ROOT / "config" / "branding" / "template_banamex.pptx"

LAYOUTS_REQUERIDOS = ("Portada", "Titulo_y_Grafica", "Dos_Columnas", "Tabla", "Cierre")

FORMATOS_VALOR = frozenset({"pct1", "pct2", "mxn", "mxn_millones", "int"})

PATRON_MARCADOR = re.compile(
    r"\{\{m\.(?P<metrica>[A-Za-z_][A-Za-z0-9_]*)"
    r"\.(?P<cohorte>\d{6}|todas)"
    r"\.MOB(?P<mob>\d+)"
    r"\|(?P<formato>pct1|pct2|mxn|mxn_millones|int)\}\}"
)


class PptxError(Exception):
    """Error de ensamblado con un código estable ``E-PPT-*``."""

    def __init__(self, codigo: str, mensaje: str, ruta: str = "") -> None:
        super().__init__(f"{codigo}: {mensaje}")
        self.codigo = codigo
        self.mensaje = mensaje
        self.ruta = ruta


@dataclass(frozen=True)
class _SeccionSkill:
    titulo: str
    recurso: str | None


# Orden fijo del contrato de seguimiento de cosechas. El recurso es el nombre
# de la gráfica o tabla que la sección debe insertar.
SECCIONES: tuple[tuple[str, str, str | None, str], ...] = (
    ("portada", "Portada", None, "Portada"),
    ("resumen_ejecutivo", "Dos_Columnas", None, "Resumen ejecutivo"),
    ("volumen_cosecha", "Titulo_y_Grafica", "volumen_cosecha", "Volumen por cosecha"),
    ("curvas_over30", "Titulo_y_Grafica", "curvas_over30", "Curvas Over30 vs. esperada"),
    ("curvas_ncl", "Titulo_y_Grafica", "curvas_ncl", "Curvas NCL vs. esperada"),
    ("fuera_intervalo", "Tabla", "fuera_intervalo", "Cohortes fuera de intervalo"),
    ("siguientes_pasos", "Cierre", None, "Siguientes pasos"),
)

CLAVES_SECCION = frozenset(clave for clave, _, _, _ in SECCIONES)

CLAVES_CONTENIDO: dict[str, frozenset[str]] = {
    "portada": frozenset({"titulo", "subtitulo"}),
    "resumen_ejecutivo": frozenset({"titulo", "vinetas", "columnas"}),
    "volumen_cosecha": frozenset({"titulo", "nota"}),
    "curvas_over30": frozenset({"titulo", "nota"}),
    "curvas_ncl": frozenset({"titulo", "nota"}),
    "fuera_intervalo": frozenset({"titulo", "nota"}),
    "siguientes_pasos": frozenset({"titulo", "vinetas"}),
}

DatosGrafica = Path | str | Mapping[str, Any]
DatosTabla = Mapping[str, Any]


# ---------------------------------------------------------------------------
# Marcadores y formato
# ---------------------------------------------------------------------------


def formatear_valor(valor: float, formato: str) -> str:
    """Formatea un valor ya calculado según el formato del marcador."""
    if formato not in FORMATOS_VALOR:
        raise PptxError(
            "E-PPT-030",
            f"formato desconocido {formato!r}; usa {sorted(FORMATOS_VALOR)}",
        )
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        raise PptxError("E-PPT-030", f"el valor {valor!r} no es numérico")
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
            raise PptxError(
                "E-PPT-030",
                f"el marcador {clave} no tiene valor calculado",
            )
        return formatear_valor(metricas[clave], coincidencia.group("formato"))

    return PATRON_MARCADOR.sub(reemplazo, texto)


# ---------------------------------------------------------------------------
# Carga de la skill, la plantilla y el branding
# ---------------------------------------------------------------------------


def _partir_frontmatter(texto: str) -> str:
    if not texto.startswith("---"):
        return texto
    partes = texto.split("---", 2)
    if len(partes) < 3:
        return texto
    return partes[2]


def _limpiar_titulo(linea: str) -> str:
    titulo = re.split(r"\s+[—–-]\s+", linea, maxsplit=1)[0].strip()
    return titulo.rstrip(".").strip()


def _leer_secciones_skill(skill_ruta: Path) -> list[_SeccionSkill]:
    archivo = skill_ruta / "SKILL.md"
    if not archivo.is_file():
        raise PptxError("E-PPT-010", f"falta {archivo}", "SKILL.md")
    try:
        texto = archivo.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PptxError("E-PPT-010", f"no se pudo leer SKILL.md: {exc}", "SKILL.md") from exc

    cuerpo = _partir_frontmatter(texto)
    dentro = False
    secciones: list[_SeccionSkill] = []
    for linea in cuerpo.splitlines():
        limpia = linea.strip()
        if limpia.lower().startswith("## secciones"):
            dentro = True
            continue
        if dentro and limpia.startswith("## "):
            break
        if not dentro:
            continue
        coincidencia = re.match(r"^\d+\.\s+(.+)$", limpia)
        if coincidencia is None:
            continue
        cruda = coincidencia.group(1)
        recurso = re.search(r"`([a-z_][a-z0-9_]*)`", cruda)
        secciones.append(
            _SeccionSkill(
                titulo=_limpiar_titulo(cruda),
                recurso=recurso.group(1) if recurso else None,
            )
        )

    if len(secciones) != len(SECCIONES):
        raise PptxError(
            "E-PPT-010",
            f"SKILL.md declara {len(secciones)} secciones; el contrato exige {len(SECCIONES)}",
            "SKILL.md",
        )
    for (_, _, recurso_esperado, _), seccion in zip(SECCIONES, secciones, strict=True):
        if recurso_esperado is not None and seccion.recurso != recurso_esperado:
            raise PptxError(
                "E-PPT-010",
                f"la sección {seccion.titulo!r} declara el recurso {seccion.recurso!r}; "
                f"se esperaba {recurso_esperado!r}",
                "SKILL.md",
            )
    return secciones


def _leer_titulo_skill(skill_ruta: Path) -> str:
    archivo = skill_ruta / "SKILL.md"
    try:
        texto = archivo.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return skill_ruta.name
    if not texto.startswith("---"):
        return skill_ruta.name
    cabecera = texto.split("---", 2)[1]
    try:
        datos = yaml.safe_load(cabecera) or {}
    except yaml.YAMLError:
        return skill_ruta.name
    nombre = datos.get("name") if isinstance(datos, dict) else None
    return nombre if isinstance(nombre, str) and nombre.strip() else skill_ruta.name


def _cargar_branding() -> dict[str, Any]:
    if not BRANDING_RUTA.is_file():
        raise PptxError("E-PPT-002", f"falta el branding {BRANDING_RUTA}", "branding")
    try:
        datos = yaml.safe_load(BRANDING_RUTA.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise PptxError("E-PPT-002", f"no se pudo leer el branding: {exc}", "branding") from exc
    if not isinstance(datos, dict):
        raise PptxError("E-PPT-002", "el branding debe ser un mapa YAML", "branding")
    return datos


def _cargar_charts(skill_ruta: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    archivo = skill_ruta / "charts.yaml"
    if not archivo.is_file():
        raise PptxError("E-PPT-011", f"falta {archivo}", "charts.yaml")
    try:
        datos = yaml.safe_load(archivo.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise PptxError("E-PPT-011", f"no se pudo leer charts.yaml: {exc}", "charts.yaml") from exc
    try:
        documento = GraficasDoc.model_validate(datos)
    except ValidationError as exc:
        error = exc.errors()[0]
        ubicacion = ".".join(str(parte) for parte in error["loc"])
        raise PptxError(
            "E-PPT-011",
            f"charts.yaml inválido en {ubicacion}: {error['msg']}",
            "charts.yaml",
        ) from exc
    graficas = {nombre: grafica.model_dump() for nombre, grafica in documento.graficas.items()}
    return graficas, documento.estilos


def _resolver_plantilla(skill_ruta: Path, plantilla: Path | str | None) -> Path:
    if plantilla is not None:
        ruta = Path(plantilla).expanduser()
        if not ruta.is_file():
            raise PptxError("E-PPT-002", f"no existe la plantilla {ruta}", "plantilla")
        return ruta.resolve()
    propia = skill_ruta / "template.pptx"
    if propia.is_file():
        return propia.resolve()
    if not PLANTILLA_FALLBACK.is_file():
        raise PptxError("E-PPT-002", f"falta la plantilla base {PLANTILLA_FALLBACK}", "plantilla")
    return PLANTILLA_FALLBACK


def _abrir_plantilla(ruta: Path) -> Presentation:
    try:
        prs = Presentation(str(ruta))
    except Exception as exc:  # python-pptx lanza distintos tipos según el daño
        raise PptxError(
            "E-PPT-002", f"no se pudo abrir la plantilla {ruta}: {exc}", "plantilla"
        ) from exc
    nombres = {layout.name for layout in prs.slide_layouts}
    faltantes = [nombre for nombre in LAYOUTS_REQUERIDOS if nombre not in nombres]
    if faltantes:
        raise PptxError(
            "E-PPT-003",
            f"la plantilla {ruta} no tiene los layouts {faltantes}",
            "plantilla",
        )
    return prs


def _vaciar_diapositivas(prs: Presentation) -> None:
    lista = prs.slides._sldIdLst
    for sld_id in list(lista):
        prs.part.drop_rel(sld_id.rId)
        lista.remove(sld_id)


# ---------------------------------------------------------------------------
# Utilidades de dibujo
# ---------------------------------------------------------------------------


def _layout(prs: Presentation, nombre: str) -> Any:
    for layout in prs.slide_layouts:
        if layout.name == nombre:
            return layout
    raise PptxError("E-PPT-003", f"falta el layout {nombre!r}", "plantilla")


def _placeholder(slide: Any, idx: int) -> Any:
    for forma in slide.placeholders:
        if forma.placeholder_format.idx == idx:
            return forma
    raise PptxError(
        "E-PPT-003",
        f"la diapositiva {slide.slide_id} no tiene el placeholder {idx}",
        "plantilla",
    )


def _quitar(forma: Any) -> None:
    elemento = forma._element
    elemento.getparent().remove(elemento)


def _color(branding: Mapping[str, Any], nombre: str) -> RGBColor:
    paleta = branding.get("paleta", {})
    valor = paleta.get(nombre)
    if not isinstance(valor, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", valor):
        raise PptxError("E-PPT-002", f"color inválido {nombre!r} en el branding", "branding")
    return RGBColor.from_string(valor.lstrip("#").upper())


def _familia(branding: Mapping[str, Any]) -> str:
    tipografia = branding.get("tipografia", {})
    familia = tipografia.get("familia", "Arial")
    return str(familia)


def _aplicar_fuente(
    run: Any,
    branding: Mapping[str, Any],
    tamano: float,
    color: str,
    *,
    negrita: bool = False,
) -> None:
    run.font.name = _familia(branding)
    run.font.size = Pt(tamano)
    run.font.bold = negrita
    run.font.color.rgb = _color(branding, color)


def _titulo_texto(seccion: Mapping[str, Any], defecto: str, clave: str) -> str:
    valor = seccion.get("titulo", defecto)
    if not isinstance(valor, str) or not valor.strip():
        raise PptxError("E-PPT-021", f"la sección {clave!r} necesita un título no vacío")
    return valor.strip()


def _validar_sin_marcadores(texto: str, campo: str) -> None:
    if "{{m." in texto:
        raise PptxError(
            "E-PPT-031",
            f"{campo} no admite métricas; usa solo texto narrativo",
        )


def _leer_vinetas(
    seccion: Mapping[str, Any],
    branding: Mapping[str, Any],
    campo: str,
) -> list[str]:
    vinetas = seccion.get("vinetas", [])
    if not isinstance(vinetas, list) or not all(isinstance(v, str) for v in vinetas):
        raise PptxError("E-PPT-020", f"{campo}.vinetas debe ser una lista de textos")
    reglas = branding.get("reglas", {})
    max_vinetas = int(reglas.get("max_vinetas", 4))
    max_palabras = int(reglas.get("max_palabras_vineta", 40))
    if len(vinetas) > max_vinetas:
        raise PptxError(
            "E-PPT-032",
            f"{campo} tiene {len(vinetas)} viñetas; el máximo es {max_vinetas}",
        )
    for vineta in vinetas:
        palabras = len(vineta.split())
        if palabras > max_palabras:
            raise PptxError(
                "E-PPT-033",
                f"una viñeta de {campo} tiene {palabras} palabras; el máximo es {max_palabras}",
            )
    return vinetas


def _escribir_titulo(
    forma: Any,
    texto: str,
    branding: Mapping[str, Any],
    *,
    tamano: float | None = None,
) -> None:
    tipografia = branding.get("tipografia", {})
    tamano = tamano or float(tipografia.get("tamano_titulo", 24))
    marco = forma.text_frame
    marco.clear()
    marco.word_wrap = True
    parrafo = marco.paragraphs[0]
    parrafo.text = texto
    parrafo.alignment = PP_ALIGN.LEFT
    for run in parrafo.runs:
        _aplicar_fuente(run, branding, tamano, "texto", negrita=True)


def _escribir_texto(
    forma: Any,
    texto: str,
    branding: Mapping[str, Any],
    *,
    tamano: float | None = None,
    color: str = "texto",
    negrita: bool = False,
    alineacion: PP_ALIGN = PP_ALIGN.LEFT,
) -> None:
    tipografia = branding.get("tipografia", {})
    tamano = tamano or float(tipografia.get("tamano_cuerpo", 14))
    marco = forma.text_frame
    marco.clear()
    marco.word_wrap = True
    parrafo = marco.paragraphs[0]
    parrafo.text = texto
    parrafo.alignment = alineacion
    for run in parrafo.runs:
        _aplicar_fuente(run, branding, tamano, color, negrita=negrita)


def _poner_vineta(parrafo: Any) -> None:
    p_pr = parrafo._p.get_or_add_pPr()
    existentes = {qn("a:buNone"), qn("a:buChar"), qn("a:buAutoNum")}
    for nodo in list(p_pr):
        if nodo.tag in existentes:
            p_pr.remove(nodo)
    fuente = OxmlElement("a:buFont")
    fuente.set("typeface", "Arial")
    caracter = OxmlElement("a:buChar")
    caracter.set("char", "•")
    p_pr.append(fuente)
    p_pr.append(caracter)


def _escribir_vinetas(
    forma: Any,
    vinetas: Sequence[str],
    branding: Mapping[str, Any],
) -> None:
    tipografia = branding.get("tipografia", {})
    tamano = float(tipografia.get("tamano_cuerpo", 14))
    marco = forma.text_frame
    marco.clear()
    marco.word_wrap = True
    if not vinetas:
        return
    for indice, texto in enumerate(vinetas):
        parrafo = marco.paragraphs[0] if indice == 0 else marco.add_paragraph()
        parrafo.text = texto
        parrafo.level = 0
        parrafo.space_after = Pt(10)
        parrafo.line_spacing = 1.1
        _poner_vineta(parrafo)
        for run in parrafo.runs:
            _aplicar_fuente(run, branding, tamano, "texto")


def _barra_acento(slide: Any, prs: Presentation, branding: Mapping[str, Any]) -> None:
    alto = Inches(0.16)
    forma = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, 0, prs.slide_height - alto, prs.slide_width, alto
    )
    forma.fill.solid()
    forma.fill.fore_color.rgb = _color(branding, "acento")
    forma.line.fill.background()


def _area_placeholder(
    prs: Presentation,
    forma: Any,
    branding: Mapping[str, Any],
) -> tuple[int, int, int, int]:
    margenes = branding.get("margenes_pulgadas", {})
    izquierda = max(int(forma.left), Inches(float(margenes.get("izquierda", 0.6))))
    superior = max(int(forma.top), Inches(float(margenes.get("superior", 0.4))))
    derecha = min(
        int(forma.left + forma.width),
        int(prs.slide_width) - Inches(float(margenes.get("derecha", 0.6))),
    )
    inferior = min(
        int(forma.top + forma.height),
        int(prs.slide_height) - Inches(float(margenes.get("inferior", 0.4))),
    )
    return izquierda, superior, max(1, derecha - izquierda), max(1, inferior - superior)


def _area_margenes(prs: Presentation, branding: Mapping[str, Any]) -> tuple[int, int, int, int]:
    margenes = branding.get("margenes_pulgadas", {})
    izquierda = Inches(float(margenes.get("izquierda", 0.6)))
    derecha = Inches(float(margenes.get("derecha", 0.6)))
    superior = Inches(float(margenes.get("superior", 0.4)))
    inferior = Inches(float(margenes.get("inferior", 0.4)))
    ancho = int(prs.slide_width) - izquierda - derecha
    alto = int(prs.slide_height) - superior - inferior
    return int(izquierda), int(superior), int(ancho), int(alto)


def _pie_fuente(
    slide: Any,
    prs: Presentation,
    branding: Mapping[str, Any],
    estilos: Mapping[str, Any],
    corte: str | None,
) -> None:
    plantilla = estilos.get("fuente_datos")
    if not isinstance(plantilla, str) or not plantilla.strip():
        return
    if corte:
        texto = plantilla.replace("{corte}", corte)
    else:
        texto = plantilla.replace(", corte {corte}", "").replace("{corte}", "")
    margenes = branding.get("margenes_pulgadas", {})
    izquierda = Inches(float(margenes.get("izquierda", 0.6)))
    inferior = Inches(float(margenes.get("inferior", 0.4)))
    ancho = int(prs.slide_width) - 2 * izquierda
    caja = slide.shapes.add_textbox(
        izquierda,
        int(prs.slide_height) - inferior - Inches(0.22),
        ancho,
        Inches(0.22),
    )
    _escribir_texto(
        caja,
        texto,
        branding,
        tamano=float(branding.get("tipografia", {}).get("tamano_pie", 9)),
        color="texto",
    )


# ---------------------------------------------------------------------------
# Gráficas y tablas
# ---------------------------------------------------------------------------


def _insertar_imagen(
    slide: Any,
    area: tuple[int, int, int, int],
    ruta: Path,
    nombre: str,
) -> None:
    izquierda, superior, ancho, alto = area
    if not ruta.is_file():
        raise PptxError("E-PPT-042", f"no existe la imagen de la gráfica {nombre}: {ruta}")
    try:
        imagen = slide.shapes.add_picture(str(ruta), izquierda, superior, width=ancho)
    except Exception as exc:
        raise PptxError("E-PPT-042", f"no se pudo insertar la imagen {ruta}: {exc}") from exc
    if imagen.height > alto:
        escala = alto / imagen.height
        imagen.width = int(imagen.width * escala)
        imagen.height = alto
    imagen.left = izquierda + (ancho - imagen.width) // 2
    imagen.top = superior + (alto - imagen.height) // 2


def _valores_numericos(valores: Any, nombre: str) -> list[float | int]:
    if not isinstance(valores, (list, tuple)) or not valores:
        raise PptxError("E-PPT-042", f"la serie {nombre!r} necesita una lista de valores")
    numeros: list[float | int] = []
    for valor in valores:
        if isinstance(valor, bool) or not isinstance(valor, (int, float)):
            raise PptxError(
                "E-PPT-042",
                f"la serie {nombre!r} contiene un valor no numérico: {valor!r}",
            )
        numeros.append(valor)
    return numeros


def _insertar_grafica_nativa(
    slide: Any,
    area: tuple[int, int, int, int],
    nombre: str,
    definicion: Mapping[str, Any],
    datos: Mapping[str, Any],
    branding: Mapping[str, Any],
    metricas: Mapping[str, float | int],
) -> None:
    categorias = datos.get("categorias")
    series = datos.get("series")
    if not isinstance(categorias, list) or not categorias:
        raise PptxError("E-PPT-042", f"la gráfica {nombre!r} necesita 'categorias'")
    if not isinstance(series, Mapping) or not series:
        raise PptxError("E-PPT-042", f"la gráfica {nombre!r} necesita 'series'")

    tipo = definicion.get("tipo")
    if tipo == "columnas":
        tipo_chart = XL_CHART_TYPE.COLUMN_CLUSTERED
    elif tipo == "barras":
        tipo_chart = XL_CHART_TYPE.BAR_CLUSTERED
    elif tipo in {"lineas", "abanico"}:
        tipo_chart = XL_CHART_TYPE.LINE_MARKERS
    else:
        raise PptxError(
            "E-PPT-042",
            f"la gráfica {nombre!r} de tipo {tipo!r} no se puede crear de forma nativa",
        )

    datos_chart = CategoryChartData()
    datos_chart.categories = [
        sustituir_marcadores(categoria, metricas) if isinstance(categoria, str) else categoria
        for categoria in categorias
    ]
    for etiqueta, valores in series.items():
        numeros = _valores_numericos(valores, str(etiqueta))
        if len(numeros) != len(categorias):
            raise PptxError(
                "E-PPT-042",
                f"la serie {etiqueta!r} tiene {len(numeros)} valores y hay "
                f"{len(categorias)} categorías",
            )
        datos_chart.add_series(sustituir_marcadores(str(etiqueta), metricas), tuple(numeros))

    izquierda, superior, ancho, alto = area
    try:
        forma = slide.shapes.add_chart(tipo_chart, izquierda, superior, ancho, alto, datos_chart)
    except Exception as exc:
        raise PptxError("E-PPT-042", f"no se pudo crear la gráfica {nombre!r}: {exc}") from exc

    chart = forma.chart
    chart.has_title = False
    chart.font.name = _familia(branding)
    chart.font.size = Pt(9)
    chart.has_legend = len(series) > 1
    if chart.has_legend:
        chart.legend.position = XL_LEGEND_POSITION.BOTTOM
        chart.legend.include_in_layout = False
        chart.legend.font.size = Pt(9)

    paleta = branding.get("graficas", {}).get("paleta", ["primario", "acento"])
    plot = chart.plots[0]
    plot.has_data_labels = False
    if tipo in {"columnas", "barras"}:
        plot.gap_width = 60
    for indice, serie in enumerate(plot.series):
        color = _color(branding, str(paleta[indice % len(paleta)]))
        if tipo in {"columnas", "barras"}:
            serie.format.fill.solid()
            serie.format.fill.fore_color.rgb = color
        else:
            serie.format.line.color.rgb = color
            serie.format.line.width = Pt(1.75)

    chart.category_axis.has_major_gridlines = False
    chart.category_axis.tick_labels.font.size = Pt(9)
    chart.value_axis.has_major_gridlines = True
    chart.value_axis.tick_labels.font.size = Pt(9)
    chart.value_axis.tick_labels.number_format = "#,##0"
    chart.value_axis.tick_labels.number_format_is_linked = False


def _insertar_grafica(
    slide: Any,
    area: tuple[int, int, int, int],
    nombre: str,
    definicion: Mapping[str, Any],
    datos: DatosGrafica,
    branding: Mapping[str, Any],
    metricas: Mapping[str, float | int],
) -> None:
    if isinstance(datos, (str, Path)):
        _insertar_imagen(slide, area, Path(datos), nombre)
        return
    if not isinstance(datos, Mapping):
        raise PptxError(
            "E-PPT-042",
            f"la gráfica {nombre!r} debe ser una ruta de imagen o un mapa con categorías y series",
        )
    if not definicion.get("nativa"):
        raise PptxError(
            "E-PPT-042",
            f"la gráfica {nombre!r} se genera con matplotlib; pasa la ruta de la imagen",
        )
    _insertar_grafica_nativa(slide, area, nombre, definicion, datos, branding, metricas)


def _texto_celda(
    valor: Any,
    formato: str | None,
    metricas: Mapping[str, float | int],
) -> str:
    if valor is None:
        return ""
    if formato is not None and isinstance(valor, (int, float)) and not isinstance(valor, bool):
        return formatear_valor(valor, formato)
    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        return f"{valor:,}" if isinstance(valor, int) else f"{valor:,.2f}"
    return sustituir_marcadores(str(valor), metricas)


def _insertar_tabla(
    slide: Any,
    area: tuple[int, int, int, int],
    nombre: str,
    definicion: Mapping[str, Any],
    datos: DatosTabla,
    branding: Mapping[str, Any],
    metricas: Mapping[str, float | int],
) -> None:
    columnas = datos.get("columnas")
    filas = datos.get("filas")
    formatos = datos.get("formatos", {})
    if (
        not isinstance(columnas, list)
        or not columnas
        or not all(isinstance(columna, str) and columna for columna in columnas)
    ):
        raise PptxError("E-PPT-043", f"la tabla {nombre!r} necesita 'columnas' de texto")
    if not isinstance(filas, list):
        raise PptxError("E-PPT-043", f"la tabla {nombre!r} necesita 'filas'")
    if not isinstance(formatos, Mapping) or not all(
        isinstance(clave, str) and isinstance(valor, str) for clave, valor in formatos.items()
    ):
        raise PptxError("E-PPT-043", f"la tabla {nombre!r} tiene 'formatos' inválidos")
    if filas and isinstance(filas[0], Mapping):
        filas = [
            [fila.get(columna, "") for columna in columnas]  # type: ignore[union-attr]
            for fila in filas
        ]
    for fila in filas:
        if not isinstance(fila, (list, tuple)) or len(fila) != len(columnas):
            raise PptxError(
                "E-PPT-043",
                f"cada fila de {nombre!r} debe tener {len(columnas)} valores",
            )

    max_filas = definicion.get("max_filas")
    if isinstance(max_filas, int) and max_filas > 0:
        filas = filas[:max_filas]

    izquierda, superior, ancho, alto = area
    filas_tabla = len(filas) + 1
    alto_fila = min(Inches(0.34), max(Inches(0.26), int(alto) // filas_tabla))
    alto_tabla = alto_fila * filas_tabla
    try:
        forma = slide.shapes.add_table(
            filas_tabla, len(columnas), izquierda, superior, ancho, alto_tabla
        )
    except Exception as exc:
        raise PptxError("E-PPT-043", f"no se pudo crear la tabla {nombre!r}: {exc}") from exc

    tabla = forma.table
    ancho_columna = ancho // len(columnas)
    for columna in tabla.columns:
        columna.width = ancho_columna

    tabla.rows[0].height = alto_fila
    for indice, columna in enumerate(columnas):
        celda = tabla.cell(0, indice)
        celda.fill.solid()
        celda.fill.fore_color.rgb = _color(branding, "primario")
        celda.vertical_anchor = MSO_ANCHOR.MIDDLE
        celda.margin_left = Inches(0.06)
        celda.margin_right = Inches(0.06)
        marco = celda.text_frame
        marco.word_wrap = True
        parrafo = marco.paragraphs[0]
        parrafo.text = _texto_celda(columna, None, metricas)
        parrafo.alignment = PP_ALIGN.LEFT
        for run in parrafo.runs:
            _aplicar_fuente(run, branding, 9, "fondo", negrita=True)

    for indice_fila, fila in enumerate(filas, start=1):
        tabla.rows[indice_fila].height = alto_fila
        relleno = "neutro_claro" if indice_fila % 2 == 1 else "fondo"
        for indice_columna, valor in enumerate(fila):
            celda = tabla.cell(indice_fila, indice_columna)
            celda.fill.solid()
            celda.fill.fore_color.rgb = _color(branding, relleno)
            celda.vertical_anchor = MSO_ANCHOR.MIDDLE
            celda.margin_left = Inches(0.06)
            celda.margin_right = Inches(0.06)
            formato = formatos.get(columnas[indice_columna])
            marco = celda.text_frame
            marco.word_wrap = True
            parrafo = marco.paragraphs[0]
            parrafo.text = _texto_celda(valor, formato, metricas)
            parrafo.alignment = PP_ALIGN.RIGHT if isinstance(valor, (int, float)) else PP_ALIGN.LEFT
            for run in parrafo.runs:
                _aplicar_fuente(run, branding, 9, "texto")


# ---------------------------------------------------------------------------
# Secciones
# ---------------------------------------------------------------------------


def _seccion(contenido: Mapping[str, Any], clave: str) -> Mapping[str, Any]:
    valor = contenido.get(clave, {})
    if not isinstance(valor, Mapping):
        raise PptxError("E-PPT-020", f"la sección {clave!r} debe ser un mapa")
    desconocidas = set(valor) - CLAVES_CONTENIDO[clave]
    if desconocidas:
        raise PptxError(
            "E-PPT-020",
            f"la sección {clave!r} tiene claves no soportadas: {sorted(desconocidas)}",
        )
    return valor


def _crear_portada(
    prs: Presentation,
    branding: Mapping[str, Any],
    seccion: Mapping[str, Any],
    defecto: str,
    corte: str | None,
    titulo_skill: str,
) -> None:
    slide = prs.slides.add_slide(_layout(prs, "Portada"))
    titulo = seccion.get("titulo", titulo_skill or defecto)
    if not isinstance(titulo, str) or not titulo.strip():
        raise PptxError("E-PPT-021", "la portada necesita un título no vacío")
    titulo = titulo.strip()
    _validar_sin_marcadores(titulo, "la portada")

    subtitulo = seccion.get("subtitulo")
    if subtitulo is None:
        subtitulo = f"Corte {corte}" if corte else None
    elif not isinstance(subtitulo, str):
        raise PptxError("E-PPT-020", "el subtítulo de la portada debe ser texto")
    else:
        _validar_sin_marcadores(subtitulo, "la portada")

    _escribir_titulo(_placeholder(slide, 0), titulo, branding, tamano=36)
    if subtitulo:
        _escribir_texto(
            _placeholder(slide, 1),
            subtitulo,
            branding,
            tamano=18,
            color="primario",
        )
    else:
        _quitar(_placeholder(slide, 1))
    _barra_acento(slide, prs, branding)


def _crear_resumen(
    prs: Presentation,
    branding: Mapping[str, Any],
    seccion: Mapping[str, Any],
    defecto: str,
) -> None:
    slide = prs.slides.add_slide(_layout(prs, "Dos_Columnas"))
    _escribir_titulo(
        _placeholder(slide, 0), _titulo_texto(seccion, defecto, "resumen_ejecutivo"), branding
    )

    columnas = seccion.get("columnas")
    if columnas is None:
        vinetas = _leer_vinetas(seccion, branding, "resumen_ejecutivo")
        mitad = (len(vinetas) + 1) // 2
        columnas = [vinetas[:mitad], vinetas[mitad:]]
    else:
        if (
            not isinstance(columnas, list)
            or len(columnas) != 2
            or not all(isinstance(columna, list) for columna in columnas)
        ):
            raise PptxError("E-PPT-020", "resumen_ejecutivo.columnas debe tener dos listas")
        columnas = [
            _leer_vinetas({"vinetas": columna}, branding, "resumen_ejecutivo")
            for columna in columnas
        ]
        if sum(len(columna) for columna in columnas) > int(
            branding.get("reglas", {}).get("max_vinetas", 4)
        ):
            raise PptxError("E-PPT-032", "el resumen ejecutivo supera el máximo de viñetas")

    for idx, columna in zip((1, 2), columnas, strict=True):
        forma = _placeholder(slide, idx)
        if columna:
            _escribir_vinetas(forma, columna, branding)
        else:
            _quitar(forma)


def _crear_grafica(
    prs: Presentation,
    branding: Mapping[str, Any],
    estilos: Mapping[str, Any],
    definiciones: Mapping[str, Mapping[str, Any]],
    seccion: Mapping[str, Any],
    defecto: str,
    nombre_grafica: str,
    datos: DatosGrafica | None,
    metricas: Mapping[str, float | int],
    corte: str | None,
) -> None:
    slide = prs.slides.add_slide(_layout(prs, "Titulo_y_Grafica"))
    _escribir_titulo(
        _placeholder(slide, 0), _titulo_texto(seccion, defecto, nombre_grafica), branding
    )
    if datos is None:
        raise PptxError("E-PPT-041", f"falta la gráfica {nombre_grafica!r}")
    definicion = definiciones.get(nombre_grafica)
    if definicion is None:
        raise PptxError("E-PPT-040", f"la gráfica {nombre_grafica!r} no está en charts.yaml")

    forma = _placeholder(slide, 1)
    area = _area_placeholder(prs, forma, branding)
    _quitar(forma)

    nota = seccion.get("nota")
    if nota is not None:
        if not isinstance(nota, str):
            raise PptxError("E-PPT-020", f"la nota de {nombre_grafica!r} debe ser texto")
        area = (area[0], area[1], area[2], max(1, area[3] - int(Inches(0.5))))

    _insertar_grafica(slide, area, nombre_grafica, definicion, datos, branding, metricas)

    if nota:
        margenes = branding.get("margenes_pulgadas", {})
        izquierda = Inches(float(margenes.get("izquierda", 0.6)))
        ancho = int(prs.slide_width) - 2 * int(izquierda)
        caja = slide.shapes.add_textbox(
            izquierda,
            area[1] + area[3] + int(Inches(0.05)),
            ancho,
            Inches(0.4),
        )
        _escribir_texto(
            caja,
            nota,
            branding,
            tamano=float(branding.get("tipografia", {}).get("tamano_pie", 9)),
            color="texto",
        )
    _pie_fuente(slide, prs, branding, estilos, corte)


def _crear_tabla(
    prs: Presentation,
    branding: Mapping[str, Any],
    estilos: Mapping[str, Any],
    definiciones: Mapping[str, Mapping[str, Any]],
    seccion: Mapping[str, Any],
    defecto: str,
    nombre_tabla: str,
    datos: DatosTabla | None,
    metricas: Mapping[str, float | int],
    corte: str | None,
) -> None:
    slide = prs.slides.add_slide(_layout(prs, "Tabla"))
    titulo = _placeholder(slide, 0)
    _escribir_titulo(titulo, _titulo_texto(seccion, defecto, nombre_tabla), branding)
    if datos is None:
        raise PptxError("E-PPT-041", f"falta la tabla {nombre_tabla!r}")
    definicion = definiciones.get(nombre_tabla)
    if definicion is None:
        raise PptxError("E-PPT-040", f"la tabla {nombre_tabla!r} no está en charts.yaml")

    nota = seccion.get("nota")
    if nota is not None and not isinstance(nota, str):
        raise PptxError("E-PPT-020", f"la nota de {nombre_tabla!r} debe ser texto")

    margenes = branding.get("margenes_pulgadas", {})
    izquierda = Inches(float(margenes.get("izquierda", 0.6)))
    derecha = Inches(float(margenes.get("derecha", 0.6)))
    inferior = Inches(float(margenes.get("inferior", 0.4)))
    ancho = int(prs.slide_width) - int(izquierda) - int(derecha)
    arriba = max(
        int(Inches(float(margenes.get("superior", 0.4)))),
        int(titulo.top + titulo.height) + int(Inches(0.1)),
    )
    alto = int(prs.slide_height) - arriba - int(inferior) - int(Inches(0.5))
    area = (int(izquierda), arriba, ancho, max(1, alto))
    _insertar_tabla(slide, area, nombre_tabla, definicion, datos, branding, metricas)
    if nota:
        margenes = branding.get("margenes_pulgadas", {})
        izquierda = Inches(float(margenes.get("izquierda", 0.6)))
        ancho = int(prs.slide_width) - 2 * int(izquierda)
        caja = slide.shapes.add_textbox(
            izquierda,
            area[1] + area[3] + int(Inches(0.05)),
            ancho,
            Inches(0.4),
        )
        _escribir_texto(
            caja,
            nota,
            branding,
            tamano=float(branding.get("tipografia", {}).get("tamano_pie", 9)),
            color="texto",
        )
    _pie_fuente(slide, prs, branding, estilos, corte)


def _crear_cierre(
    prs: Presentation,
    branding: Mapping[str, Any],
    seccion: Mapping[str, Any],
    defecto: str,
) -> None:
    slide = prs.slides.add_slide(_layout(prs, "Cierre"))
    titulo = _titulo_texto(seccion, defecto, "siguientes_pasos")
    _validar_sin_marcadores(titulo, "los siguientes pasos")
    vinetas = _leer_vinetas(seccion, branding, "siguientes_pasos")
    for vineta in vinetas:
        _validar_sin_marcadores(vineta, "los siguientes pasos")

    margenes = branding.get("margenes_pulgadas", {})
    izquierda = Inches(float(margenes.get("izquierda", 0.6)))
    superior = Inches(float(margenes.get("superior", 0.4)))
    ancho = int(prs.slide_width) - 2 * int(izquierda)

    caja_titulo = slide.shapes.add_textbox(izquierda, superior, ancho, Inches(1.2))
    _escribir_titulo(caja_titulo, titulo, branding, tamano=30)

    alto = int(prs.slide_height) - int(superior) - int(Inches(1.8))
    caja = slide.shapes.add_textbox(izquierda, superior + Inches(1.3), ancho, max(1, alto))
    _escribir_vinetas(caja, vinetas, branding)
    _barra_acento(slide, prs, branding)


# ---------------------------------------------------------------------------
# Barrido final de marcadores
# ---------------------------------------------------------------------------


def _iterar_formas(shapes: Any) -> Any:
    for forma in shapes:
        if forma.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _iterar_formas(forma.shapes)
        else:
            yield forma


def _sustituir_en_presentacion(
    prs: Presentation,
    metricas: Mapping[str, float | int],
) -> None:
    for slide in prs.slides:
        for forma in _iterar_formas(slide.shapes):
            if forma.has_text_frame:
                for parrafo in forma.text_frame.paragraphs:
                    for run in parrafo.runs:
                        if "{{m." in run.text:
                            run.text = sustituir_marcadores(run.text, metricas)
            if getattr(forma, "has_table", False) and forma.has_table:
                for fila in forma.table.rows:
                    for celda in fila.cells:
                        for parrafo in celda.text_frame.paragraphs:
                            for run in parrafo.runs:
                                if "{{m." in run.text:
                                    run.text = sustituir_marcadores(run.text, metricas)


def _textos_presentacion(prs: Presentation) -> list[str]:
    textos: list[str] = []
    for slide in prs.slides:
        for forma in _iterar_formas(slide.shapes):
            if forma.has_text_frame:
                textos.extend(parrafo.text for parrafo in forma.text_frame.paragraphs)
            if getattr(forma, "has_table", False) and forma.has_table:
                for fila in forma.table.rows:
                    for celda in fila.cells:
                        textos.extend(parrafo.text for parrafo in celda.text_frame.paragraphs)
            if getattr(forma, "has_chart", False) and forma.has_chart:
                for plot in forma.chart.plots:
                    for serie in plot.series:
                        if serie.name:
                            textos.append(str(serie.name))
                    try:
                        textos.extend(str(categoria) for categoria in plot.categories)
                    except (AttributeError, ValueError):
                        continue
    return textos


def _verificar_sin_marcadores(prs: Presentation) -> None:
    restos = [texto for texto in _textos_presentacion(prs) if "{{m." in texto]
    if restos:
        raise PptxError(
            "E-PPT-030",
            f"quedaron {len(restos)} marcadores sin sustituir: {restos[:3]}",
        )


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def pptx_ensamblar(
    skill_ruta: Path | str,
    salida: Path | str,
    *,
    metricas: Mapping[str, float | int],
    contenido: Mapping[str, Mapping[str, Any]] | None = None,
    graficas: Mapping[str, DatosGrafica] | None = None,
    tablas: Mapping[str, DatosTabla] | None = None,
    plantilla: Path | str | None = None,
    corte: str | None = None,
) -> Path:
    """Ensambla las siete diapositivas del contrato y devuelve la ruta del archivo.

    Parameters
    ----------
    skill_ruta:
        Directorio de la skill aprobada (``SKILL.md``, ``charts.yaml`` y, si
        existe, ``template.pptx``).
    salida:
        Ruta del ``.pptx`` a generar.
    metricas:
        Diccionario con claves ``{{m.<metrica>.<cohorte>.<MOBn>|<formato>}}``
        y valores numéricos, tal como lo devuelve ``metricas_calcular``.
    contenido:
        Texto ya redactado por sección. Claves admitidas: ``portada``,
        ``resumen_ejecutivo``, ``volumen_cosecha``, ``curvas_over30``,
        ``curvas_ncl``, ``fuera_intervalo`` y ``siguientes_pasos``.
    graficas:
        Ruta de imagen (gráficas de matplotlib) o mapa ``categorias``/``series``
        (gráficas nativas) por nombre de gráfica de ``charts.yaml``.
    tablas:
        Mapa ``columnas``/``filas`` y, opcionalmente, ``formatos`` por nombre de
        tabla de ``charts.yaml``.
    plantilla:
        Plantilla explícita. Si se omite, se usa ``template.pptx`` de la skill o
        el fallback de ``config/branding/``.
    corte:
        Fecha de corte en formato ``AAAAMM``. Si se omite, se infiere de las
        claves de ``metricas``.

    Returns
    -------
    Path
        Ruta absoluta del archivo generado.
    """
    ruta_skill = Path(skill_ruta).expanduser().resolve()
    if not ruta_skill.is_dir():
        raise PptxError("E-PPT-001", f"no existe la skill {ruta_skill}", "skill")
    destino = Path(salida).expanduser()
    if destino.suffix.lower() != ".pptx":
        raise PptxError("E-PPT-050", f"la salida debe terminar en .pptx: {destino}", "salida")
    destino = destino.resolve()

    if not isinstance(metricas, Mapping):
        raise PptxError("E-PPT-030", "metricas debe ser un mapa de marcador a valor")
    contenido = contenido or {}
    graficas = graficas or {}
    tablas = tablas or {}
    if not isinstance(contenido, Mapping) or not isinstance(graficas, Mapping):
        raise PptxError("E-PPT-020", "contenido y graficas deben ser mapas")
    if not isinstance(tablas, Mapping):
        raise PptxError("E-PPT-043", "tablas debe ser un mapa")

    desconocidas = set(contenido) - CLAVES_SECCION
    if desconocidas:
        raise PptxError(
            "E-PPT-020",
            f"secciones de contenido desconocidas: {sorted(desconocidas)}",
        )

    secciones_skill = _leer_secciones_skill(ruta_skill)
    titulo_skill = _leer_titulo_skill(ruta_skill)
    branding = _cargar_branding()
    definiciones, estilos = _cargar_charts(ruta_skill)
    ruta_plantilla = _resolver_plantilla(ruta_skill, plantilla)
    prs = _abrir_plantilla(ruta_plantilla)
    _vaciar_diapositivas(prs)

    if corte is None:
        for clave in metricas:
            coincidencia = PATRON_MARCADOR.fullmatch(str(clave))
            if coincidencia is not None and coincidencia.group("cohorte") != "todas":
                corte = coincidencia.group("cohorte")
                break

    _crear_portada(
        prs,
        branding,
        _seccion(contenido, "portada"),
        secciones_skill[0].titulo,
        corte,
        titulo_skill,
    )
    _crear_resumen(
        prs,
        branding,
        _seccion(contenido, "resumen_ejecutivo"),
        secciones_skill[1].titulo,
    )
    _crear_grafica(
        prs,
        branding,
        estilos,
        definiciones,
        _seccion(contenido, "volumen_cosecha"),
        secciones_skill[2].titulo,
        "volumen_cosecha",
        graficas.get("volumen_cosecha"),
        metricas,
        corte,
    )
    _crear_grafica(
        prs,
        branding,
        estilos,
        definiciones,
        _seccion(contenido, "curvas_over30"),
        secciones_skill[3].titulo,
        "curvas_over30",
        graficas.get("curvas_over30"),
        metricas,
        corte,
    )
    _crear_grafica(
        prs,
        branding,
        estilos,
        definiciones,
        _seccion(contenido, "curvas_ncl"),
        secciones_skill[4].titulo,
        "curvas_ncl",
        graficas.get("curvas_ncl"),
        metricas,
        corte,
    )
    _crear_tabla(
        prs,
        branding,
        estilos,
        definiciones,
        _seccion(contenido, "fuera_intervalo"),
        secciones_skill[5].titulo,
        "fuera_intervalo",
        tablas.get("fuera_intervalo"),
        metricas,
        corte,
    )
    _crear_cierre(
        prs,
        branding,
        _seccion(contenido, "siguientes_pasos"),
        secciones_skill[6].titulo,
    )

    _sustituir_en_presentacion(prs, metricas)
    _verificar_sin_marcadores(prs)

    if len(prs.slides) != len(SECCIONES):
        raise PptxError(
            "E-PPT-060",
            f"se generaron {len(prs.slides)} diapositivas; se esperaban {len(SECCIONES)}",
        )

    prs.core_properties.title = str(contenido.get("portada", {}).get("titulo", titulo_skill))
    prs.core_properties.author = "basecards"
    destino.parent.mkdir(parents=True, exist_ok=True)
    try:
        prs.save(str(destino))
    except Exception as exc:
        raise PptxError("E-PPT-050", f"no se pudo guardar {destino}: {exc}", "salida") from exc
    return destino
