from __future__ import annotations

import json

import pandas as pd
import pytest

from basecards.ingesta import (
    IngestaError,
    corte_anterior_derivado,
    ingerir_csv,
    leer_corte,
    perf_month,
)
from basecards.ingesta.corte import sha256_archivo
from basecards.soporte.config import cargar_mapeo


def test_parquet_tipado_y_manifiesto(corte_parquet, cubo):
    res, d = corte_parquet
    df = leer_corte(202609, dir_cortes=d / "cortes")
    assert len(df) == len(cubo)
    assert list(df.columns)[:8] == [
        "VINTAGE",
        "MOB",
        "STRATEGY",
        "STRATEGY_SEGMENT",
        "PRODUCT",
        "CHANNEL",
        "AGE_TAG",
        "SCORE_TAG",
    ]
    assert isinstance(df["STRATEGY"].dtype, pd.CategoricalDtype)
    assert df["ACCOUNTS"].dtype.kind == "i" and df["NCLAMTS"].dtype == "float64"
    # N-01: se recortan los espacios finales
    assert not df["STRATEGY_SEGMENT"].astype(str).str.endswith(" ").any()
    m = res.manifiesto
    assert m["n01_filas_normalizadas"] == int(cubo["STRATEGY_SEGMENT"].str.endswith(" ").sum())
    assert m["parquet"]["sha256"] == sha256_archivo(res.ruta_parquet)
    assert m["filas"] == len(cubo) and m["perf_month_max"] == 202609
    # S-01: el cubo de prueba es acumulado, igual que el CSV del banco
    assert m["series_acumuladas"]["NCLAMTS"]["acumulada"] is True
    assert json.loads(res.ruta_manifiesto.read_text(encoding="utf-8"))["corte"] == 202609


def test_acepta_encabezados_dummi(tmp_path, cubo):
    """S-06: un CSV con códigos dummiN en el encabezado se traduce a nombres de variable."""
    inverso = {v: k for k, v in cargar_mapeo()["dummi_a_variable"].items()}
    # Códigos del diccionario de Banamex: no son consecutivos y MOB no tiene código
    assert len(inverso) == 21 and "MOB" not in inverso
    assert (inverso["STRATEGY"], inverso["AGE_TAG"], inverso["SCORE_TAG"]) == (
        "dummi12",
        "dummi2",
        "dummi10",
    )
    assert (inverso["REVENUE"], inverso["EXPENSES_TOTAL"]) == ("dummi44", "dummi49")
    ruta = tmp_path / "dummi.csv"
    cubo.head(200).rename(columns=inverso).to_csv(ruta, index=False)
    res = ingerir_csv(ruta, 202609, dir_cortes=tmp_path / "cortes")
    assert res.manifiesto["filas"] == 200
    assert "STRATEGY" in leer_corte(202609, dir_cortes=tmp_path / "cortes").columns


def test_falta_columna(tmp_path, cubo):
    ruta = tmp_path / "malo.csv"
    cubo.head(50).drop(columns=["REVENUE"]).to_csv(ruta, index=False)
    with pytest.raises(IngestaError, match="REVENUE"):
        ingerir_csv(ruta, 202609, dir_cortes=tmp_path / "cortes")


def test_valores_no_numericos_se_reportan(tmp_path, cubo):
    sub = cubo.head(30).astype({"ACCOUNTS": object})
    sub.loc[0, "ACCOUNTS"] = "diez"
    ruta = tmp_path / "texto.csv"
    sub.to_csv(ruta, index=False)
    res = ingerir_csv(ruta, 202609, dir_cortes=tmp_path / "cortes")
    assert res.manifiesto["valores_no_numericos"] == {"ACCOUNTS": 1}


def test_validar_queda_en_el_manifiesto(tmp_path, cubo):
    """--validar corre E1 del Agente de Soporte y deja el dictamen en el manifiesto."""
    ruta = tmp_path / "corte.csv"
    cubo.to_csv(ruta, index=False)
    res = ingerir_csv(ruta, 202609, dir_cortes=tmp_path / "cortes", validar=True)
    v = res.manifiesto["validacion"]
    assert v.get("error") is None, v["error"]
    assert v["dictamen"] == "apto"
    assert (res.ruta_parquet.parent / v["archivo_hallazgos"]).exists()


def test_validar_no_tumba_la_ingesta(tmp_path, cubo, monkeypatch):
    """Si una regla del Agente de Soporte lanza un error, queda en el manifiesto."""

    def regla_rota(*args, **kwargs):
        raise RuntimeError("regla rota")

    monkeypatch.setattr("basecards.soporte.pipeline_e1.validar_dataframe", regla_rota)
    ruta = tmp_path / "corte.csv"
    cubo.head(100).to_csv(ruta, index=False)
    res = ingerir_csv(ruta, 202609, dir_cortes=tmp_path / "cortes", validar=True)
    assert res.manifiesto["validacion"] == {"dictamen": None, "error": "RuntimeError: regla rota"}
    assert res.ruta_parquet.exists()


def test_perf_month_y_corte_anterior():
    df = pd.DataFrame({"VINTAGE": [202401, 202411, 202609, 202608], "MOB": [32, 2, 0, 0]})
    assert perf_month(df["VINTAGE"], df["MOB"]).tolist() == [202609, 202501, 202609, 202608]
    assert corte_anterior_derivado(df, 202609)["VINTAGE"].tolist() == [202411, 202608]
