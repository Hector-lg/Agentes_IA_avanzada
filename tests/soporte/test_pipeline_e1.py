from __future__ import annotations

from pathlib import Path

import pandas as pd

from basecards.soporte.carga import normalizar_catalogos
from basecards.soporte.pipeline_e1 import validar_corte, validar_dataframe
from basecards.soporte.sintetico import generar_cubo_sintetico


def test_cubo_sintetico_apto(cubo_ok):
    res = validar_dataframe(cubo_ok, fecha_corte=202609, derivar_corte_anterior=True)
    criticas = [h for h in res.hallazgos if h.severidad == "critica"]
    assert criticas == [], [h.to_dict() for h in criticas]
    assert res.dictamen == "apto"


def test_n01_recorta_espacios(cubo_ok):
    df = cubo_ok.copy()
    df.loc[0, "STRATEGY_SEGMENT"] = str(df.loc[0, "STRATEGY_SEGMENT"]) + "   "
    normalizado, n = normalizar_catalogos(df)
    assert n >= 1
    assert not str(normalizado.loc[0, "STRATEGY_SEGMENT"]).endswith(" ")


def test_r05_producto_fuera_catalogo(cubo_ok):
    df = cubo_ok.copy()
    df.loc[0, "PRODUCT"] = "ProductoInvalido"
    res = validar_dataframe(df, fecha_corte=202609, derivar_corte_anterior=False)
    assert any(h.regla_id == "R-05" for h in res.hallazgos)


def test_r08_daria_en_vintage_temprano(cubo_ok):
    df = cubo_ok.copy()
    # Forzar DARIA 1.6 en 202403
    mask = df["VINTAGE"] == 202403
    df.loc[mask, "STRATEGY"] = "DARIA 1.6"
    res = validar_dataframe(df, fecha_corte=202609, derivar_corte_anterior=False)
    assert any(h.regla_id == "R-08" for h in res.hallazgos)


def test_r14_ncl_inconsistente(cubo_ok):
    df = cubo_ok.copy()
    df.loc[0, "NCLAMTS"] = float(df.loc[0, "GCLAMTS"]) + 999
    res = validar_dataframe(df, fecha_corte=202609, derivar_corte_anterior=False)
    assert any(h.regla_id == "R-14" for h in res.hallazgos)
    assert res.dictamen == "no apto"


def test_r16_acq_en_mob_positivo(cubo_ok):
    df = cubo_ok.copy()
    mob_pos = df["MOB"] > 0
    df.loc[mob_pos, "EXPENSES_ACQ"] = 10.0
    df.loc[mob_pos, "EXPENSES_TOTAL"] = (
        df.loc[mob_pos, "EXPENSES_ACQ"]
        + df.loc[mob_pos, "EXPENSES_ECM_VAR"]
        + df.loc[mob_pos, "EXPENSES_ECM_FIX"]
    )
    res = validar_dataframe(df, fecha_corte=202609, derivar_corte_anterior=False)
    assert any(h.regla_id == "R-16" for h in res.hallazgos)


def test_r17_duplicado(cubo_ok):
    df = pd.concat([cubo_ok, cubo_ok.iloc[[0]]], ignore_index=True)
    res = validar_dataframe(df, fecha_corte=202609, derivar_corte_anterior=False)
    assert any(h.regla_id == "R-17" and h.detalle.get("tipo") == "duplicado" for h in res.hallazgos)


def test_p01_enraccts_mayor_accounts(cubo_ok):
    df = cubo_ok.copy()
    df.loc[0, "ENRACCTS"] = int(df.loc[0, "ACCOUNTS"]) + 50
    res = validar_dataframe(df, fecha_corte=202609, derivar_corte_anterior=False)
    assert any(h.regla_id == "P-01" for h in res.hallazgos)


def test_r01_falta_columna(cubo_ok):
    df = cubo_ok.drop(columns=["REVENUE"])
    res = validar_dataframe(df, fecha_corte=202609, derivar_corte_anterior=False)
    assert res.dictamen == "no apto"
    assert any(h.regla_id == "R-01" for h in res.hallazgos)


def test_validar_corte_csv(tmp_path: Path):
    df = generar_cubo_sintetico(mobs_por_cohorte=2)
    ruta = tmp_path / "corte.csv"
    df.to_csv(ruta, index=False)
    res = validar_corte(ruta, fecha_corte=202609)
    assert res.filas == len(df)
    assert isinstance(res.to_dict(), dict)
