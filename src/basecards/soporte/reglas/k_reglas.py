"""Regla K-01: cohorte fuera del intervalo de predicción del MPDC (Manual §5.4)."""

from __future__ import annotations

from typing import Any

import pandas as pd

from basecards.soporte.config import cargar_predicciones_cfg, cargar_reglas
from basecards.soporte.hallazgos import Hallazgo, pct

LLAVE_COHORTE = [
    "VINTAGE",
    "STRATEGY",
    "STRATEGY_SEGMENT",
    "PRODUCT",
    "CHANNEL",
    "AGE_TAG",
    "SCORE_TAG",
]


def _severidad_k01(desviacion: float, monto_exceso: float, umbral_monto: float) -> str:
    """Media ≤1.5, Alta ≤2.5, Crítica >2.5 o monto NCL en exceso ≥ umbral."""
    if desviacion > 2.5 or monto_exceso >= umbral_monto:
        return "critica"
    if desviacion > 1.5:
        return "alta"
    return "media"


def _monto_ncl_exceso(row: pd.Series) -> float:
    """Monto NCL por encima del q95 (aprox. (obs - q95) * ENRAMTS vía nclamts/obs)."""
    obs = float(row["observado"])
    q95 = float(row["q95"])
    nclamts = float(row.get("nclamts", 0.0) or 0.0)
    if obs <= q95 or obs <= 0:
        return 0.0
    # nclamts corresponde al observado; escalar el exceso de tasa a monto
    return max(0.0, nclamts * (obs - q95) / obs)


def evaluar_k01(
    predicciones: pd.DataFrame,
    *,
    reglas: dict[str, Any] | None = None,
    pred_cfg: dict[str, Any] | None = None,
) -> list[Hallazgo]:
    """Crea un hallazgo K-01 por cohorte+indicador fuera de [q05, q95] con ENRACCTS ≥ mínimo."""
    cfg = reglas or cargar_reglas()
    pcfg = pred_cfg or cargar_predicciones_cfg()
    enraccts_min = int(pcfg.get("enraccts_min", 200))
    umbral_monto = float(pcfg.get("umbral_monto_critico", 50_000.0))

    if predicciones.empty:
        return []

    candidatas = predicciones[
        predicciones["fuera_de_intervalo"].astype(bool)
        & (pd.to_numeric(predicciones["enraccts"], errors="coerce").fillna(0) >= enraccts_min)
    ].copy()
    if candidatas.empty:
        return []

    hallazgos: list[Hallazgo] = []
    total = max(len(predicciones), 1)

    # Un hallazgo por cohorte (7 dims) + indicador; si hay varios MOB, se reporta el de mayor |desviación|
    group_cols = LLAVE_COHORTE + ["indicador"]
    for keys, g in candidatas.groupby(group_cols, dropna=False):
        g = g.copy()
        g["_abs_desv"] = pd.to_numeric(g["desviacion"], errors="coerce").fillna(0).abs()
        row = g.loc[g["_abs_desv"].idxmax()]
        desv = float(row["desviacion"])
        monto = _monto_ncl_exceso(row)
        sev = _severidad_k01(abs(desv), monto, umbral_monto)
        # Si la severidad default del YAML es más alta y no es critica por monto, usar la calculada
        cohorte = {c: row[c] for c in LLAVE_COHORTE}
        muestra = [
            {
                **{c: r[c] for c in LLAVE_COHORTE},
                "MOB": int(r["MOB"]),
                "indicador": r["indicador"],
                "observado": float(r["observado"]),
                "q05": float(r["q05"]),
                "q50": float(r["q50"]),
                "q95": float(r["q95"]),
                "desviacion": float(r["desviacion"]),
            }
            for _, r in g.head(20).iterrows()
        ]
        hallazgos.append(
            Hallazgo(
                regla_id="K-01",
                severidad=sev,
                descripcion=(
                    f"Cohorte fuera del intervalo [{row['q05']}, {row['q95']}] "
                    f"en {row['indicador']} (MOB={int(row['MOB'])})"
                ),
                filas_afectadas=len(g),
                total_filas=total,
                pct_filas=pct(len(g), total),
                monto_involucrado=round(monto, 2),
                muestra=muestra,
                detalle={
                    "cohorte": cohorte,
                    "indicador": str(row["indicador"]),
                    "MOB": int(row["MOB"]),
                    "q05": float(row["q05"]),
                    "q50": float(row["q50"]),
                    "q95": float(row["q95"]),
                    "observado": float(row["observado"]),
                    "desviacion": desv,
                    "enraccts": int(row["enraccts"]),
                    "drivers": [row.get("driver_1"), row.get("driver_2"), row.get("driver_3")],
                    "modelo": {
                        "nombre": row.get("modelo_nombre"),
                        "version": row.get("modelo_version"),
                    },
                    "keys": keys if isinstance(keys, tuple) else (keys,),
                },
            )
        )

    # Preferir severidad del cálculo; si YAML dice algo, no lo pisamos salvo info
    _ = cfg.get("severidades", {}).get("K-01")
    return hallazgos
