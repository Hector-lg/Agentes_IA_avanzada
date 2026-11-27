"""Pruebas de la API pública del paquete ``basecards.herramientas``."""

from __future__ import annotations

from basecards import herramientas
from basecards.herramientas import (
    ConversionError,
    DocxError,
    MetricaError,
    PptxError,
    ResultadoComando,
    SandboxRunner,
    XlsxError,
    compilar_expresion,
    compilar_metrica,
    documento_convertir,
    documento_miniaturas,
    docx_ensamblar,
    formatear_valor,
    metricas_calcular,
    pptx_ensamblar,
    skills_leer,
    skills_listar,
    sustituir_marcadores,
    xlsx_ensamblar,
)


def test_exporta_la_herramienta_de_metricas() -> None:
    assert callable(metricas_calcular)
    assert callable(compilar_expresion)
    assert callable(compilar_metrica)
    assert issubclass(MetricaError, Exception)


def test_exporta_el_catalogo_de_skills() -> None:
    assert callable(skills_leer)
    assert callable(skills_listar)


def test_exporta_el_ensamblador_de_pptx() -> None:
    assert callable(pptx_ensamblar)
    assert callable(formatear_valor)
    assert callable(sustituir_marcadores)
    assert issubclass(PptxError, Exception)


def test_exporta_el_ensamblador_de_docx() -> None:
    assert callable(docx_ensamblar)
    assert issubclass(DocxError, Exception)


def test_exporta_el_ensamblador_de_xlsx() -> None:
    assert callable(xlsx_ensamblar)
    assert issubclass(XlsxError, Exception)


def test_exporta_las_herramientas_de_conversion() -> None:
    assert callable(documento_convertir)
    assert callable(documento_miniaturas)
    assert issubclass(ConversionError, Exception)
    assert ResultadoComando(codigo=0).codigo == 0
    assert SandboxRunner is not None


def test_dir_incluye_los_simbolos_publicos() -> None:
    visibles = set(dir(herramientas))
    assert {"metricas_calcular", "MetricaError", "skills_leer"} <= visibles
    assert {"pptx_ensamblar", "PptxError"} <= visibles
    assert {"docx_ensamblar", "DocxError"} <= visibles
    assert {"xlsx_ensamblar", "XlsxError"} <= visibles
    assert {
        "documento_convertir",
        "documento_miniaturas",
        "ConversionError",
        "ResultadoComando",
        "SandboxRunner",
    } <= visibles
