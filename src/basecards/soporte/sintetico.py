"""Generador mínimo de cubo sintético que cumple las reglas (pruebas unitarias)."""

from __future__ import annotations

from typing import Any

import pandas as pd

from basecards.soporte.carga import mob_max
from basecards.soporte.config import cargar_reglas, strategies_permitidas


def _vintages(vmin: int = 202401, vmax: int = 202609) -> list[int]:
    out: list[int] = []
    y, m = divmod(vmin, 100)
    fin_y, fin_m = divmod(vmax, 100)
    while (y, m) <= (fin_y, fin_m):
        out.append(y * 100 + m)
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def _strategy_para(vintage: int, reglas: dict[str, Any]) -> str:
    opts = sorted(strategies_permitidas(vintage, reglas))
    # Preferir champion cuando exista, para R-09 estable
    if "r.Reca champ" in opts:
        return "r.Reca champ"
    return opts[0]


def _segmento_score(strategy: str, reglas: dict[str, Any]) -> tuple[str, str]:
    fam = None
    for nombre, meta in reglas["familias"].items():
        if strategy in meta["strategies"]:
            fam = nombre
            break
    assert fam is not None
    segs = reglas["catalogos"][reglas["familias"][fam]["segmentos"]]
    # Evitar segmentos de edad en el cubo base para no disparar P-05
    preferidos = [s for s in segs if s not in {"Young < 25", "Young 25-30", "Mature >30"}]
    seg = (preferidos or segs)[0]
    score = reglas["catalogos"][reglas["familias"][fam]["scores"]][0]
    return seg, score


def generar_cubo_sintetico(
    fecha_corte: int = 202609,
    mobs_por_cohorte: int = 3,
) -> pd.DataFrame:
    """Genera un cubo pequeño válido (todas las cosechas, MOB contiguos)."""
    reglas = cargar_reglas()
    filas: list[dict[str, Any]] = []

    for vintage in _vintages():
        # Pares (strategy, accounts) para cumplir R-08 y R-09 sin duplicar llave
        pares: list[tuple[str, int]] = [(_strategy_para(vintage, reglas), 100)]
        for banda in reglas["mezcla_strategy"]:
            if banda["vintage_min"] <= vintage <= banda["vintage_max"]:
                p = float(banda["p_esperada"])
                pares = [
                    ("r.Reca champ", int(round(1000 * (1 - p)))),
                    (banda["challenger"], int(round(1000 * p))),
                ]
                break

        max_m = mob_max(vintage, fecha_corte)
        n_mobs = min(mobs_por_cohorte, max_m + 1)

        for st, accounts in pares:
            seg_i, score_i = _segmento_score(st, reglas)
            for mob in range(n_mobs):
                gcl = 10.0 * (mob + 1)
                rec = 2.0
                acq = 5.0 if mob == 0 else 0.0
                var = 1.0
                fix = 1.0
                filas.append(
                    {
                        "VINTAGE": vintage,
                        "MOB": mob,
                        "STRATEGY": st,
                        "STRATEGY_SEGMENT": seg_i,
                        "PRODUCT": "Clasica",
                        "CHANNEL": "Internet",
                        "AGE_TAG": "02| Young 25-30",
                        "SCORE_TAG": score_i,
                        "ACCOUNTS": accounts,
                        "CREDITLINESAMTS": 50_000,
                        "ENRACCTS": min(80, accounts),
                        "OVER30ACCTS": 5,
                        "OVER30AMTS": 100.0,
                        "GCLAMTS": gcl,
                        "NCLAMTS": gcl - rec,
                        "RECOVERYAMTS": rec,
                        "ENRAMTS": 10_000.0,
                        "REVENUE": 200.0,
                        "EXPENSES_ACQ": acq,
                        "EXPENSES_ECM_VAR": var,
                        "EXPENSES_ECM_FIX": fix,
                        "EXPENSES_TOTAL": acq + var + fix,
                    }
                )

    return pd.DataFrame(filas)
