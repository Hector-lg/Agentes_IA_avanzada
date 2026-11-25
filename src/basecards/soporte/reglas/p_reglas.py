"""Reglas de plausibilidad P-01 … P-07."""

from __future__ import annotations

from typing import Any

import pandas as pd

from basecards.soporte.config import cargar_indicadores, cargar_reglas
from basecards.soporte.hallazgos import Hallazgo, muestra_filas, pct

# Mapeo AGE_TAG ↔ etiquetas cortas que pueden aparecer en STRATEGY_SEGMENT (P-05)
AGE_COHERENCIA = {
    "01| Young < 25": {"Young < 25", "01| Young < 25"},
    "02| Young 25-30": {"Young 25-30", "02| Young 25-30"},
    "03| Mature >30": {"Mature >30", "03| Mature >30"},
}


def _sev(reglas: dict[str, Any], regla_id: str) -> str:
    return reglas["severidades"][regla_id]


def _hallazgo(
    regla_id: str,
    descripcion: str,
    df: pd.DataFrame,
    mask: pd.Series,
    reglas: dict[str, Any],
    detalle: dict[str, Any] | None = None,
) -> Hallazgo | None:
    idx = df.index[mask.fillna(False)]
    if len(idx) == 0:
        return None
    monto = float(pd.to_numeric(df.loc[idx, "NCLAMTS"], errors="coerce").fillna(0).sum())
    return Hallazgo(
        regla_id=regla_id,
        severidad=_sev(reglas, regla_id),
        descripcion=descripcion,
        filas_afectadas=len(idx),
        total_filas=len(df),
        pct_filas=pct(len(idx), len(df)),
        monto_involucrado=monto,
        muestra=muestra_filas(df, idx, reglas.get("muestra_filas", 20)),
        detalle=detalle or {},
    )


def evaluar_p(df: pd.DataFrame, reglas: dict[str, Any] | None = None) -> list[Hallazgo]:
    cfg = reglas or cargar_reglas()
    ind = cargar_indicadores()
    hallazgos: list[Hallazgo] = []

    enr_a = pd.to_numeric(df["ENRACCTS"], errors="coerce")
    acc = pd.to_numeric(df["ACCOUNTS"], errors="coerce")
    o30a = pd.to_numeric(df["OVER30ACCTS"], errors="coerce")
    o30m = pd.to_numeric(df["OVER30AMTS"], errors="coerce")
    enrm = pd.to_numeric(df["ENRAMTS"], errors="coerce")
    rec = pd.to_numeric(df["RECOVERYAMTS"], errors="coerce")
    gcl = pd.to_numeric(df["GCLAMTS"], errors="coerce")

    checks = [
        ("P-01", "ENRACCTS ≤ ACCOUNTS", enr_a > acc),
        ("P-02", "OVER30ACCTS ≤ ENRACCTS", o30a > enr_a),
        ("P-03", "OVER30AMTS ≤ ENRAMTS", o30m > enrm),
        ("P-04", "RECOVERYAMTS ≤ GCLAMTS", rec > gcl),
    ]
    for rid, desc, mask in checks:
        h = _hallazgo(rid, desc, df, mask, cfg)
        if h:
            hallazgos.append(h)

    # P-05: si el segmento menciona edad, debe alinearse con AGE_TAG
    def incoherente_edad(row: pd.Series) -> bool:
        seg = str(row["STRATEGY_SEGMENT"]) if pd.notna(row["STRATEGY_SEGMENT"]) else ""
        age = str(row["AGE_TAG"]) if pd.notna(row["AGE_TAG"]) else ""
        etiquetas = AGE_COHERENCIA.get(age, set())
        # Solo valida cuando el segmento es explícitamente de edad
        if seg in {v for vals in AGE_COHERENCIA.values() for v in vals}:
            return seg not in etiquetas
        return False

    malo = df.apply(incoherente_edad, axis=1)
    h = _hallazgo("P-05", "AGE_TAG incoherente con segmento de edad", df, malo, cfg)
    if h:
        hallazgos.append(h)

    # P-06: Costco / Home Depot coherentes entre PRODUCT y CHANNEL / segmento
    prod = df["PRODUCT"].astype("string")
    chan = df["CHANNEL"].astype("string")
    seg = df["STRATEGY_SEGMENT"].astype("string")
    malo_costco = (prod == "Costco") & (chan != "Costco") & ~seg.str.contains("Costco", na=False)
    malo_hd = (
        (prod == "Home Depot")
        & (chan != "Home Depot")
        & ~seg.str.contains("Home Depot", na=False)
    )
    # También: canal Costco/HD con producto distinto
    malo_canal = ((chan == "Costco") & (prod != "Costco")) | (
        (chan == "Home Depot") & (prod != "Home Depot")
    )
    malo = malo_costco | malo_hd | malo_canal
    h = _hallazgo("P-06", "PRODUCT/CHANNEL incoherentes con Costco o Home Depot", df, malo, cfg)
    if h:
        hallazgos.append(h)

    # P-07: solo si acumulado=true
    if ind.get("acumulado", False):
        hallazgos.extend(_p07_no_decrecen(df, cfg))

    return hallazgos


def _p07_no_decrecen(df: pd.DataFrame, cfg: dict[str, Any]) -> list[Hallazgo]:
    dims = [
        "VINTAGE",
        "STRATEGY",
        "STRATEGY_SEGMENT",
        "PRODUCT",
        "CHANNEL",
        "AGE_TAG",
        "SCORE_TAG",
    ]
    metricas = ["OVER30ACCTS", "OVER30AMTS", "GCLAMTS", "NCLAMTS", "RECOVERYAMTS"]
    idx_malos: list[Any] = []
    for _, g in df.sort_values("MOB").groupby(dims, dropna=False):
        for col in metricas:
            s = pd.to_numeric(g[col], errors="coerce")
            if s.diff().fillna(0).lt(-1e-6).any():
                idx_malos.extend(g.index.tolist())
                break
    if not idx_malos:
        return []
    return [
        Hallazgo(
            regla_id="P-07",
            severidad=_sev(cfg, "P-07"),
            descripcion="Métricas de riesgo acumuladas decrecen con el MOB",
            filas_afectadas=len(set(idx_malos)),
            total_filas=len(df),
            pct_filas=pct(len(set(idx_malos)), len(df)),
            muestra=muestra_filas(df, list(dict.fromkeys(idx_malos)), cfg.get("muestra_filas", 20)),
        )
    ]
