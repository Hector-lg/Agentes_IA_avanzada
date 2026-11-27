"""Calibración conformal de la banda 5–95 del MPDC (CQR, RN-04; Romano et al., 2019).

Los modelos cuantílicos solos cubren menos de lo prometido fuera de tiempo (~84% en vez de 90%).
CQR corrige la banda con los residuos del backtest: para cada tramo de MOB (y, si hay suficientes
residuos, para cada familia × tramo) calcula un desplazamiento Q y usa [q05 − Q, q95 + Q].

- `no_conformidad`: puntaje E = max(q05 − y, y − q95); y queda dentro de la banda si E ≤ 0.
- `offsets_cqr`: Q por tramo y por familia × tramo a partir de los puntajes.
- `cobertura_validacion_cruzada`: cobertura honesta, calibrando sin el origen que se evalúa.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

# Mínimo de residuos para calibrar una familia × tramo por separado; si no, se usa el del tramo
MIN_FILAS_CQR_FAMILIA = 500


def tramo_de_mob(mob: np.ndarray, tramos: list[list[int]]) -> np.ndarray:
    """Índice del tramo de MOB de cada fila; lo que no cae en ningún tramo va al último."""
    mob = np.asarray(mob)
    out = np.full(len(mob), len(tramos) - 1, dtype="int64")
    for i, (lo, hi) in enumerate(tramos):
        out[(mob >= lo) & (mob <= hi)] = i
    return out


def no_conformidad(y: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    """E = max(lo − y, y − hi). Es negativo dentro de la banda y positivo fuera."""
    y = np.asarray(y, dtype="float64")
    return np.maximum(np.asarray(lo) - y, y - np.asarray(hi))


def cuantil_conformal(e: np.ndarray, alpha: float) -> float:
    """Cuantil ⌈(n + 1)(1 − α)⌉ / n de los puntajes: cobertura ≥ 1 − α si son intercambiables."""
    e = np.sort(np.asarray(e, dtype="float64"))
    n = len(e)
    if n == 0:
        return 0.0
    k = min(n, math.ceil((n + 1) * (1 - alpha)))
    return float(e[k - 1])


def offsets_cqr(e: pd.DataFrame, n_tramos: int, alpha: float) -> dict[str, Any]:
    """Q por tramo de MOB y, con al menos MIN_FILAS_CQR_FAMILIA residuos, por familia × tramo.

    `e` tiene columnas `tramo`, `familia` y `e` (puntaje de no conformidad).
    """
    por_tramo = []
    for t in range(n_tramos):
        et = e.loc[e["tramo"] == t, "e"].to_numpy()
        por_tramo.append(cuantil_conformal(et, alpha) if len(et) else 0.0)
    por_familia: dict[str, list[float | None]] = {}
    for fam, g in e.groupby("familia"):
        vals: list[float | None] = []
        for t in range(n_tramos):
            et = g.loc[g["tramo"] == t, "e"].to_numpy()
            vals.append(cuantil_conformal(et, alpha) if len(et) >= MIN_FILAS_CQR_FAMILIA else None)
        por_familia[str(fam)] = vals
    return {"tramo": por_tramo, "familia_tramo": por_familia}


def offsets_por_fila(tramo: np.ndarray, familia: np.ndarray, offsets: dict[str, Any]) -> np.ndarray:
    """Q de cada fila: el de su familia × tramo si se calibró; si no, el de su tramo."""
    tramo = np.asarray(tramo, dtype="int64")
    q = np.asarray(offsets["tramo"], dtype="float64")[tramo]
    familia = np.asarray(familia).astype(str)
    for fam, vals in offsets.get("familia_tramo", {}).items():
        v = np.asarray([np.nan if x is None else x for x in vals], dtype="float64")[tramo]
        sel = (familia == fam) & ~np.isnan(v)
        q[sel] = v[sel]
    return q


def aplicar_cqr(pred: np.ndarray, q: np.ndarray) -> np.ndarray:
    """Banda CQR [q05 − Q, q95 + Q]. q50 no cambia y la banda siempre lo contiene."""
    out = pred.copy()
    out[:, 0] = np.minimum(pred[:, 0] - q, pred[:, 1])
    out[:, -1] = np.maximum(pred[:, -1] + q, pred[:, 1])
    return out


def cobertura_validacion_cruzada(e: pd.DataFrame, n_tramos: int, alpha: float) -> dict[str, Any]:
    """Cobertura de la banda CQR medida sin hacer trampa: en cada origen se calibra con los demás.

    `e` tiene columnas `origen`, `tramo`, `familia`, `e` y `w` (exposición).
    """
    cob, cob_w, por_fam = [], [], {}
    for o in e["origen"].unique():
        cal, prueba = e.loc[e["origen"] != o], e.loc[e["origen"] == o]
        if cal.empty:
            continue
        q = offsets_por_fila(
            prueba["tramo"].to_numpy(),
            prueba["familia"].to_numpy(),
            offsets_cqr(cal, n_tramos, alpha),
        )
        dentro = (prueba["e"].to_numpy() <= q).astype("float64")
        w = prueba["w"].to_numpy(dtype="float64")
        cob.append(dentro.mean())
        cob_w.append(np.sum(dentro * w) / np.sum(w))
        for fam, idx in prueba.groupby("familia").indices.items():
            por_fam.setdefault(str(fam), []).append(float(dentro[idx].mean()))
    return {
        "cobertura_cqr": float(np.mean(cob)) if cob else float("nan"),
        "cobertura_cqr_ponderada": float(np.mean(cob_w)) if cob_w else float("nan"),
        "cobertura_cqr_por_familia": {k: float(np.mean(v)) for k, v in por_fam.items()},
    }
