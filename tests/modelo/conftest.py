"""Cubo sintético pequeño con variación realista (series acumuladas, como el CSV del banco)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from basecards.ingesta.corte import meses_entre, sumar_meses
from basecards.modelo.config import LLAVE_COHORTE

# Catálogos del diccionario de Banamex (orden de los rangos de score dentro de cada familia)
SCORES = {
    "Recalibration": [
        "<130",
        "130-140",
        "141-150",
        "151-160",
        "161-170",
        "171-180",
        "181-190",
        "190+",
    ],
    "DARIA": [
        "a.<=154",
        "b.155-159",
        "c.160-164",
        "d.165-169",
        "e.170-174",
        "f.175-189",
        "g.190-199",
        "h.200-204",
        "i.205+",
    ],
}
# Con espacio final a propósito, como en el CSV real (N-01)
SEG_RECA = [
    "Brands-NonPayroll-OffUs&LE ",
    "Brands-NonPayroll-OnUs",
    "Costco",
    "Priority & Affluent",
]
SEG_DARIA = ["AAC Off Us Non-Payroll ", "LE Thin", "Affluent"]
PRODUCTOS = ["Clasica", "Joy", "Oro", "Platinum"]
CANALES = ["Internet", "Branch", "TMK"]
EDADES = ["01| Young < 25", "02| Young 25-30", "03| Mature >30"]


def _strategy(v: int, rng: np.random.Generator) -> str:
    if v <= 202405:
        return "14.Recalibration 1.0"
    if v <= 202507:
        return "15.Recalibration 2.0"
    if v <= 202510:
        return "r.Reca champ"
    if v <= 202607:
        return "DARIA 1.6" if rng.random() < 0.3 else "r.Reca champ"
    return "DARIA 2.0" if rng.random() < 0.5 else "r.Reca champ"


def generar_cubo(
    cohortes_por_cosecha: int = 6, corte: int = 202609, semilla: int = 7
) -> pd.DataFrame:
    """Cubo con la estructura del diccionario y series de riesgo acumuladas al MOB."""
    rng = np.random.default_rng(semilla)
    filas = []
    v = 202401
    while v <= corte:
        for _ in range(cohortes_por_cosecha):
            st = _strategy(v, rng)
            daria = st.startswith("DARIA")
            scores = SCORES["DARIA" if daria else "Recalibration"]
            k = int(rng.integers(len(scores)))
            dims = {
                "VINTAGE": v,
                "STRATEGY": st,
                "STRATEGY_SEGMENT": str(rng.choice(SEG_DARIA if daria else SEG_RECA)),
                "PRODUCT": str(rng.choice(PRODUCTOS)),
                "CHANNEL": str(rng.choice(CANALES)),
                "AGE_TAG": str(rng.choice(EDADES)),
                "SCORE_TAG": scores[k],
            }
            acc = int(rng.integers(5, 300))
            linea = int(acc * rng.uniform(20_000, 80_000))
            riesgo = (1.6 - k / len(scores)) * (1.4 if daria else 1.0)
            o30_acum = gcl_acum = rev_acum = 0.0
            for mob in range(meses_entre(v, corte) + 1):
                enracc = max(1, int(acc * min(0.95, 0.4 + 0.05 * mob)))
                enram = linea * min(0.6, 0.05 + 0.04 * mob) * rng.uniform(0.9, 1.1)
                o30_acum = max(
                    o30_acum, min(enracc, round(enracc * 0.25 * riesgo * (1 - np.exp(-mob / 8))))
                )
                gcl_acum += enram * 0.004 * riesgo * (mob > 0) * rng.uniform(0.5, 1.5)
                rec = gcl_acum * 0.55
                rev_acum += enram * 0.02 * (mob > 0)
                acq = acc * 400.0 if mob == 0 else 0.0
                var = enram * 0.003
                fix = acc * 3.5
                filas.append(
                    {
                        **dims,
                        "MOB": mob,
                        "ACCOUNTS": acc,
                        "CREDITLINESAMTS": linea,
                        "ENRACCTS": enracc,
                        "OVER30ACCTS": int(o30_acum),
                        "OVER30AMTS": round(enram * o30_acum / enracc, 2),
                        "GCLAMTS": round(gcl_acum, 2),
                        "NCLAMTS": round(gcl_acum, 2) - round(rec, 2),
                        "RECOVERYAMTS": round(rec, 2),
                        "ENRAMTS": round(enram, 2),
                        "REVENUE": round(rev_acum, 2),
                        "EXPENSES_ACQ": round(acq, 2),
                        "EXPENSES_ECM_VAR": round(var, 2),
                        "EXPENSES_ECM_FIX": round(fix, 2),
                        "EXPENSES_TOTAL": round(acq, 2) + round(var, 2) + round(fix, 2),
                    }
                )
        v = sumar_meses(v, 1)
    df = pd.DataFrame(filas)
    df["NCLAMTS"] = (df["GCLAMTS"] - df["RECOVERYAMTS"]).round(2)
    return df.drop_duplicates([*LLAVE_COHORTE, "MOB"]).reset_index(drop=True)


@pytest.fixture(scope="session")
def cubo() -> pd.DataFrame:
    return generar_cubo()


@pytest.fixture(scope="session")
def corte_parquet(tmp_path_factory, cubo):
    """Cubo → CSV → ingesta → Parquet en un directorio temporal."""
    from basecards.ingesta import ingerir_csv

    d = tmp_path_factory.mktemp("datos")
    ruta_csv = d / "corte.csv"
    cubo.to_csv(ruta_csv, index=False)
    res = ingerir_csv(ruta_csv, 202609, dir_cortes=d / "cortes")
    return res, d


@pytest.fixture(scope="session")
def modelo_entrenado(tmp_path_factory, corte_parquet):
    """MPDC entrenado con el cubo de prueba (25 árboles) en un directorio temporal."""
    from basecards.modelo.entrenar import entrenar

    _, d = corte_parquet
    dir_modelos = tmp_path_factory.mktemp("modelos")
    res = entrenar(
        202609, dir_cortes=d / "cortes", dir_base=dir_modelos, n_estimators=25, mlflow=False
    )
    return res, dir_modelos, d / "cortes"
