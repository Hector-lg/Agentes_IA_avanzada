"""Pruebas del ensamblador de presentaciones ``.pptx``."""

from __future__ import annotations

import base64
import shutil
from pathlib import Path

import pytest
from pptx import Presentation

from basecards.herramientas.pptx_ensamblar import (
    PptxError,
    formatear_valor,
    pptx_ensamblar,
    sustituir_marcadores,
)

RAIZ = Path(__file__).resolve().parents[2]
SKILL = RAIZ / "skills" / "aprobadas" / "seguimiento-cosechas"
PLANTILLA = RAIZ / "config" / "branding" / "template_banamex.pptx"

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

LAYOUTS = [
    "Portada",
    "Dos_Columnas",
    "Titulo_y_Grafica",
    "Titulo_y_Grafica",
    "Titulo_y_Grafica",
    "Tabla",
    "Cierre",
]


def _metricas() -> dict[str, float | int]:
    return {
        "{{m.tasa_over30.202401.MOB12|pct2}}": 0.0213,
        "{{m.tasa_ncl.202401.MOB12|pct2}}": 0.0345,
        "{{m.volumen.202401.MOB0|int}}": 1200,
        "{{m.saldo.202401.MOB12|mxn_millones}}": 12_400_000,
    }


def _contenido() -> dict[str, dict[str, object]]:
    return {
        "portada": {
            "titulo": "Seguimiento de cosechas",
            "subtitulo": "Corte 202401",
        },
        "resumen_ejecutivo": {
            "titulo": "La cosecha 202401 concentra la mayor desviación de Over30",
            "vinetas": [
                "La tasa Over30 de la cohorte es {{m.tasa_over30.202401.MOB12|pct2}}.",
                "La tasa NCL observada es {{m.tasa_ncl.202401.MOB12|pct2}}.",
                "El saldo expuesto asciende a {{m.saldo.202401.MOB12|mxn_millones}}.",
            ],
        },
        "volumen_cosecha": {
            "titulo": "El volumen se concentra en las cosechas recientes",
        },
        "curvas_over30": {
            "titulo": "La curva Over30 supera la banda esperada desde MOB 8",
        },
        "curvas_ncl": {
            "titulo": "La curva NCL se mantiene dentro del intervalo esperado",
        },
        "fuera_intervalo": {
            "titulo": "Diez cohortes quedaron fuera del intervalo esperado",
        },
        "siguientes_pasos": {
            "titulo": "Siguientes pasos",
            "vinetas": [
                "Revisar con Riesgo las cohortes fuera de intervalo.",
                "Actualizar el modelo champion con el corte más reciente.",
            ],
        },
    }


def _graficas(tmp_path: Path) -> dict[str, object]:
    imagen = tmp_path / "curva.png"
    imagen.write_bytes(PNG_1X1)
    return {
        "volumen_cosecha": {
            "categorias": ["202401", "202402"],
            "series": {"Cuentas": [1200, 900]},
        },
        "curvas_over30": imagen,
        "curvas_ncl": imagen,
    }


def _tablas() -> dict[str, object]:
    filas = [[202401, "DARIA 1.6", mob, 0.051, 0.049, 0.002] for mob in range(12)]
    return {
        "fuera_intervalo": {
            "columnas": ["VINTAGE", "STRATEGY", "MOB", "observado", "q50", "desviacion"],
            "filas": filas,
            "formatos": {"observado": "pct2", "q50": "pct2", "desviacion": "pct1"},
        }
    }


def _ensamblar(tmp_path: Path, **cambios: object) -> Path:
    argumentos: dict[str, object] = {
        "metricas": _metricas(),
        "contenido": _contenido(),
        "graficas": _graficas(tmp_path),
        "tablas": _tablas(),
    }
    argumentos.update(cambios)
    return pptx_ensamblar(SKILL, tmp_path / "deck.pptx", **argumentos)


def _textos(prs: Presentation) -> list[str]:
    textos: list[str] = []
    for slide in prs.slides:
        for forma in slide.shapes:
            if forma.has_text_frame:
                textos.append(forma.text_frame.text)
            if getattr(forma, "has_table", False) and forma.has_table:
                for fila in forma.table.rows:
                    for celda in fila.cells:
                        textos.append(celda.text_frame.text)
    return textos


@pytest.mark.parametrize(
    ("valor", "formato", "esperado"),
    [
        (0.0213, "pct1", "2.1%"),
        (0.0213, "pct2", "2.13%"),
        (1_234_567, "mxn", "$1,234,567"),
        (12_400_000, "mxn_millones", "$12.4 M"),
        (1234, "int", "1,234"),
    ],
)
def test_formatear_valor(valor: float, formato: str, esperado: str) -> None:
    assert formatear_valor(valor, formato) == esperado


def test_formatear_valor_rechaza_formato_desconocido() -> None:
    with pytest.raises(PptxError) as exc:
        formatear_valor(0.1, "porcentaje")
    assert exc.value.codigo == "E-PPT-030"


def test_sustituir_marcadores_reemplaza_varios() -> None:
    texto = (
        "Over30 {{m.tasa_over30.202401.MOB12|pct2}} y saldo {{m.saldo.202401.MOB12|mxn_millones}}"
    )
    assert sustituir_marcadores(texto, _metricas()) == "Over30 2.13% y saldo $12.4 M"


def test_sustituir_marcadores_falla_si_falta_el_valor() -> None:
    with pytest.raises(PptxError) as exc:
        sustituir_marcadores("{{m.tasa_over30.202401.MOB12|pct2}}", {})
    assert exc.value.codigo == "E-PPT-030"
    assert "{{m.tasa_over30.202401.MOB12|pct2}}" in exc.value.mensaje


def test_genera_siete_diapositivas_en_orden(tmp_path: Path) -> None:
    salida = _ensamblar(tmp_path)
    assert salida.is_file()

    prs = Presentation(str(salida))
    assert len(prs.slides) == 7
    assert [slide.slide_layout.name for slide in prs.slides] == LAYOUTS


def test_no_quedan_marcadores_y_formatea_cifras(tmp_path: Path) -> None:
    salida = _ensamblar(tmp_path)
    prs = Presentation(str(salida))
    textos = _textos(prs)
    assert not any("{{m." in texto for texto in textos)
    assert any("2.13%" in texto for texto in textos)
    assert any("$12.4 M" in texto for texto in textos)


def test_inserta_grafica_nativa_imagen_y_tabla(tmp_path: Path) -> None:
    salida = _ensamblar(tmp_path)
    prs = Presentation(str(salida))

    assert any(forma.has_chart for forma in prs.slides[2].shapes)
    assert any(forma.shape_type == 13 for forma in prs.slides[3].shapes)
    assert any(forma.shape_type == 13 for forma in prs.slides[4].shapes)

    forma_tabla = next(
        forma for forma in prs.slides[5].shapes if getattr(forma, "has_table", False)
    )
    titulo = next(
        forma for forma in prs.slides[5].placeholders if forma.placeholder_format.idx == 0
    )
    assert forma_tabla.top >= titulo.top + titulo.height
    tabla = forma_tabla.table
    assert len(tabla.rows) == 11
    assert len(tabla.columns) == 6
    textos = _textos(prs)
    assert any("5.10%" in texto for texto in textos)


def test_falla_si_un_marcador_no_tiene_valor(tmp_path: Path) -> None:
    metricas = _metricas()
    del metricas["{{m.tasa_over30.202401.MOB12|pct2}}"]
    with pytest.raises(PptxError) as exc:
        _ensamblar(tmp_path, metricas=metricas)
    assert exc.value.codigo == "E-PPT-030"


def test_falla_con_marcador_malformado(tmp_path: Path) -> None:
    contenido = _contenido()
    contenido["resumen_ejecutivo"]["vinetas"] = [
        "Una cifra con formato inválido {{m.tasa_over30.202401.MOB12|porcentaje}}."
    ]
    with pytest.raises(PptxError) as exc:
        _ensamblar(tmp_path, contenido=contenido)
    assert exc.value.codigo == "E-PPT-030"


def test_falla_si_falta_una_grafica(tmp_path: Path) -> None:
    graficas = _graficas(tmp_path)
    del graficas["curvas_ncl"]
    with pytest.raises(PptxError) as exc:
        _ensamblar(tmp_path, graficas=graficas)
    assert exc.value.codigo == "E-PPT-041"


def test_falla_si_falta_una_tabla(tmp_path: Path) -> None:
    with pytest.raises(PptxError) as exc:
        _ensamblar(tmp_path, tablas={})
    assert exc.value.codigo == "E-PPT-041"


def test_portada_no_admite_marcadores(tmp_path: Path) -> None:
    contenido = _contenido()
    contenido["portada"]["titulo"] = "{{m.volumen.202401.MOB0|int}} cuentas"
    with pytest.raises(PptxError) as exc:
        _ensamblar(tmp_path, contenido=contenido)
    assert exc.value.codigo == "E-PPT-031"


def test_cierre_no_admite_marcadores(tmp_path: Path) -> None:
    contenido = _contenido()
    contenido["siguientes_pasos"]["vinetas"] = ["Revisar {{m.volumen.202401.MOB0|int}} cuentas."]
    with pytest.raises(PptxError) as exc:
        _ensamblar(tmp_path, contenido=contenido)
    assert exc.value.codigo == "E-PPT-031"


def test_resumen_limita_el_numero_de_vinetas(tmp_path: Path) -> None:
    contenido = _contenido()
    contenido["resumen_ejecutivo"]["vinetas"] = ["Mensaje"] * 5
    with pytest.raises(PptxError) as exc:
        _ensamblar(tmp_path, contenido=contenido)
    assert exc.value.codigo == "E-PPT-032"


def test_resumen_limita_las_palabras_por_vineta(tmp_path: Path) -> None:
    contenido = _contenido()
    contenido["resumen_ejecutivo"]["vinetas"] = ["palabra " * 41]
    with pytest.raises(PptxError) as exc:
        _ensamblar(tmp_path, contenido=contenido)
    assert exc.value.codigo == "E-PPT-033"


def test_rechaza_seccion_desconocida(tmp_path: Path) -> None:
    contenido = _contenido()
    contenido["anexos"] = {"titulo": "Anexos"}
    with pytest.raises(PptxError) as exc:
        _ensamblar(tmp_path, contenido=contenido)
    assert exc.value.codigo == "E-PPT-020"


def test_rechaza_plantilla_sin_layouts(tmp_path: Path) -> None:
    plantilla = tmp_path / "simple.pptx"
    Presentation().save(str(plantilla))
    with pytest.raises(PptxError) as exc:
        _ensamblar(tmp_path, plantilla=plantilla)
    assert exc.value.codigo == "E-PPT-003"


def test_prefiere_la_plantilla_de_la_skill(tmp_path: Path) -> None:
    skill = tmp_path / "skill"
    shutil.copytree(SKILL, skill)
    plantilla = skill / "template.pptx"
    shutil.copyfile(PLANTILLA, plantilla)
    prs = Presentation(str(plantilla))
    prs.core_properties.keywords = "plantilla-de-la-skill"
    prs.save(str(plantilla))

    salida = pptx_ensamblar(
        skill,
        tmp_path / "deck.pptx",
        metricas=_metricas(),
        contenido=_contenido(),
        graficas=_graficas(tmp_path),
        tablas=_tablas(),
    )
    resultado = Presentation(str(salida))
    assert resultado.core_properties.keywords == "plantilla-de-la-skill"


def test_rechaza_salida_sin_extension_pptx(tmp_path: Path) -> None:
    with pytest.raises(PptxError) as exc:
        pptx_ensamblar(
            SKILL,
            tmp_path / "deck.pdf",
            metricas=_metricas(),
            contenido=_contenido(),
            graficas=_graficas(tmp_path),
            tablas=_tablas(),
        )
    assert exc.value.codigo == "E-PPT-050"
