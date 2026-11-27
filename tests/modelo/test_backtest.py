"""Datos de entrenamiento, 9 modelos cuantílicos y backtest sobre el cubo sintético (pocos árboles)."""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from basecards.ingesta import ingerir_csv, perf_month
from basecards.ingesta.corte import meses_entre
from basecards.modelo.backtest import correr_backtest, main, origenes_backtest
from basecards.modelo.config import LLAVE_COHORTE, cargar_config
from basecards.modelo.entrenar import (
    EntrenamientoError,
    cargar_corte,
    etiqueta_cuantil,
    ordenar_cuantiles,
    preparar_datos,
)

OBJETIVOS = {"tasa_ncl", "tasa_over30", "margen_por_cuenta"}


@pytest.fixture(scope="module")
def cfg():
    return cargar_config()


@pytest.fixture(scope="module")
def resultado(cubo):
    return correr_backtest(202609, df=cubo, n_estimators=25)


def test_preparar_datos_excluye_y_reporta(cubo, cfg):
    df = cubo.copy()
    pm = perf_month(df["VINTAGE"], df["MOB"])
    viejas = df.index[pm < 202609]
    df.loc[viejas[0], "NCLAMTS"] += 999  # falla R-14
    df.loc[viejas[1], "EXPENSES_TOTAL"] += 50  # falla R-15
    df.loc[viejas[2], "ENRACCTS"] = 0  # sin exposición
    ent, info = preparar_datos(df, 202609, cfg)
    excl = info["filas_excluidas"]
    assert info["entrenado_hasta"] == 202608 and (ent["PERF_MONTH"] <= 202608).all()
    assert excl["perf_month_del_corte_actual"] == int((pm == 202609).sum()) > 0
    assert excl["regla_critica_R14"] == 1 and excl["regla_critica_R15"] == 1
    assert excl["exposicion_ENRACCTS_menor_a_1"] == 1 and excl["nulos"] == 0
    assert info["filas_entrenamiento"] == len(ent) == int((pm < 202609).sum()) - 3


def test_rapido_toma_cohortes_completas(cubo, cfg):
    completo, _ = preparar_datos(cubo, 202609, cfg)
    rapido, info = preparar_datos(cubo, 202609, cfg, rapido=True)
    assert info["rapido"] == {"fraccion_cohortes": cfg["modelo"]["fraccion_rapido"]}
    assert 0 < info["cohortes_entrenamiento"] < completo.drop_duplicates(LLAVE_COHORTE).shape[0]
    # Cada cohorte de la muestra conserva todos sus MOB
    filas = completo.groupby(LLAVE_COHORTE, observed=True).size()
    filas_rapido = rapido.groupby(LLAVE_COHORTE, observed=True).size()
    assert (filas.loc[filas_rapido.index] == filas_rapido).all()


def test_no_entrena_con_corte_no_apto(tmp_path, cubo):
    malo = cubo.copy()
    malo.loc[0, "NCLAMTS"] = float(malo.loc[0, "GCLAMTS"]) + 999  # R-14 es crítica
    ruta = tmp_path / "corte.csv"
    malo.to_csv(ruta, index=False)
    dir_cortes = tmp_path / "cortes"
    res = ingerir_csv(ruta, 202609, dir_cortes=dir_cortes, validar=True)
    assert res.manifiesto["validacion"]["dictamen"] == "no apto"
    with pytest.raises(EntrenamientoError, match="no apto"):
        cargar_corte(202609, dir_cortes=dir_cortes)
    with pytest.raises(EntrenamientoError, match="no apto"):
        correr_backtest(202609, dir_cortes=dir_cortes, n_estimators=5)
    df, manifiesto = cargar_corte(202609, dir_cortes=dir_cortes, forzar=True)
    assert len(df) == len(malo) and manifiesto["corte"] == 202609


def test_origenes_y_cuantiles(cfg):
    assert origenes_backtest(202608, cfg) == [202602, 202511, 202508, 202505]
    assert [etiqueta_cuantil(q) for q in cfg["modelo"]["cuantiles"]] == ["q05", "q50", "q95"]
    cruzados = np.array([[0.3, 0.1, 0.2], [0.1, 0.2, 0.3]])
    assert ordenar_cuantiles(cruzados).tolist() == [[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]]


def test_backtest_con_origen_movil(resultado):
    r = resultado["resumen"]
    assert r["origenes"] == [202602, 202511, 202508, 202505]
    assert set(r["objetivos"]) == OBJETIVOS
    for obj, m in r["objetivos"].items():
        assert m["n"] > 0, obj
        for k in ("pinball_q05", "pinball_q50", "pinball_q95", "pinball_q50_curva"):
            assert m[k] > 0, (obj, k)
        assert 0 <= m["cobertura_cruda"] <= 1 and 0 <= m["cobertura_cruda_ponderada"] <= 1
        assert math.isfinite(m["mejora_pinball_q50_vs_curva_pct"])
        assert math.isfinite(m["mejora_pinball_q50_vs_arbol_pct"])
    assert -1 <= r["objetivos"]["margen_por_cuenta"]["spearman_estrategia_segmento"] <= 1
    # En cada origen se evalúa solo lo que el modelo no vio: los 6 meses siguientes
    for o in resultado["por_origen"]:
        assert meses_entre(o["origen"], o["evaluado_hasta"]) == 6


def test_backtest_calibra_cqr_y_mide_m4(resultado, cfg):
    n_tramos = len(cfg["modelo"]["cqr"]["tramos_mob"])
    for obj, m in resultado["resumen"]["objetivos"].items():
        assert 0 <= m["cobertura_cqr"] <= 1 and 0 <= m["cobertura_cqr_ponderada"] <= 1, obj
        assert set(m["cobertura_cqr_por_familia"]) <= {"Recalibration", "DARIA"}
        off = resultado["cqr_offsets"][obj]
        assert len(off["tramo"]) == n_tramos and all(np.isfinite(off["tramo"]))
    m4 = resultado["resumen"]["m4"]
    assert m4["n"] > 0 and m4["mae"] >= 0 and math.isfinite(m4["mape_agregado_pct"])
    res = resultado["m4_residuos"]
    assert len(res["residuo_q05"]) == len(res["residuo_q95"]) == n_tramos
    assert all(lo <= hi for lo, hi in zip(res["residuo_q05"], res["residuo_q95"], strict=True))


def test_cli(tmp_path, corte_parquet, capsys):
    _, d = corte_parquet
    salida = tmp_path / "backtest.json"
    codigo = main(
        ["--corte", "202609", "--dir-cortes", str(d / "cortes"), "--n-estimators", "5"]
        + ["--salida", str(salida)]
    )
    assert codigo == 0
    impreso = json.loads(capsys.readouterr().out)
    assert set(impreso["objetivos"]) == OBJETIVOS
    assert len(json.loads(salida.read_text(encoding="utf-8"))["por_origen"]) == 4
