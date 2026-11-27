"""Métricas de evaluación del MPDC (PRD §6.4 y §12.1).

Las métricas ponderadas usan la exposición del indicador: ENRAMTS para la tasa NCL, ENRACCTS para
la tasa Over30 y ACCOUNTS para el margen por cuenta.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy.stats import spearmanr


def _pesos(w: np.ndarray | None, n: int) -> np.ndarray:
    return np.ones(n) if w is None else np.asarray(w, dtype="float64")


def pinball(y: np.ndarray, q: np.ndarray, alpha: float, w: np.ndarray | None = None) -> float:
    """Pérdida pinball del cuantil `alpha`, ponderada. Es la pérdida que minimiza cada modelo."""
    y = np.asarray(y, dtype="float64")
    d = y - np.asarray(q, dtype="float64")
    perdida = np.maximum(alpha * d, (alpha - 1) * d)
    ww = _pesos(w, len(y))
    return float(np.sum(ww * perdida) / np.sum(ww))


def mae(y: np.ndarray, p: np.ndarray, w: np.ndarray | None = None) -> float:
    ww = _pesos(w, len(y))
    return float(np.sum(ww * np.abs(np.asarray(y) - np.asarray(p))) / np.sum(ww))


def rmse(y: np.ndarray, p: np.ndarray, w: np.ndarray | None = None) -> float:
    ww = _pesos(w, len(y))
    return float(math.sqrt(np.sum(ww * (np.asarray(y) - np.asarray(p)) ** 2) / np.sum(ww)))


def cobertura(y: np.ndarray, lo: np.ndarray, hi: np.ndarray, w: np.ndarray | None = None) -> float:
    """Fracción (ponderada) de observaciones dentro de la banda [lo, hi]."""
    y = np.asarray(y, dtype="float64")
    if len(y) == 0:
        return float("nan")
    dentro = ((y >= np.asarray(lo)) & (y <= np.asarray(hi))).astype("float64")
    ww = _pesos(w, len(y))
    return float(np.sum(ww * dentro) / np.sum(ww))


def cuantil_ponderado(x: np.ndarray, w: np.ndarray, qs: list[float]) -> np.ndarray:
    """Cuantiles empíricos ponderados.

    Cada valor se ubica a la mitad de su peso acumulado (interpolación de Hazen), así que con pesos
    iguales coincide con el cuantil usual: la mediana de [1, 2, 3, 4] es 2.5.
    """
    x = np.asarray(x, dtype="float64")
    w = np.asarray(w, dtype="float64")
    m = w > 0
    x, w = x[m], w[m]
    if len(x) == 0:
        return np.full(len(qs), np.nan)
    orden = np.argsort(x, kind="stable")
    x, w = x[orden], w[orden]
    acum = np.cumsum(w)
    return np.interp(qs, (acum - 0.5 * w) / acum[-1], x)


def spearman_ranking(
    df: pd.DataFrame, pred: str, obs: str, peso: str, por: list[str], min_grupos: int = 3
) -> float:
    """Spearman entre el ranking predicho y el observado de los grupos `por`.

    Cada grupo se resume con su promedio ponderado por `peso` (razón de sumas).
    """
    g = df.assign(_p=df[pred] * df[peso], _o=df[obs] * df[peso]).groupby(por, observed=True)
    agg = g[["_p", "_o", peso]].sum()
    agg = agg[agg[peso] > 0]
    if len(agg) < min_grupos:
        return float("nan")
    return float(spearmanr(agg["_p"] / agg[peso], agg["_o"] / agg[peso]).statistic)


def mape_agregado(df: pd.DataFrame, pred: str, obs: str, peso: str, por: list[str]) -> float:
    """MAPE, en %, de los promedios ponderados por grupo (razón de sumas, no promedio de razones)."""
    g = df.assign(_p=df[pred] * df[peso], _o=df[obs] * df[peso]).groupby(por, observed=True)
    agg = g[["_p", "_o", peso]].sum()
    agg = agg[(agg[peso] > 0) & (agg["_o"] > 0)]
    if agg.empty:
        return float("nan")
    p, o = agg["_p"] / agg[peso], agg["_o"] / agg[peso]
    return float(100 * np.mean(np.abs(p - o) / o))
