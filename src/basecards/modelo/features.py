"""Features del MPDC (PRD §6.2) sin fuga de información.

Una fila por cohorte × MOB. Features:
- MOB;
- dimensiones categóricas: STRATEGY, FAMILIA, STRATEGY_SEGMENT, PRODUCT, CHANNEL, AGE_TAG;
- SCORE_ORD: posición del SCORE_TAG dentro de su familia (sustituye a SCORE_TAG categórico);
- VINTAGE_MES: mes del año de VINTAGE. VINTAGE no entra como categoría porque el modelo debe
  pronosticar cosechas que no vio al entrenar (backtest fuera de tiempo y cosechas nuevas);
- originación del MOB 0 de la misma cohorte: LOG_ACCOUNTS, LINEA_PROM y UTIL_INICIAL.

Fuga de información: ninguna feature usa métricas del MOB que se predice ni de MOB posteriores.
ACCOUNTS y CREDITLINESAMTS son atributos de originación (constantes en la cohorte). UTIL_INICIAL
usa ENRAMTS del MOB 0, así que vale NaN en las filas de MOB 0.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from basecards.ingesta.corte import perf_month
from basecards.modelo.config import LLAVE_COHORTE, LLAVE_FILA, cargar_config
from basecards.modelo.indicadores import agregar_indicadores

FEATURES_CAT = ["STRATEGY", "FAMILIA", "STRATEGY_SEGMENT", "PRODUCT", "CHANNEL", "AGE_TAG"]
FEATURES_NUM = ["MOB", "SCORE_ORD", "VINTAGE_MES", "LOG_ACCOUNTS", "LINEA_PROM", "UTIL_INICIAL"]
FEATURES = FEATURES_NUM + FEATURES_CAT

# Features adicionales de M4 (pronóstico de Over30 a 3 MOB)
FEATURES_M4_EXTRA = ["O30_T", "O30_T1", "O30_T2", "O30_PENDIENTE"]
FEATURES_M4 = FEATURES + FEATURES_M4_EXTRA

ORIGINACION = ["ACCOUNTS_0", "CREDITLINESAMTS_0", "ENRAMTS_0"]


def familia_y_orden(
    cfg: dict[str, Any] | None = None,
) -> tuple[dict[str, str], dict[tuple[str, str], int]]:
    m = (cfg or cargar_config())["modelo"]
    familia = {s: fam for fam, strategies in m["familias"].items() for s in strategies}
    orden = {
        (fam, tag): i + 1 for fam, tags in m["score_orden"].items() for i, tag in enumerate(tags)
    }
    return familia, orden


def agregar_familia_score(df: pd.DataFrame, cfg: dict[str, Any] | None = None) -> pd.DataFrame:
    """Agrega FAMILIA, SCORE_ORD y VINTAGE_MES. Modifica `df` (sin copiar) y lo devuelve."""
    familia, orden = familia_y_orden(cfg)
    out = df
    fam = out["STRATEGY"].astype("string").map(familia)
    out["FAMILIA"] = fam.fillna("Desconocida")
    claves = list(zip(out["FAMILIA"].astype(str), out["SCORE_TAG"].astype(str), strict=True))
    out["SCORE_ORD"] = pd.array([orden.get(k) for k in claves], dtype="Float64").astype("float64")
    out["VINTAGE_MES"] = (out["VINTAGE"].astype("int64") % 100).astype("int16")
    return out


def tabla_originacion(df: pd.DataFrame) -> pd.DataFrame:
    """Atributos de MOB 0 por cohorte (si una cohorte no trae MOB 0, se usa su MOB mínimo)."""
    cols = [*LLAVE_COHORTE, "MOB", "ACCOUNTS", "CREDITLINESAMTS", "ENRAMTS"]
    base = df[cols].sort_values(LLAVE_FILA).drop_duplicates(LLAVE_COHORTE, keep="first")
    base = base.rename(
        columns={
            "ACCOUNTS": "ACCOUNTS_0",
            "CREDITLINESAMTS": "CREDITLINESAMTS_0",
            "ENRAMTS": "ENRAMTS_0",
            "MOB": "MOB_ORIGEN",
        }
    )
    return base.reset_index(drop=True)


def calcular_originacion(df: pd.DataFrame) -> pd.DataFrame:
    """LOG_ACCOUNTS, LINEA_PROM y UTIL_INICIAL a partir de ACCOUNTS_0, CREDITLINESAMTS_0, ENRAMTS_0.

    Modifica `df` (sin copiar) y lo devuelve.
    """
    out = df
    acc = out["ACCOUNTS_0"].astype("float64")
    lin = out["CREDITLINESAMTS_0"].astype("float64")
    out["LOG_ACCOUNTS"] = np.log1p(acc)
    out["LINEA_PROM"] = lin / acc.where(acc > 0)
    util = out["ENRAMTS_0"].astype("float64") / lin.where(lin > 0)
    # No usar métricas del mismo MOB que se predice: en MOB 0 la utilización inicial no se conoce
    out["UTIL_INICIAL"] = util.where(out["MOB"].astype("int64") > out["MOB_ORIGEN"].astype("int64"))
    return out


def agregar_originacion(df: pd.DataFrame, orig: pd.DataFrame) -> pd.DataFrame:
    return calcular_originacion(
        df.merge(orig, on=LLAVE_COHORTE, how="left", validate="many_to_one")
    )


def construir_tabla(df: pd.DataFrame, cfg: dict[str, Any] | None = None) -> pd.DataFrame:
    """Corte tipado → tabla con features, objetivos, pesos y PERF_MONTH."""
    out = agregar_indicadores(df, cfg=cfg)
    out = agregar_familia_score(out, cfg)
    out = agregar_originacion(out, tabla_originacion(out))
    out["PERF_MONTH"] = perf_month(out["VINTAGE"], out["MOB"])
    return out


def niveles_categoricos(df: pd.DataFrame) -> dict[str, list[str]]:
    return {c: sorted(df[c].astype(str).unique().tolist()) for c in FEATURES_CAT}


def matriz(
    df: pd.DataFrame, niveles: dict[str, list[str]], columnas: list[str] | None = None
) -> pd.DataFrame:
    """Matriz de features con categorías fijadas a las del entrenamiento (desconocidas → NaN)."""
    columnas = columnas or FEATURES
    X = pd.DataFrame(index=df.index)
    for c in columnas:
        if c in niveles:
            s = df[c].astype(str)
            X[c] = pd.Categorical(s.where(s.isin(niveles[c])), categories=niveles[c])
        else:
            X[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    return X


def filas_a_mob(tabla: pd.DataFrame, mob: int) -> pd.DataFrame:
    """Una fila por cohorte con sus features estáticas, puesta en el MOB indicado.

    Sirve para proyectar la curva esperada de cohortes que aún no llegan a ese MOB.
    """
    cols = [*LLAVE_COHORTE, "FAMILIA", "SCORE_ORD", "VINTAGE_MES", "MOB_ORIGEN", *ORIGINACION]
    base = tabla.drop_duplicates(LLAVE_COHORTE)[cols].copy()
    base["MOB"] = int(mob)
    return calcular_originacion(base).reset_index(drop=True)


def tabla_desde_cohortes(df: pd.DataFrame, cfg: dict[str, Any] | None = None) -> pd.DataFrame:
    """Features de cohortes dadas solo por su llave, el MOB y su originación.

    `df` trae LLAVE_COHORTE, MOB, ACCOUNTS_0, CREDITLINESAMTS_0 y ENRAMTS_0 (y MOB_ORIGEN, que
    vale 0 si falta). Lo usan el modelo pyfunc de MLflow y la API para cohortes fuera del corte.
    """
    out = df.copy()
    if "MOB_ORIGEN" not in out.columns:
        out["MOB_ORIGEN"] = 0
    return calcular_originacion(agregar_familia_score(out, cfg))


def agregar_rezagos_over30(
    tabla: pd.DataFrame, rezagos: int = 3, adelanto: int = 3
) -> pd.DataFrame:
    """Rezagos de la tasa Over30 observada (t, t−1, t−2) y objetivo en t+adelanto (M4).

    Solo usa MOB contiguos dentro de la cohorte: si falta un MOB intermedio, los rezagos, la
    pendiente y el objetivo de esa fila quedan en NaN.
    """
    out = tabla.sort_values(LLAVE_FILA).copy()
    g = out.groupby(LLAVE_COHORTE, sort=False, observed=True)
    nombres = ["O30_T", *(f"O30_T{i}" for i in range(1, rezagos))]
    for i, n in enumerate(nombres):
        out[n] = g["tasa_over30"].shift(i)
    mob_lag = g["MOB"].shift(rezagos - 1)
    out.loc[mob_lag != out["MOB"] - (rezagos - 1), nombres] = np.nan
    out["O30_PENDIENTE"] = (out["O30_T"] - out[nombres[-1]]) / max(rezagos - 1, 1)
    out["O30_OBJETIVO"] = g["tasa_over30"].shift(-adelanto)
    out["PESO_OBJETIVO"] = g["ENRACCTS"].shift(-adelanto)
    mob_obj = g["MOB"].shift(-adelanto)
    out.loc[mob_obj != out["MOB"] + adelanto, ["O30_OBJETIVO", "PESO_OBJETIVO"]] = np.nan
    out["PERF_MONTH_OBJETIVO"] = perf_month(out["VINTAGE"], out["MOB"] + adelanto)
    return out
