"""Reglas del diccionario R-01 … R-18."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from basecards.soporte.carga import mob_max
from basecards.soporte.config import (
    catalogo_scores,
    catalogo_segmentos,
    cargar_reglas,
    strategies_permitidas,
)
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


def _sev(reglas: dict[str, Any], regla_id: str) -> str:
    return reglas["severidades"][regla_id]


def _hallazgo(
    regla_id: str,
    descripcion: str,
    df: pd.DataFrame,
    mask: pd.Series | None,
    reglas: dict[str, Any],
    monto_col: str = "NCLAMTS",
    detalle: dict[str, Any] | None = None,
) -> Hallazgo | None:
    total = len(df)
    if mask is None:
        return None
    idx = df.index[mask.fillna(False)]
    if len(idx) == 0:
        return None
    monto = 0.0
    if monto_col in df.columns:
        monto = float(pd.to_numeric(df.loc[idx, monto_col], errors="coerce").fillna(0).sum())
    return Hallazgo(
        regla_id=regla_id,
        severidad=_sev(reglas, regla_id),
        descripcion=descripcion,
        filas_afectadas=len(idx),
        total_filas=total,
        pct_filas=pct(len(idx), total),
        monto_involucrado=monto,
        muestra=muestra_filas(df, idx, reglas.get("muestra_filas", 20)),
        detalle=detalle or {},
    )


def evaluar_esquema(df: pd.DataFrame, reglas: dict[str, Any] | None = None) -> list[Hallazgo]:
    """R-01, R-02, R-13, R-18 — fallas críticas de esquema."""
    cfg = reglas or cargar_reglas()
    hallazgos: list[Hallazgo] = []
    esperadas = list(cfg["columnas"])
    total = max(len(df), 1)

    faltantes = [c for c in esperadas if c not in df.columns]
    extras = [c for c in df.columns if c not in esperadas]
    if faltantes or len(df.columns) != len(esperadas):
        hallazgos.append(
            Hallazgo(
                regla_id="R-01",
                severidad=_sev(cfg, "R-01"),
                descripcion="El archivo no tiene exactamente las 22 columnas del diccionario",
                filas_afectadas=total,
                total_filas=total,
                pct_filas=100.0,
                detalle={"faltantes": faltantes, "extras": extras},
            )
        )
        return hallazgos  # sin columnas no se puede seguir tipando

    # R-02: tipos — valores no casteables a entero/real
    for col in cfg["tipos"]["enteros"]:
        raw = df[col]
        num = pd.to_numeric(raw, errors="coerce")
        # enteros: si hay valor no-nulo que no es entero
        malo = raw.notna() & (num.isna() | (num != num.round()))
        h = _hallazgo("R-02", f"Columna {col} no es entera", df, malo, cfg, detalle={"columna": col})
        if h:
            hallazgos.append(h)

    for col in cfg["tipos"]["reales"]:
        raw = df[col]
        num = pd.to_numeric(raw, errors="coerce")
        malo = raw.notna() & num.isna()
        h = _hallazgo("R-02", f"Columna {col} no es numérica", df, malo, cfg, detalle={"columna": col})
        if h:
            hallazgos.append(h)

    # R-13 MOB entero >= 0 (también cubierto en parte por R-02)
    if "MOB" in df.columns:
        mob = pd.to_numeric(df["MOB"], errors="coerce")
        malo = mob.isna() | (mob < 0) | (mob != mob.round())
        h = _hallazgo("R-13", "MOB debe ser entero >= 0", df, malo, cfg)
        if h:
            hallazgos.append(h)

    # R-18 sin nulos
    if set(esperadas).issubset(df.columns):
        nulos = df[esperadas].isna().any(axis=1)
        h = _hallazgo("R-18", "Hay valores nulos en columnas del diccionario", df, nulos, cfg)
        if h:
            hallazgos.append(h)

    return hallazgos


def _evaluar_r03_r18(df: pd.DataFrame, fecha_corte: int, cfg: dict[str, Any]) -> list[Hallazgo]:
    hallazgos: list[Hallazgo] = []
    tol = float(cfg.get("tolerancia_absoluta_montos", 0.01))
    max_m = float(cfg.get("metricas_rango_max", 99_999_999_999_999))

    # R-03 VINTAGE
    v = df["VINTAGE"].astype("Int64")
    mes = v % 100
    malo = v.isna() | (v < 202401) | (v > 202609) | (mes < 1) | (mes > 12)
    h = _hallazgo("R-03", "VINTAGE fuera de 202401–202609 o mes inválido", df, malo, cfg)
    if h:
        hallazgos.append(h)

    # R-04 rango de métricas
    metricas = cfg["tipos"]["enteros"][2:] + cfg["tipos"]["reales"]  # sin VINTAGE/MOB
    for col in metricas:
        if col not in df.columns:
            continue
        s = pd.to_numeric(df[col], errors="coerce")
        malo = s.notna() & ((s < 0) | (s > max_m))
        h = _hallazgo("R-04", f"{col} fuera de [0, {max_m:.0f}]", df, malo, cfg, monto_col=col)
        if h:
            hallazgos.append(h)

    # R-05..R-07 catálogos simples
    for regla, col in [("R-05", "PRODUCT"), ("R-06", "CHANNEL"), ("R-07", "AGE_TAG")]:
        cat = set(cfg["catalogos"][col])
        malo = df[col].notna() & ~df[col].isin(cat)
        h = _hallazgo(regla, f"{col} fuera de catálogo", df, malo, cfg)
        if h:
            hallazgos.append(h)

    # R-08 STRATEGY vs VINTAGE
    permitidas = [
        strategies_permitidas(int(vi), cfg) if pd.notna(vi) else set()
        for vi in df["VINTAGE"]
    ]
    malo = pd.Series(
        [
            pd.notna(s) and pd.notna(vi) and s not in perm
            for s, vi, perm in zip(df["STRATEGY"], df["VINTAGE"], permitidas, strict=True)
        ],
        index=df.index,
    )
    h = _hallazgo("R-08", "STRATEGY no corresponde al rango de VINTAGE", df, malo, cfg)
    if h:
        hallazgos.append(h)

    # R-09 mezcla (advertencia a nivel cosecha)
    h9 = _regla_r09(df, cfg)
    if h9:
        hallazgos.append(h9)

    # R-10 / R-11 por familia
    segs_ok = [
        pd.isna(st)
        or pd.isna(seg)
        or seg in catalogo_segmentos(str(st), cfg)
        for st, seg in zip(df["STRATEGY"], df["STRATEGY_SEGMENT"], strict=True)
    ]
    malo = ~pd.Series(segs_ok, index=df.index)
    h = _hallazgo("R-10", "STRATEGY_SEGMENT fuera del catálogo de su familia", df, malo, cfg)
    if h:
        hallazgos.append(h)

    scores_ok = [
        pd.isna(st)
        or pd.isna(sc)
        or sc in catalogo_scores(str(st), cfg)
        for st, sc in zip(df["STRATEGY"], df["SCORE_TAG"], strict=True)
    ]
    malo = ~pd.Series(scores_ok, index=df.index)
    h = _hallazgo("R-11", "SCORE_TAG fuera del catálogo de su familia", df, malo, cfg)
    if h:
        hallazgos.append(h)

    # R-12 MOB <= mob_max
    mmax = df["VINTAGE"].map(
        lambda vi: mob_max(int(vi), fecha_corte) if pd.notna(vi) else -1
    )
    mob = df["MOB"].astype("Int64")
    malo = mob.notna() & (mob > mmax)
    h = _hallazgo("R-12", "MOB supera los meses entre VINTAGE y la fecha de corte", df, malo, cfg)
    if h:
        hallazgos.append(h)

    # R-14 NCL = GCL - RECOVERY
    ncl = pd.to_numeric(df["NCLAMTS"], errors="coerce")
    gcl = pd.to_numeric(df["GCLAMTS"], errors="coerce")
    rec = pd.to_numeric(df["RECOVERYAMTS"], errors="coerce")
    malo = (ncl - (gcl - rec)).abs() > tol
    h = _hallazgo("R-14", "NCLAMTS ≠ GCLAMTS − RECOVERYAMTS", df, malo, cfg)
    if h:
        hallazgos.append(h)

    # R-15 EXPENSES_TOTAL
    total_e = pd.to_numeric(df["EXPENSES_TOTAL"], errors="coerce")
    acq = pd.to_numeric(df["EXPENSES_ACQ"], errors="coerce")
    var = pd.to_numeric(df["EXPENSES_ECM_VAR"], errors="coerce")
    fix = pd.to_numeric(df["EXPENSES_ECM_FIX"], errors="coerce")
    malo = (total_e - (acq + var + fix)).abs() > tol
    h = _hallazgo("R-15", "EXPENSES_TOTAL ≠ suma de componentes", df, malo, cfg)
    if h:
        hallazgos.append(h)

    # R-16 EXPENSES_ACQ = 0 si MOB ≠ 0
    malo = (mob != 0) & (acq.abs() > tol)
    h = _hallazgo("R-16", "EXPENSES_ACQ debe ser 0 cuando MOB ≠ 0", df, malo, cfg)
    if h:
        hallazgos.append(h)

    # R-17 llave única + cosechas + MOB contiguos
    hallazgos.extend(_regla_r17(df, fecha_corte, cfg))

    return hallazgos


def _regla_r09(df: pd.DataFrame, cfg: dict[str, Any]) -> Hallazgo | None:
    """Mezcla r.Reca champ / DARIA por cosecha en MOB 0 (banda binomial 99%)."""
    mob0 = df[df["MOB"] == 0].copy()
    if mob0.empty:
        return None

    z = 2.5758293035489004  # Φ^{-1}(0.995), banda binomial bilateral al 99%
    fallos: list[dict[str, Any]] = []
    indices: list[Any] = []

    for banda in cfg["mezcla_strategy"]:
        sub = mob0[
            (mob0["VINTAGE"] >= banda["vintage_min"])
            & (mob0["VINTAGE"] <= banda["vintage_max"])
        ]
        for vintage, g in sub.groupby("VINTAGE"):
            n = float(pd.to_numeric(g["ACCOUNTS"], errors="coerce").fillna(0).sum())
            if n <= 0:
                continue
            chal = g[g["STRATEGY"] == banda["challenger"]]
            k = float(pd.to_numeric(chal["ACCOUNTS"], errors="coerce").fillna(0).sum())
            p = float(banda["p_esperada"])
            phat = k / n
            se = np.sqrt(p * (1 - p) / n)
            lo, hi = p - z * se, p + z * se
            if phat < lo or phat > hi:
                fallos.append(
                    {
                        "VINTAGE": int(vintage),
                        "challenger": banda["challenger"],
                        "p_esperada": p,
                        "p_obs": round(phat, 4),
                        "n_accounts": n,
                    }
                )
                indices.extend(g.index.tolist())

    if not fallos:
        return None
    return Hallazgo(
        regla_id="R-09",
        severidad=_sev(cfg, "R-09"),
        descripcion="Mezcla Reca/DARIA fuera de la banda binomial al 99%",
        filas_afectadas=len(set(indices)),
        total_filas=len(df),
        pct_filas=pct(len(set(indices)), len(df)),
        muestra=muestra_filas(df, list(dict.fromkeys(indices)), cfg.get("muestra_filas", 20)),
        detalle={"cosechas": fallos},
    )


def _regla_r17(df: pd.DataFrame, fecha_corte: int, cfg: dict[str, Any]) -> list[Hallazgo]:
    hallazgos: list[Hallazgo] = []
    llave = [c for c in LLAVE if c in df.columns]
    dup = df.duplicated(llave, keep=False)
    h = _hallazgo("R-17", "Llave de fila duplicada", df, dup, cfg, detalle={"tipo": "duplicado"})
    if h:
        hallazgos.append(h)

    # Cosechas 202401–202609 presentes
    vintages_esperados = set()
    y, m = 2024, 1
    while y * 100 + m <= 202609:
        vintages_esperados.add(y * 100 + m)
        m += 1
        if m > 12:
            m = 1
            y += 1
    presentes = set(df["VINTAGE"].dropna().astype(int).tolist())
    faltan = sorted(vintages_esperados - presentes)
    if faltan:
        hallazgos.append(
            Hallazgo(
                regla_id="R-17",
                severidad="alta",  # completitud = Alta
                descripcion="Faltan cosechas en el cubo",
                filas_afectadas=len(faltan),
                total_filas=len(df),
                pct_filas=pct(len(faltan), len(df)),
                detalle={"tipo": "cosechas_faltantes", "faltan": faltan},
            )
        )

    # MOB contiguos 0..mob_max por cohorte
    dims = [c for c in LLAVE if c != "MOB"]
    huecos = 0
    sample_idx: list[Any] = []
    for _, g in df.groupby(dims, dropna=False):
        mobs = sorted(g["MOB"].dropna().astype(int).unique().tolist())
        if not mobs:
            continue
        vintage = int(g["VINTAGE"].iloc[0])
        esperado = list(range(0, mob_max(vintage, fecha_corte) + 1))
        # Contiguos desde 0 hasta el máximo observado (y sin huecos)
        if mobs[0] != 0 or mobs != list(range(mobs[0], mobs[-1] + 1)):
            huecos += 1
            sample_idx.extend(g.index[:1].tolist())
            continue
        # Si la cohorte declara llegar al mob_max del corte, debe estar completo;
        # si es más corta, solo exigimos contigüidad interna (ya verificada).
        _ = esperado  # documentado; no forzar presencia de todos los MOB futuros

    if huecos:
        hallazgos.append(
            Hallazgo(
                regla_id="R-17",
                severidad=_sev(cfg, "R-17"),
                descripcion="Hay cohortes con MOB no contiguos desde 0",
                filas_afectadas=huecos,
                total_filas=len(df),
                pct_filas=pct(huecos, len(df)),
                muestra=muestra_filas(df, sample_idx, cfg.get("muestra_filas", 20)),
                detalle={"tipo": "mob_no_contiguo", "cohortes_afectadas": huecos},
            )
        )
    return hallazgos


def evaluar_r(
    df: pd.DataFrame,
    fecha_corte: int,
    reglas: dict[str, Any] | None = None,
    *,
    incluir_esquema: bool = False,
) -> list[Hallazgo]:
    """Evalúa R-03..R-17 (y opcionalmente esquema R-01/02/13/18)."""
    cfg = reglas or cargar_reglas()
    out: list[Hallazgo] = []
    if incluir_esquema:
        out.extend(evaluar_esquema(df, cfg))
        criticas = set(cfg.get("criticas_esquema", []))
        if any(h.regla_id in criticas for h in out):
            return out
    out.extend(_evaluar_r03_r18(df, fecha_corte, cfg))
    return out
