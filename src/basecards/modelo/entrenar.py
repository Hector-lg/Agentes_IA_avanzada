"""E2 — Datos de entrenamiento y modelos cuantílicos del MPDC (PRD §6.3, Manual §5).

- `cargar_corte`: lee el Parquet del corte (E1) y se niega a usarlo si la validación lo dictaminó
  `no apto`, salvo con `forzar=True`.
- `preparar_datos`: corte anterior derivado (PERF_MONTH < corte) sin las filas que fallan las reglas
  críticas R-14/R-15, con nulos o sin exposición. Con `rapido=True` usa una fracción de las
  cohortes, estratificada por VINTAGE × STRATEGY.
- `ajustar_cuantiles` / `predecir_cuantiles`: un LightGBM cuantílico por indicador y cuantil
  (3 indicadores × q05/q50/q95 = 9 modelos), ponderado por la exposición del indicador.

El backtest con origen móvil está en `basecards.modelo.backtest`.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd

from basecards.ingesta.corte import leer_corte, leer_manifiesto, sumar_meses
from basecards.modelo.config import LLAVE_COHORTE
from basecards.modelo.features import FEATURES, construir_tabla, matriz
from basecards.soporte.config import cargar_reglas

log = logging.getLogger("basecards.modelo.entrenar")


class EntrenamientoError(RuntimeError):
    pass


def etiqueta_cuantil(q: float) -> str:
    """0.05 → "q05", 0.5 → "q50", 0.95 → "q95"."""
    return f"q{round(q * 100):02d}"


def ordenar_cuantiles(pred: np.ndarray) -> np.ndarray:
    """Si los cuantiles se cruzan (q05 > q50 o q50 > q95), se ordenan por fila (Manual §5.2)."""
    return np.sort(pred, axis=1)


# --------------------------------------------------------------------------------------------
# Datos
# --------------------------------------------------------------------------------------------
def cargar_corte(
    corte: int, *, dir_cortes: str | Path | None = None, forzar: bool = False
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Parquet y manifiesto del corte. Nunca se entrena con un corte `no apto` (salvo `forzar`)."""
    df = leer_corte(corte, dir_cortes=dir_cortes)
    try:
        manifiesto = leer_manifiesto(corte, dir_cortes=dir_cortes)
    except FileNotFoundError:
        manifiesto = {}
    dictamen = (manifiesto.get("validacion") or {}).get("dictamen")
    if dictamen == "no apto":
        if not forzar:
            raise EntrenamientoError(
                f"El corte {corte} tiene dictamen 'no apto' en la validación E1: no se entrena "
                "con él. Corrige el CSV o usa --forzar bajo tu responsabilidad."
            )
        log.warning("El corte %s es 'no apto', pero se usa porque se pidió --forzar", corte)
    elif dictamen is None:
        log.warning("El corte %s no tiene dictamen E1 (corre la ingesta con --validar)", corte)
    return df, manifiesto


def preparar_datos(
    df: pd.DataFrame, corte: int, cfg: dict[str, Any], *, rapido: bool = False, semilla: int = 42
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Tabla de entrenamiento y reporte de cuántas filas se excluyen por cada motivo."""
    mcfg = cfg["modelo"]
    tabla = construir_tabla(df, cfg)
    hasta = sumar_meses(corte, -1)
    info: dict[str, Any] = {"filas_corte": len(tabla), "entrenado_hasta": int(hasta)}

    # Corte anterior derivado (S-07): las filas con PERF_MONTH = corte no se ven al entrenar
    m_corte = tabla["PERF_MONTH"] > hasta
    tol = float(cargar_reglas().get("tolerancia_absoluta_montos", 0.01))
    m_r14 = (tabla["NCLAMTS"] - (tabla["GCLAMTS"] - tabla["RECOVERYAMTS"])).abs() > tol
    gastos = tabla["EXPENSES_ACQ"] + tabla["EXPENSES_ECM_VAR"] + tabla["EXPENSES_ECM_FIX"]
    m_r15 = (tabla["EXPENSES_TOTAL"] - gastos).abs() > tol
    numeradores = [o["numerador"] for o in mcfg["objetivos"].values()]
    m_nulos = tabla[[*LLAVE_COHORTE, "MOB", *numeradores]].isna().any(axis=1)
    expo = mcfg["exposicion_minima"]
    m_expo = tabla[expo["columna"]].fillna(0) < float(expo["valor"])

    info["filas_excluidas"] = {
        "perf_month_del_corte_actual": int(m_corte.sum()),
        "regla_critica_R14": int((m_r14 & ~m_corte).sum()),
        "regla_critica_R15": int((m_r15 & ~m_corte).sum()),
        "nulos": int((m_nulos & ~m_corte).sum()),
        f"exposicion_{expo['columna']}_menor_a_{expo['valor']}": int((m_expo & ~m_corte).sum()),
    }
    ent = tabla.loc[~(m_corte | m_r14 | m_r15 | m_nulos | m_expo)].copy()

    if rapido:
        # Muestra estratificada de cohortes completas (Manual §5.1)
        frac = float(mcfg["fraccion_rapido"])
        coh = ent.drop_duplicates(LLAVE_COHORTE)[LLAVE_COHORTE]
        muestra = coh.groupby(["VINTAGE", "STRATEGY"], observed=True, group_keys=False).sample(
            frac=frac, random_state=semilla
        )
        ent = ent.merge(muestra, on=LLAVE_COHORTE, how="inner")
        info["rapido"] = {"fraccion_cohortes": frac}

    info["filas_entrenamiento"] = len(ent)
    info["cohortes_entrenamiento"] = int(ent.drop_duplicates(LLAVE_COHORTE).shape[0])
    return ent.reset_index(drop=True), info


# --------------------------------------------------------------------------------------------
# Modelos
# --------------------------------------------------------------------------------------------
def ajustar_cuantiles(
    tabla: pd.DataFrame,
    niveles: dict[str, list[str]],
    cfg: dict[str, Any],
    n_estimators: int | None = None,
) -> dict[str, list[lgb.Booster]]:
    """Un `LGBMRegressor(objective="quantile")` por indicador y cuantil, ponderado por exposición."""
    mcfg = cfg["modelo"]
    params = dict(mcfg["lgbm"])
    if n_estimators:
        params["n_estimators"] = n_estimators
    modelos: dict[str, list[lgb.Booster]] = {}
    for obj, meta in mcfg["objetivos"].items():
        sub = tabla.loc[tabla[obj].notna() & (tabla[meta["peso"]].fillna(0) > 0)]
        X = matriz(sub, niveles, FEATURES)
        y = sub[obj].to_numpy()
        w = sub[meta["peso"]].to_numpy(dtype="float64")
        modelos[obj] = []
        for q in mcfg["cuantiles"]:
            t0 = time.perf_counter()
            m = lgb.LGBMRegressor(objective="quantile", alpha=q, **params)
            m.fit(X, y, sample_weight=w)
            modelos[obj].append(m.booster_)
            log.info(
                "  %s %s: %d filas, %.1fs",
                obj,
                etiqueta_cuantil(q),
                len(sub),
                time.perf_counter() - t0,
            )
    return modelos


def predecir_cuantiles(
    modelos: dict[str, list[lgb.Booster]], tabla: pd.DataFrame, niveles: dict[str, list[str]]
) -> dict[str, np.ndarray]:
    """Matriz (filas × cuantiles) por indicador, con los cuantiles ordenados por fila."""
    X = matriz(tabla, niveles, FEATURES)
    return {
        obj: ordenar_cuantiles(np.column_stack([b.predict(X) for b in bs]))
        for obj, bs in modelos.items()
    }
