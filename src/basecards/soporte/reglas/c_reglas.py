"""Reglas de comparación entre cortes C-01 … C-03."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from basecards.soporte.config import cargar_reglas
from basecards.soporte.hallazgos import Hallazgo, muestra_filas, pct

LLAVE = [
    "VINTAGE",
    "STRATEGY",
    "STRATEGY_SEGMENT",
    "PRODUCT",
    "CHANNEL",
    "AGE_TAG",
    "SCORE_TAG",
    "MOB",
]

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


def _sev(reglas: dict[str, Any], regla_id: str) -> str:
    return reglas["severidades"][regla_id]


def _indices_por_llave(df: pd.DataFrame, flagged: pd.DataFrame) -> list[Any]:
    """Devuelve índices del df actual cuyas llaves aparecen en flagged."""
    if flagged.empty:
        return []
    df2 = df.reset_index(names="_i")
    hit = df2.merge(flagged.drop_duplicates(), on=LLAVE, how="inner")
    return hit["_i"].tolist()


def _merge_metricas(df: pd.DataFrame, df_anterior: pd.DataFrame) -> pd.DataFrame:
    cols = [c for c in LLAVE + METRICAS if c in df.columns]
    a = df[cols].copy()
    b = df_anterior[[c for c in cols if c in df_anterior.columns]].copy()
    return a.merge(b, on=LLAVE, how="inner", suffixes=("", "_prev"))


def evaluar_c(
    df: pd.DataFrame,
    df_anterior: pd.DataFrame | None,
    reglas: dict[str, Any] | None = None,
) -> list[Hallazgo]:
    """C-01 reexpresión, C-02 límites MAD, C-03 Isolation Forest sobre variaciones."""
    if df_anterior is None or df_anterior.empty:
        return []

    cfg = reglas or cargar_reglas()
    hallazgos: list[Hallazgo] = []

    h1 = _c01_reexpresion(df, df_anterior, cfg)
    if h1:
        hallazgos.append(h1)

    h2 = _c02_mad(df, df_anterior, cfg)
    if h2:
        hallazgos.append(h2)

    h3 = _c03_isolation_forest(df, df_anterior, cfg)
    if h3:
        hallazgos.append(h3)

    return hallazgos


def _c01_reexpresion(
    df: pd.DataFrame, df_anterior: pd.DataFrame, cfg: dict[str, Any]
) -> Hallazgo | None:
    tol = float(cfg.get("tolerancia_relativa_reexpresion", 1e-6))
    merged = _merge_metricas(df, df_anterior)
    if merged.empty:
        return None

    mask = pd.Series(False, index=merged.index)
    for col in METRICAS:
        prev = f"{col}_prev"
        if prev not in merged.columns:
            continue
        # float64: en columnas Int64 con ceros, clip(lower=1e-9) lanza TypeError
        cur = pd.to_numeric(merged[col], errors="coerce").astype("float64")
        ant = pd.to_numeric(merged[prev], errors="coerce").astype("float64")
        denom = ant.abs().clip(lower=1e-9)
        rel = (cur - ant).abs() / denom
        mask |= (rel > tol).fillna(False)

    if not mask.any():
        return None

    flagged = merged.loc[mask, LLAVE]
    idx_df = _indices_por_llave(df, flagged)
    return Hallazgo(
        regla_id="C-01",
        severidad=_sev(cfg, "C-01"),
        descripcion="Reexpresión: celdas del corte anterior cambiaron",
        filas_afectadas=len(idx_df),
        total_filas=len(df),
        pct_filas=pct(len(idx_df), len(df)),
        monto_involucrado=float(
            pd.to_numeric(df.loc[idx_df, "NCLAMTS"], errors="coerce").fillna(0).sum()
        )
        if idx_df
        else 0.0,
        muestra=muestra_filas(df, idx_df, cfg.get("muestra_filas", 20)),
        detalle={"filas_comparadas": len(merged)},
    )


def _c02_mad(df: pd.DataFrame, df_anterior: pd.DataFrame, cfg: dict[str, Any]) -> Hallazgo | None:
    """Límites de control robustos sobre variación de totales por cosecha (|z| > 3.5)."""
    umbral = float(cfg.get("mad_z_umbral", 3.5))
    cur = df.groupby("VINTAGE", dropna=False)[METRICAS].apply(
        lambda g: g.apply(pd.to_numeric, errors="coerce").sum()
    )
    ant = df_anterior.groupby("VINTAGE", dropna=False)[METRICAS].apply(
        lambda g: g.apply(pd.to_numeric, errors="coerce").sum()
    )
    comunes = cur.index.intersection(ant.index)
    if len(comunes) == 0:
        return None

    var = []
    for v in comunes:
        csum = float(cur.loc[v].sum())
        asum = float(ant.loc[v].sum())
        denom = max(abs(asum), 1e-9)
        var.append((int(v), (csum - asum) / denom))

    arr = np.array([x[1] for x in var], dtype=float)
    med = float(np.median(arr))
    mad = float(np.median(np.abs(arr - med)))
    if mad < 1e-12:
        return None
    z = 0.6745 * (arr - med) / mad
    alertas = [var[i][0] for i, zi in enumerate(z) if abs(zi) > umbral]
    if not alertas:
        return None

    idx = df.index[df["VINTAGE"].isin(alertas)].tolist()
    return Hallazgo(
        regla_id="C-02",
        severidad=_sev(cfg, "C-02"),
        descripcion="Variación de totales por cosecha fuera de límites MAD",
        filas_afectadas=len(idx),
        total_filas=len(df),
        pct_filas=pct(len(idx), len(df)),
        muestra=muestra_filas(df, idx, cfg.get("muestra_filas", 20)),
        detalle={"cosechas": alertas, "mad": mad, "mediana": med},
    )


def _c03_isolation_forest(
    df: pd.DataFrame, df_anterior: pd.DataFrame, cfg: dict[str, Any]
) -> Hallazgo | None:
    """Isolation Forest sobre variaciones por celda (métricas)."""
    try:
        from sklearn.ensemble import IsolationForest
    except ImportError:
        return None

    merged = _merge_metricas(df, df_anterior)
    if len(merged) < 10:
        return None

    feats = []
    for col in METRICAS:
        prev = f"{col}_prev"
        if prev not in merged.columns:
            continue
        cur = pd.to_numeric(merged[col], errors="coerce").fillna(0)
        ant = pd.to_numeric(merged[prev], errors="coerce").fillna(0)
        denom = ant.abs().clip(lower=1.0)
        feats.append(((cur - ant) / denom).to_numpy())
    if not feats:
        return None

    X = np.column_stack(feats)
    clf = IsolationForest(
        n_estimators=100,
        contamination=0.01,
        random_state=42,
        n_jobs=1,
    )
    pred = clf.fit_predict(X)
    anom = pred == -1
    if not anom.any():
        return None

    flagged = merged.loc[anom, LLAVE]
    idx_df = _indices_por_llave(df, flagged)
    return Hallazgo(
        regla_id="C-03",
        severidad=_sev(cfg, "C-03"),
        descripcion="Anomalías de carga (Isolation Forest) sobre variaciones por celda",
        filas_afectadas=len(idx_df),
        total_filas=len(df),
        pct_filas=pct(len(idx_df), len(df)),
        muestra=muestra_filas(df, idx_df, cfg.get("muestra_filas", 20)),
        detalle={"n_anomalias": int(anom.sum())},
    )
