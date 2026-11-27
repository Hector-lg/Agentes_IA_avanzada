from __future__ import annotations

import numpy as np
import pandas as pd

from basecards.modelo.config import LLAVE_COHORTE
from basecards.modelo.features import FEATURES, agregar_rezagos_over30, construir_tabla, filas_a_mob
from basecards.modelo.indicadores import agregar_indicadores, calcular_margen

# Todas las métricas del cubo (las 14 columnas numéricas que no son llave)
METRICAS = [
    "ACCOUNTS",
    "CREDITLINESAMTS",
    "ENRACCTS",
    "OVER30ACCTS",
    "OVER30AMTS",
    "GCLAMTS",
    "NCLAMTS",
    "RECOVERYAMTS",
    "ENRAMTS",
    "REVENUE",
    "EXPENSES_ACQ",
    "EXPENSES_ECM_VAR",
    "EXPENSES_ECM_FIX",
    "EXPENSES_TOTAL",
]


def _cohorte(n: int, **cols) -> pd.DataFrame:
    """Una sola cohorte sintética con n filas y las columnas indicadas."""
    return pd.DataFrame(
        {**{k: ["x"] * n for k in LLAVE_COHORTE if k != "VINTAGE"}, "VINTAGE": [202401] * n, **cols}
    )


def test_sin_fuga_de_informacion(cubo):
    """Cambiar las métricas de los MOB ≥ 1 no cambia ninguna feature de ninguna fila."""
    base = construir_tabla(cubo)
    alterado = cubo.copy()
    m = alterado["MOB"] >= 1
    alterado.loc[m, METRICAS] = alterado.loc[m, METRICAS] * 7 + 3
    otra = construir_tabla(alterado)
    pd.testing.assert_frame_equal(base[FEATURES], otra[FEATURES])
    # y en MOB 0 la utilización inicial (métrica del propio MOB 0) no se usa
    assert base.loc[base["MOB"] == 0, "UTIL_INICIAL"].isna().all()
    assert base.loc[base["MOB"] > 0, "UTIL_INICIAL"].notna().all()


def test_score_ord_y_familia(cubo):
    t = construir_tabla(cubo)
    reca = t.loc[t["SCORE_TAG"].astype(str) == "<130"]
    daria = t.loc[t["SCORE_TAG"].astype(str) == "i.205+"]
    assert len(reca) > 0 and len(daria) > 0
    assert (reca["SCORE_ORD"] == 1).all() and (reca["FAMILIA"] == "Recalibration").all()
    assert (daria["SCORE_ORD"] == 9).all() and (daria["FAMILIA"] == "DARIA").all()
    assert t["SCORE_ORD"].notna().all()


def test_indicadores_objetivo():
    df = _cohorte(
        2,
        MOB=[0, 1],
        NCLAMTS=[0.0, 30.0],
        ENRAMTS=[0.0, 600.0],
        OVER30ACCTS=[0, 3],
        ENRACCTS=[0, 12],
        ACCOUNTS=[20, 20],
        REVENUE=[0.0, 50.0],
        EXPENSES_TOTAL=[10.0, 5.0],
    )
    t = agregar_indicadores(df, acumulado=False)
    # Con denominador 0 la tasa queda en NaN, no en inf
    assert t["tasa_ncl"].isna().iloc[0] and t["tasa_over30"].isna().iloc[0]
    assert t["tasa_ncl"].iloc[1] == 0.05 and t["tasa_over30"].iloc[1] == 0.25
    assert t["margen_por_cuenta"].tolist() == [-0.5, (50 - 30 - 5) / 20]


def test_margen_con_gastos_acumulados():
    df = _cohorte(
        3,
        MOB=[2, 0, 1],
        REVENUE=[30.0, 0.0, 10.0],
        NCLAMTS=[5.0, 0.0, 1.0],
        EXPENSES_TOTAL=[2.0, 100.0, 2.0],
    )
    acum = calcular_margen(df, acumulado=True)
    assert acum.tolist() == [30 - 5 - 104, -100, 10 - 1 - 102]
    assert calcular_margen(df, acumulado=False).tolist() == [23, -100, 7]


def test_rezagos_m4(cubo):
    t = agregar_rezagos_over30(construir_tabla(cubo))
    coh = t.loc[
        t[LLAVE_COHORTE].apply(tuple, axis=1) == tuple(t.iloc[0][LLAVE_COHORTE])
    ].sort_values("MOB")
    r = coh.iloc[5]
    assert np.isclose(r["O30_T1"], coh.iloc[4]["tasa_over30"]) and np.isclose(
        r["O30_T2"], coh.iloc[3]["tasa_over30"]
    )
    assert np.isclose(r["O30_OBJETIVO"], coh.iloc[8]["tasa_over30"])
    assert coh.iloc[:2]["O30_T2"].isna().all()


def test_rezagos_m4_con_hueco_de_mob():
    """Si falta un MOB intermedio, la fila no usa rezagos ni pendiente de MOB no contiguos."""
    df = _cohorte(4, MOB=[0, 1, 2, 4], tasa_over30=[0.0, 0.1, 0.2, 0.4], ENRACCTS=[10, 10, 10, 10])
    t = agregar_rezagos_over30(df, rezagos=3, adelanto=1).set_index("MOB")
    assert np.isclose(t.loc[2, "O30_PENDIENTE"], 0.1)  # (0.2 − 0.0) / 2
    assert t.loc[4, ["O30_T", "O30_T1", "O30_T2", "O30_PENDIENTE"]].isna().all()
    assert np.isnan(t.loc[2, "O30_OBJETIVO"])  # MOB 3 no existe


def test_filas_a_mob(cubo):
    t = construir_tabla(cubo)
    h = filas_a_mob(t, 12)
    assert len(h) == t.drop_duplicates(LLAVE_COHORTE).shape[0] and (h["MOB"] == 12).all()
    assert h["UTIL_INICIAL"].notna().all()
