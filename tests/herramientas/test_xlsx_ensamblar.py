"""Pruebas del ensamblador de anexos ``.xlsx``."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest
from openpyxl import load_workbook

from basecards.herramientas.xlsx_ensamblar import XlsxError, xlsx_ensamblar

RAIZ = Path(__file__).resolve().parents[2]
COLUMNAS = ["VINTAGE", "MOB", "TASA_NCL", "NCLAMTS", "ENRAMTS", "SALDO"]


def _datos() -> dict[str, object]:
    return {
        "columnas": COLUMNAS,
        "filas": [
            [202401, 6, 0.0300, 120_000, 4_000_000, 12_400_000],
            [202401, 9, 0.0400, 180_000, 4_500_000, 13_100_000],
            [202402, 6, 0.0237, 90_000, 3_800_000, 11_900_000],
        ],
        "formatos": {
            "TASA_NCL": "pct",
            "NCLAMTS": "mxn",
            "ENRAMTS": "mxn",
            "SALDO": "mxn",
        },
    }


def _resumen() -> list[dict[str, object]]:
    return [
        {
            "nombre": "Tasa NCL ponderada",
            "numerador": "NCLAMTS",
            "denominador": "ENRAMTS",
            "formato": "pct",
            "descripcion": "NCL / exposición",
        },
        {
            "nombre": "Saldo total",
            "numerador": "SALDO",
            "formato": "mxn",
        },
    ]


def _metricas() -> dict[str, float | int]:
    return {"{{m.volumen.202401.MOB0|int}}": 1200}


def _ensamblar(tmp_path: Path, **cambios: object) -> Path:
    argumentos: dict[str, object] = {
        "datos": _datos(),
        "resumen": _resumen(),
        "metricas": _metricas(),
        "corte": "202401",
    }
    argumentos.update(cambios)
    return xlsx_ensamblar(tmp_path / "anexo.xlsx", **argumentos)


def test_genera_hojas_datos_y_resumen(tmp_path: Path) -> None:
    salida = _ensamblar(tmp_path)
    assert salida.is_file()
    libro = load_workbook(str(salida))
    assert libro.sheetnames == ["Datos", "Resumen"]


def test_datos_congela_encabezados_y_tiene_autofiltro(tmp_path: Path) -> None:
    hoja = load_workbook(str(_ensamblar(tmp_path)))["Datos"]
    assert hoja.freeze_panes == "A2"
    assert hoja.auto_filter.ref == "A1:F4"
    assert [celda.value for celda in hoja[1]] == COLUMNAS


def test_resumen_congela_encabezados(tmp_path: Path) -> None:
    hoja = load_workbook(str(_ensamblar(tmp_path)))["Resumen"]
    assert hoja.freeze_panes == "A5"
    assert [hoja.cell(4, columna).value for columna in range(1, 6)] == [
        "Métrica",
        "Numerador",
        "Denominador",
        "Valor",
        "Descripción",
    ]


def test_formula_de_razon_de_sumas_y_valor_cacheado(tmp_path: Path) -> None:
    salida = _ensamblar(tmp_path)
    formulas = load_workbook(str(salida), data_only=False)["Resumen"]
    valores = load_workbook(str(salida), data_only=True)["Resumen"]

    assert formulas["D5"].value == "=SUM(Datos!D2:D4)/SUM(Datos!E2:E4)"
    assert formulas["D5"].number_format == "0.00%"
    assert valores["D5"].value == pytest.approx(390_000 / 12_300_000)


def test_formula_de_suma_monetaria(tmp_path: Path) -> None:
    salida = _ensamblar(tmp_path)
    formulas = load_workbook(str(salida), data_only=False)["Resumen"]
    valores = load_workbook(str(salida), data_only=True)["Resumen"]

    assert formulas["D6"].value == "=SUM(Datos!F2:F4)"
    assert formulas["D6"].number_format == "$#,##0"
    assert valores["D6"].value == pytest.approx(37_400_000)


def test_formatos_por_columna_en_datos(tmp_path: Path) -> None:
    hoja = load_workbook(str(_ensamblar(tmp_path)))["Datos"]
    assert hoja["C2"].number_format == "0.00%"
    assert hoja["D2"].number_format == "$#,##0"
    assert hoja["E2"].number_format == "$#,##0"
    assert hoja["F2"].number_format == "$#,##0"


def test_sustituye_marcadores_en_datos_y_resumen(tmp_path: Path) -> None:
    datos = _datos()
    datos["columnas"] = [*COLUMNAS, "ETIQUETA"]
    for fila in datos["filas"]:
        fila.append("Corte {{m.volumen.202401.MOB0|int}}")
    resumen = _resumen()
    resumen[0]["nombre"] = "Tasa NCL {{m.volumen.202401.MOB0|int}}"

    salida = _ensamblar(tmp_path, datos=datos, resumen=resumen)
    libro = load_workbook(str(salida))
    assert libro["Datos"]["G2"].value == "Corte 1,200"
    assert libro["Resumen"]["A5"].value == "Tasa NCL 1,200"
    assert not any(
        isinstance(celda.value, str) and "{{m." in celda.value
        for hoja in libro.worksheets
        for fila in hoja.iter_rows()
        for celda in fila
    )


def test_falla_si_falta_un_valor_de_marcador(tmp_path: Path) -> None:
    datos = _datos()
    datos["columnas"] = [*COLUMNAS, "ETIQUETA"]
    for fila in datos["filas"]:
        fila.append("Corte {{m.volumen.202401.MOB0|int}}")
    with pytest.raises(XlsxError) as exc:
        _ensamblar(tmp_path, datos=datos, metricas={})
    assert exc.value.codigo == "E-XLS-030"


def test_falla_si_el_denominador_suma_cero(tmp_path: Path) -> None:
    datos = _datos()
    for fila in datos["filas"]:
        fila[4] = 0
    with pytest.raises(XlsxError) as exc:
        _ensamblar(tmp_path, datos=datos)
    assert exc.value.codigo == "E-XLS-040"


def test_falla_si_la_columna_de_la_razon_no_es_numerica(tmp_path: Path) -> None:
    datos = _datos()
    datos["filas"][0][3] = "sin dato"
    with pytest.raises(XlsxError) as exc:
        _ensamblar(tmp_path, datos=datos)
    assert exc.value.codigo == "E-XLS-040"


def test_falla_si_la_columna_del_resumen_no_existe(tmp_path: Path) -> None:
    resumen = [{"nombre": "Tasa", "numerador": "NO_EXISTE", "denominador": "ENRAMTS"}]
    with pytest.raises(XlsxError) as exc:
        _ensamblar(tmp_path, resumen=resumen)
    assert exc.value.codigo == "E-XLS-020"


def test_rechaza_extension_incorrecta(tmp_path: Path) -> None:
    with pytest.raises(XlsxError) as exc:
        xlsx_ensamblar(tmp_path / "anexo.xls", datos=_datos(), resumen=_resumen())
    assert exc.value.codigo == "E-XLS-050"


def test_rechaza_datos_vacios(tmp_path: Path) -> None:
    with pytest.raises(XlsxError) as exc:
        _ensamblar(tmp_path, datos={"columnas": ["A"], "filas": []})
    assert exc.value.codigo == "E-XLS-010"


def test_rechaza_formatos_desconocidos(tmp_path: Path) -> None:
    with pytest.raises(XlsxError) as exc:
        _ensamblar(tmp_path, formatos={"TASA_NCL": "porcentaje"})
    assert exc.value.codigo == "E-XLS-020"


def test_rechaza_fila_con_columnas_de_mas(tmp_path: Path) -> None:
    datos = {"columnas": ["A", "B"], "filas": [[1, 2, 3]]}
    with pytest.raises(XlsxError) as exc:
        _ensamblar(tmp_path, datos=datos)
    assert exc.value.codigo == "E-XLS-010"


def test_rechaza_columnas_duplicadas(tmp_path: Path) -> None:
    datos = {"columnas": ["A", "A"], "filas": [[1, 2]]}
    with pytest.raises(XlsxError) as exc:
        _ensamblar(tmp_path, datos=datos)
    assert exc.value.codigo == "E-XLS-010"


def test_rechaza_resumen_sin_nombre(tmp_path: Path) -> None:
    with pytest.raises(XlsxError) as exc:
        _ensamblar(tmp_path, resumen=[{"numerador": "SALDO"}])
    assert exc.value.codigo == "E-XLS-020"


def test_acepta_lista_de_mapas(tmp_path: Path) -> None:
    datos = [
        {"VINTAGE": 202401, "NCLAMTS": 120_000, "ENRAMTS": 4_000_000},
        {"VINTAGE": 202401, "NCLAMTS": 180_000, "ENRAMTS": 4_500_000},
    ]
    resumen = [{"nombre": "Tasa NCL", "numerador": "NCLAMTS", "denominador": "ENRAMTS"}]
    salida = _ensamblar(tmp_path, datos=datos, resumen=resumen)
    formulas = load_workbook(str(salida), data_only=False)["Resumen"]
    valores = load_workbook(str(salida), data_only=True)["Resumen"]
    assert formulas["D5"].value == "=SUM(Datos!B2:B3)/SUM(Datos!C2:C3)"
    assert valores["D5"].value == pytest.approx(300_000 / 8_500_000)


def test_resumen_automatico_suma_columnas_numericas(tmp_path: Path) -> None:
    salida = _ensamblar(tmp_path, resumen=None)
    hoja = load_workbook(str(salida), data_only=False)["Resumen"]
    nombres = [
        hoja.cell(fila, 1).value
        for fila in range(5, hoja.max_row + 1)
        if isinstance(hoja.cell(fila, 1).value, str)
        and hoja.cell(fila, 1).value.startswith("Total ")
    ]
    assert "Total NCLAMTS" in nombres
    assert "Total SALDO" in nombres
    assert "Total VINTAGE" not in nombres
    assert "Total MOB" not in nombres
    # RB-03: nunca se suma una columna de razón (tasa/porcentaje).
    assert "Total TASA_NCL" not in nombres


def test_resumen_automatico_excluye_columnas_de_razon_por_rango(tmp_path: Path) -> None:
    # Una columna sin pista en el nombre pero con valores en [0, 1] es una razón.
    datos = {
        "columnas": ["VINTAGE", "cobertura_intervalo", "NCLAMTS"],
        "filas": [
            [202401, 0.90, 120_000],
            [202402, 0.88, 90_000],
        ],
    }
    salida = _ensamblar(tmp_path, datos=datos, resumen=None)
    hoja = load_workbook(str(salida), data_only=False)["Resumen"]
    nombres = [
        hoja.cell(fila, 1).value
        for fila in range(5, hoja.max_row + 1)
        if isinstance(hoja.cell(fila, 1).value, str)
        and hoja.cell(fila, 1).value.startswith("Total ")
    ]
    assert "Total NCLAMTS" in nombres
    assert "Total cobertura_intervalo" not in nombres


def test_escribe_fechas_con_formato_iso(tmp_path: Path) -> None:
    datos = {
        "columnas": ["FECHA", "SALDO"],
        "filas": [[date(2026, 11, 26), 1_000]],
        "formatos": {"FECHA": "fecha", "SALDO": "mxn"},
    }
    resumen = [{"nombre": "Saldo", "numerador": "SALDO", "formato": "mxn"}]
    salida = _ensamblar(tmp_path, datos=datos, resumen=resumen)
    hoja = load_workbook(str(salida))["Datos"]
    assert hoja["A2"].value == datetime(2026, 11, 26)
    assert hoja["A2"].number_format == "yyyy-mm-dd"
