"""Indicadores objetivo del MPDC (PRD §5.3 y §6.1).

- tasa_ncl          = NCLAMTS / ENRAMTS
- tasa_over30       = OVER30ACCTS / ENRACCTS
- margen_por_cuenta = MARGEN / ACCOUNTS

MARGEN depende de S-01 (`acumulado` en config/indicadores.yaml):
- acumulado=false: REVENUE − NCLAMTS − EXPENSES_TOTAL (todo del mes).
- acumulado=true:  REVENUE y NCLAMTS ya vienen acumulados al MOB, pero los gastos son del mes,
  así que se usan los gastos acumulados: REVENUE − NCLAMTS − Σ_{m ≤ MOB} EXPENSES_TOTAL(m).
  Es el margen acumulado de la cohorte a ese MOB.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from basecards.modelo.config import LLAVE_COHORTE, LLAVE_FILA, cargar_config, cargar_indicadores


def objetivos(cfg: dict[str, Any] | None = None) -> dict[str, dict[str, str]]:
    return (cfg or cargar_config())["modelo"]["objetivos"]


def calcular_margen(df: pd.DataFrame, acumulado: bool | None = None) -> pd.Series:
    if acumulado is None:
        acumulado = bool(cargar_indicadores().get("acumulado", False))
    gastos = df["EXPENSES_TOTAL"].astype("float64")
    if acumulado:
        orden = df.sort_values(LLAVE_FILA)
        gastos = (
            orden.groupby(LLAVE_COHORTE, sort=False, observed=True)["EXPENSES_TOTAL"]
            .cumsum()
            .astype("float64")
            .reindex(df.index)
        )
    return df["REVENUE"].astype("float64") - df["NCLAMTS"].astype("float64") - gastos


def razon(num: pd.Series, den: pd.Series) -> pd.Series:
    den = den.astype("float64")
    return (num.astype("float64") / den.where(den > 0)).astype("float64")


def agregar_indicadores(
    df: pd.DataFrame, acumulado: bool | None = None, cfg: dict[str, Any] | None = None
) -> pd.DataFrame:
    """Agrega MARGEN y una columna por indicador objetivo (NaN si el denominador es 0)."""
    out = df.copy()
    out["MARGEN"] = calcular_margen(out, acumulado)
    for nombre, meta in objetivos(cfg).items():
        out[nombre] = razon(out[meta["numerador"]], out[meta["denominador"]])
    return out
