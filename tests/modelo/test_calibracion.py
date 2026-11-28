from __future__ import annotations

import numpy as np
import pandas as pd

from basecards.modelo import calibracion as C


def test_tramo_de_mob():
    tramos = [[0, 3], [4, 12], [13, 99]]
    assert C.tramo_de_mob(np.array([0, 3, 4, 12, 13, 40, 150]), tramos).tolist() == [
        0,
        0,
        1,
        1,
        2,
        2,
        2,
    ]


def test_no_conformidad_y_cuantil_conformal():
    e = C.no_conformidad(np.array([1.0, 5.0, -1.0]), np.zeros(3), np.full(3, 2.0))
    assert e.tolist() == [-1.0, 3.0, 1.0]  # dentro (< 0), arriba y abajo de la banda (> 0)
    # ⌈(n + 1)(1 − α)⌉ = ⌈101 × 0.9⌉ = 91 → el valor 91.º de 0…99
    assert C.cuantil_conformal(np.arange(100.0), 0.1) == 90.0
    assert C.cuantil_conformal(np.array([]), 0.1) == 0.0


def test_offsets_por_familia_solo_con_suficientes_residuos():
    rng = np.random.default_rng(0)
    n = C.MIN_FILAS_CQR_FAMILIA
    e = pd.DataFrame(
        {
            "tramo": [0] * (n + 10),
            "familia": ["Recalibration"] * n + ["DARIA"] * 10,
            "e": np.concatenate([rng.normal(0, 1, n), np.full(10, 50.0)]),
        }
    )
    off = C.offsets_cqr(e, n_tramos=2, alpha=0.1)
    assert off["tramo"][1] == 0.0  # tramo sin residuos
    assert off["familia_tramo"]["Recalibration"][0] is not None
    assert off["familia_tramo"]["DARIA"] == [None, None]  # 10 residuos no alcanzan
    q = C.offsets_por_fila(np.array([0, 0, 1]), np.array(["Recalibration", "DARIA", "DARIA"]), off)
    assert q[0] == off["familia_tramo"]["Recalibration"][0]
    assert q[1] == off["tramo"][0] and q[2] == 0.0  # DARIA usa el Q de su tramo


def test_aplicar_cqr_nunca_excluye_a_q50():
    pred = np.array([[0.1, 0.2, 0.3]])
    assert np.allclose(C.aplicar_cqr(pred, np.array([0.05])), [[0.05, 0.2, 0.35]])
    assert np.allclose(C.aplicar_cqr(pred, np.array([-0.5])), [[0.2, 0.2, 0.2]])


def test_cobertura_validacion_cruzada():
    """Con puntajes intercambiables entre orígenes, la cobertura queda cerca de 1 − α."""
    rng = np.random.default_rng(1)
    n = 4000
    e = pd.DataFrame(
        {
            "origen": np.repeat([1, 2, 3, 4], n // 4),
            "tramo": rng.integers(0, 2, n),
            "familia": rng.choice(["Recalibration", "DARIA"], n),
            "e": rng.normal(0.3, 1.0, n),
            "w": np.ones(n),
        }
    )
    r = C.cobertura_validacion_cruzada(e, n_tramos=2, alpha=0.1)
    assert 0.87 <= r["cobertura_cqr"] <= 0.93
    assert np.isclose(r["cobertura_cqr"], r["cobertura_cqr_ponderada"])
    assert set(r["cobertura_cqr_por_familia"]) == {"Recalibration", "DARIA"}
