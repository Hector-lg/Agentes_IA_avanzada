"""Pruebas del ensamblador de memos ``.docx`` (RC-05)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from docx import Document

from basecards.herramientas.docx_ensamblar import DocxError, docx_ensamblar

RAIZ = Path(__file__).resolve().parents[2]
PLANTILLA = RAIZ / "config" / "branding" / "template_banamex.docx"


def _metricas() -> dict[str, float | int]:
    return {
        "{{m.tasa_ncl.202401.MOB12|pct2}}": 0.0345,
        "{{m.saldo.202401.MOB12|mxn_millones}}": 12_400_000,
        "{{m.volumen.202401.MOB0|int}}": 1200,
    }


def _analisis() -> dict[str, object]:
    return {
        "titulo": "Memo de riesgo",
        "nivel_riesgo": "alto",
        "rentabilidad_esperada": 0.124,
        "confianza": 0.82,
        "accion_sugerida": "Escalar a Riesgo de Modelos",
        "requiere_revision_humana": True,
        "corte": "202401",
        "fecha": "26/11/2026",
        "borrador_memo": (
            "## Resumen\n"
            "La cosecha 202401 alcanzó una tasa NCL de "
            "{{m.tasa_ncl.202401.MOB12|pct2}} y un saldo de "
            "{{m.saldo.202401.MOB12|mxn_millones}}.\n"
            "\n"
            "- Primer hallazgo con **negritas**.\n"
            "- Segundo hallazgo con *cursivas*.\n"
            "\n"
            "### Recomendación\n"
            "1. Revisar con Riesgo las cohortes fuera de intervalo.\n"
            "2. Actualizar el modelo champion.\n"
        ),
        "evidencia": [
            {
                "herramienta": "cubo_metricas_cohorte",
                "hallazgo": "NCL observada de {{m.tasa_ncl.202401.MOB12|pct2}}",
                "llamada_id": "L-001",
            },
            {
                "herramienta": "modelo_curva_esperada",
                "hallazgo": "La banda q95 se rebasa desde MOB 8.",
                "llamada_id": "L-002",
            },
        ],
        "drivers": [
            {"feature": "SCORE_ORD", "contribucion": 0.0031},
            {"feature": "CHANNEL=Internet", "contribucion": -0.0012},
        ],
    }


def _ensamblar(tmp_path: Path, **cambios: object) -> Path:
    argumentos: dict[str, object] = {
        "analisis": _analisis(),
        "metricas": _metricas(),
    }
    argumentos.update(cambios)
    return docx_ensamblar(tmp_path / "memo.docx", **argumentos)


def _textos(doc: Document) -> list[str]:
    textos = [parrafo.text for parrafo in doc.paragraphs]
    for tabla in doc.tables:
        for fila in tabla.rows:
            for celda in fila.cells:
                textos.append(celda.text)
    for seccion in doc.sections:
        textos.extend(parrafo.text for parrafo in seccion.header.paragraphs)
        textos.extend(parrafo.text for parrafo in seccion.footer.paragraphs)
    return textos


def test_genera_un_docx_que_abre_con_python_docx(tmp_path: Path) -> None:
    salida = _ensamblar(tmp_path)
    assert salida.is_file()
    assert salida.suffix == ".docx"
    doc = Document(str(salida))
    assert doc.paragraphs[0].text == "Memo de riesgo"


def test_incluye_titulo_subtitulo_y_metadatos(tmp_path: Path) -> None:
    doc = Document(str(_ensamblar(tmp_path)))
    textos = _textos(doc)
    assert "Memo de riesgo" in textos
    assert "Corte 202401 · 26/11/2026" in textos
    assert "Para" in textos
    assert "Analista de riesgo" in textos
    assert "Copiloto de análisis" in textos


def test_sustituye_marcadores_y_formatea_cifras(tmp_path: Path) -> None:
    doc = Document(str(_ensamblar(tmp_path)))
    textos = _textos(doc)
    assert not any("{{m." in texto for texto in textos)
    assert any("3.45%" in texto for texto in textos)
    assert any("$12.4 M" in texto for texto in textos)


def test_resumen_muestra_riesgo_rentabilidad_confianza_y_accion(tmp_path: Path) -> None:
    doc = Document(str(_ensamblar(tmp_path)))
    tabla = doc.tables[0]
    valores = [celda.text for celda in tabla.rows[1].cells]
    assert valores == ["Alto", "12.40%", "82.0%", "Escalar a Riesgo de Modelos"]


def test_incluye_evidencia_y_drivers(tmp_path: Path) -> None:
    doc = Document(str(_ensamblar(tmp_path)))
    textos = _textos(doc)
    assert "cubo_metricas_cohorte" in textos
    assert "L-001" in textos
    assert "SCORE_ORD" in textos
    assert "+0.31%" in textos
    assert "-0.12%" in textos


def test_renderiza_markdown_sencillo(tmp_path: Path) -> None:
    doc = Document(str(_ensamblar(tmp_path)))
    estilos = {parrafo.style.name for parrafo in doc.paragraphs}
    assert "Heading 1" in estilos
    assert "Heading 2" in estilos
    assert "List Bullet" in estilos
    assert "List Number" in estilos
    assert "Primer hallazgo con negritas." in [p.text for p in doc.paragraphs]
    assert "Segundo hallazgo con cursivas." in [p.text for p in doc.paragraphs]


def test_conserva_los_estilos_institucionales(tmp_path: Path) -> None:
    doc = Document(str(_ensamblar(tmp_path)))
    assert doc.styles["Normal"].font.name == "Liberation Sans"
    assert doc.styles["Normal"].font.size.pt == 11
    assert doc.styles["Title"].font.size.pt == 20
    seccion = doc.sections[0]
    assert seccion.page_width.inches == 8.5
    assert seccion.left_margin.inches == 0.9
    assert "Banamex" in seccion.header.paragraphs[0].text
    assert "Memo RC-05" in seccion.footer.paragraphs[0].text


def test_revision_humana_agrega_aviso(tmp_path: Path) -> None:
    doc = Document(str(_ensamblar(tmp_path)))
    assert "Requiere revisión humana antes de distribuirse." in _textos(doc)


def test_sin_evidencia_no_crea_la_tabla_de_evidencia(tmp_path: Path) -> None:
    analisis = _analisis()
    analisis["evidencia"] = []
    analisis["drivers"] = []
    analisis["accion_sugerida"] = None
    analisis["requiere_revision_humana"] = False
    doc = Document(str(_ensamblar(tmp_path, analisis=analisis)))
    assert len(doc.tables) == 2  # resumen + metadatos


def test_infiere_el_corte_de_las_metricas(tmp_path: Path) -> None:
    analisis = _analisis()
    del analisis["corte"]
    doc = Document(str(_ensamblar(tmp_path, analisis=analisis)))
    assert any("Corte 202401" in texto for texto in _textos(doc))


def test_acepta_memo_como_lista_de_parrafos(tmp_path: Path) -> None:
    analisis = _analisis()
    analisis["borrador_memo"] = ["Primer párrafo.", "Segundo párrafo."]
    doc = Document(str(_ensamblar(tmp_path, analisis=analisis)))
    textos = _textos(doc)
    assert "Primer párrafo." in textos
    assert "Segundo párrafo." in textos


def test_usa_una_plantilla_explicita(tmp_path: Path) -> None:
    plantilla = tmp_path / "plantilla.docx"
    shutil.copyfile(PLANTILLA, plantilla)
    doc = Document(str(plantilla))
    doc.sections[0].footer.paragraphs[0].text = "Plantilla propia"
    doc.save(str(plantilla))

    salida = _ensamblar(tmp_path, plantilla=plantilla)
    resultado = Document(str(salida))
    assert "Plantilla propia" in resultado.sections[0].footer.paragraphs[0].text


def test_falla_si_un_marcador_no_tiene_valor(tmp_path: Path) -> None:
    metricas = _metricas()
    del metricas["{{m.tasa_ncl.202401.MOB12|pct2}}"]
    with pytest.raises(DocxError) as exc:
        _ensamblar(tmp_path, metricas=metricas)
    assert exc.value.codigo == "E-DOC-030"


def test_falla_con_marcador_malformado(tmp_path: Path) -> None:
    analisis = _analisis()
    analisis["borrador_memo"] = "Cifra {{m.tasa_ncl.202401.MOB12|porcentaje}}."
    with pytest.raises(DocxError) as exc:
        _ensamblar(tmp_path, analisis=analisis)
    assert exc.value.codigo == "E-DOC-030"


def test_rechaza_salida_sin_extension_docx(tmp_path: Path) -> None:
    with pytest.raises(DocxError) as exc:
        docx_ensamblar(tmp_path / "memo.pdf", analisis=_analisis(), metricas=_metricas())
    assert exc.value.codigo == "E-DOC-050"


def test_rechaza_json_sin_borrador_memo(tmp_path: Path) -> None:
    analisis = _analisis()
    del analisis["borrador_memo"]
    with pytest.raises(DocxError) as exc:
        _ensamblar(tmp_path, analisis=analisis)
    assert exc.value.codigo == "E-DOC-020"


def test_rechaza_json_sin_nivel_de_riesgo(tmp_path: Path) -> None:
    analisis = _analisis()
    del analisis["nivel_riesgo"]
    with pytest.raises(DocxError) as exc:
        _ensamblar(tmp_path, analisis=analisis)
    assert exc.value.codigo == "E-DOC-020"


def test_rechaza_confianza_fuera_de_rango(tmp_path: Path) -> None:
    analisis = _analisis()
    analisis["confianza"] = 1.5
    with pytest.raises(DocxError) as exc:
        _ensamblar(tmp_path, analisis=analisis)
    assert exc.value.codigo == "E-DOC-020"


def test_rechaza_evidencia_invalida(tmp_path: Path) -> None:
    analisis = _analisis()
    analisis["evidencia"] = [123]
    with pytest.raises(DocxError) as exc:
        _ensamblar(tmp_path, analisis=analisis)
    assert exc.value.codigo == "E-DOC-040"


def test_rechaza_drivers_sin_feature(tmp_path: Path) -> None:
    analisis = _analisis()
    analisis["drivers"] = [{"contribucion": 0.1}]
    with pytest.raises(DocxError) as exc:
        _ensamblar(tmp_path, analisis=analisis)
    assert exc.value.codigo == "E-DOC-040"


def test_rechaza_plantilla_inexistente(tmp_path: Path) -> None:
    with pytest.raises(DocxError) as exc:
        _ensamblar(tmp_path, plantilla=tmp_path / "no-existe.docx")
    assert exc.value.codigo == "E-DOC-002"
