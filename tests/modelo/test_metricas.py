from __future__ import annotations

import math

import numpy as np
import pandas as pd

from basecards.modelo import metricas as M
from basecards.modelo.baselines import BaselineArbol, BaselineCurva
from basecards.modelo.features import construir_tabla


def test_pinball():
    y = np.array([0.0, 1.0, 2.0])
    assert M.pinball(y, y, 0.5) == 0.0
    # Quedarse corto cuesta alpha; pasarse cuesta 1 − alpha
    assert np.isclose(M.pinball(np.array([1.0]), np.array([0.0]), 0.9), 0.9)
    assert np.isclose(M.pinball(np.array([0.0]), np.array([1.0]), 0.9), 0.1)
    # Con pesos, la fila pesada domina
    assert np.isclose(
        M.pinball(np.array([1.0, 0.0]), np.zeros(2), 0.5, np.array([3.0, 1.0])), 0.375
    )


def test_mae_rmse_ponderados():
    y, p, w = np.zeros(2), np.array([1.0, 3.0]), np.array([3.0, 1.0])
    assert M.mae(y, p, w) == 1.5
    assert np.isclose(M.rmse(y, p, w), math.sqrt(3.0))
    assert M.mae(y, p) == 2.0


def test_cobertura():
    y = np.array([0.0, 1.0, 2.0, 3.0])
    assert M.cobertura(y, np.ones(4), np.full(4, 2.0)) == 0.5
    assert M.cobertura(y, np.ones(4), np.full(4, 2.0), np.array([1.0, 1.0, 1.0, 5.0])) == 0.25
    assert math.isnan(M.cobertura(np.array([]), np.array([]), np.array([])))


def test_cuantil_ponderado():
    x = np.array([4.0, 1.0, 3.0, 2.0])
    # Con pesos iguales coincide con el cuantil usual (Hazen): mediana 2.5, extremos en el rango
    assert M.cuantil_ponderado(x, np.ones(4), [0.05, 0.5, 0.95]).tolist() == [1.0, 2.5, 4.0]
    # Casi todo el peso en el valor 2 (x[3]): la mediana queda junto a 2
    q = M.cuantil_ponderado(x, np.array([1.0, 1.0, 1.0, 97.0]), [0.05, 0.5, 0.95])
    assert q[0] < q[1] < q[2] and abs(q[1] - 2.0) < 0.05
    # Los pesos 0 no cuentan; sin peso no hay cuantil
    assert M.cuantil_ponderado(x, np.array([0.0, 1.0, 0.0, 1.0]), [0.5]).tolist() == [1.5]
    assert np.isnan(M.cuantil_ponderado(x, np.zeros(4), [0.5])).all()


def test_spearman_y_mape_agregado():
    df = pd.DataFrame(
        {
            "g": ["a", "a", "b", "c", "d"],
            "obs": [0.1, 0.3, 0.4, 0.6, 0.8],
            "w": [1.0, 1.0, 2.0, 2.0, 2.0],
        }
    )
    df["pred"] = df["obs"] * 1.1
    assert np.isclose(M.spearman_ranking(df, "pred", "obs", "w", ["g"]), 1.0)
    assert np.isclose(
        M.spearman_ranking(df.assign(pred=-df["obs"]), "pred", "obs", "w", ["g"]), -1.0
    )
    assert math.isnan(M.spearman_ranking(df.head(2), "pred", "obs", "w", ["g"]))
    assert np.isclose(M.mape_agregado(df, "pred", "obs", "w", ["g"]), 10.0)


def test_baseline_curva_usa_respaldos():
    t = pd.DataFrame(
        {
            "MOB": [0, 0, 1, 1, 1],
            "FAMILIA": ["Recalibration"] * 3 + ["DARIA"] * 2,
            "y": [0.0, 0.2, 0.1, 0.5, 0.7],
            "w": [1.0, 1.0, 1.0, 1.0, 1.0],
        }
    )
    b = BaselineCurva([0.05, 0.5, 0.95]).fit(t, "y", "w")
    nuevo = pd.DataFrame({"MOB": [1, 0, 7], "FAMILIA": ["DARIA", "DARIA", "DARIA"]})
    p = b.predict(nuevo)
    assert np.allclose(p[0], M.cuantil_ponderado(np.array([0.5, 0.7]), np.ones(2), b.cuantiles))
    assert np.allclose(p[1], b.por_mob[0])  # DARIA no tiene MOB 0: usa la curva del MOB
    assert np.allclose(p[2], b.global_)  # MOB 7 no se vio: usa la curva global
    assert (np.diff(p, axis=1) >= 0).all()


def test_baselines_en_cubo(cubo):
    t = construir_tabla(cubo)
    curva = BaselineCurva([0.05, 0.5, 0.95]).fit(t, "tasa_ncl", "ENRAMTS").predict(t)
    arbol = BaselineArbol().fit(t, "tasa_ncl", "ENRAMTS").predict(t)
    assert curva.shape == (len(t), 3) and not np.isnan(curva).any()
    assert arbol.shape == (len(t),) and not np.isnan(arbol).any()
    # Dentro de la muestra, el árbol (usa SCORE_ORD) ajusta mejor que la curva promedio
    sub = (t["ENRAMTS"] > 0).to_numpy()
    y, w = t.loc[sub, "tasa_ncl"], t.loc[sub, "ENRAMTS"]
    assert M.mae(y, arbol[sub], w) < M.mae(y, curva[sub, 1], w)
