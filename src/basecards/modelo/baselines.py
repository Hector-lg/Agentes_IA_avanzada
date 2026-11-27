"""Baselines interpretables del MPDC (PRD §6.3).

El MPDC solo se justifica si supera, fuera de tiempo, a dos modelos simples:
- `BaselineCurva`: la curva promedio por MOB y familia (cuantiles ponderados empíricos);
- `BaselineArbol`: un árbol de regresión de profundidad 6 sobre MOB, FAMILIA y SCORE_ORD.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.tree import DecisionTreeRegressor

from basecards.modelo.metricas import cuantil_ponderado


class BaselineCurva:
    """Curva promedio por MOB y familia: cuantiles ponderados empíricos."""

    def __init__(self, cuantiles: list[float]):
        self.cuantiles = cuantiles

    def fit(self, tabla: pd.DataFrame, obj: str, peso: str) -> BaselineCurva:
        sub = tabla.loc[tabla[obj].notna() & (tabla[peso] > 0), ["MOB", "FAMILIA", obj, peso]]
        sub = sub.assign(FAMILIA=sub["FAMILIA"].astype(str))

        def q(g: pd.DataFrame) -> np.ndarray:
            return cuantil_ponderado(g[obj].to_numpy(), g[peso].to_numpy(), self.cuantiles)

        self.por_mob_familia = {k: q(g) for k, g in sub.groupby(["MOB", "FAMILIA"])}
        self.por_mob = {k: q(g) for k, g in sub.groupby("MOB")}
        self.global_ = q(sub)
        return self

    def predict(self, tabla: pd.DataFrame) -> np.ndarray:
        """Cuantiles de (MOB, familia); si no se vio esa combinación, los del MOB; si no, globales."""
        cols = [f"c{i}" for i in range(len(self.cuantiles))]
        mf = pd.DataFrame(
            [(m, f, *v) for (m, f), v in self.por_mob_familia.items()],
            columns=["MOB", "FAMILIA", *cols],
        )
        mo = pd.DataFrame([(m, *v) for m, v in self.por_mob.items()], columns=["MOB", *cols])
        llave = pd.DataFrame(
            {"MOB": tabla["MOB"].to_numpy(), "FAMILIA": tabla["FAMILIA"].astype(str).to_numpy()}
        )
        a = llave.merge(mf, on=["MOB", "FAMILIA"], how="left")[cols].to_numpy()
        b = llave[["MOB"]].merge(mo, on="MOB", how="left")[cols].to_numpy()
        out = np.where(np.isnan(a), b, a)
        return np.where(np.isnan(out), np.asarray(self.global_)[None, :], out)


class BaselineArbol:
    """Árbol de regresión de profundidad 6 sobre MOB, FAMILIA y SCORE_ORD."""

    def fit(self, tabla: pd.DataFrame, obj: str, peso: str) -> BaselineArbol:
        sub = tabla.loc[tabla[obj].notna() & (tabla[peso] > 0)]
        self.familias = sorted(sub["FAMILIA"].astype(str).unique())
        self.arbol = DecisionTreeRegressor(max_depth=6, random_state=42)
        self.arbol.fit(
            self._X(sub), sub[obj].to_numpy(), sample_weight=sub[peso].to_numpy(dtype="float64")
        )
        return self

    def _X(self, tabla: pd.DataFrame) -> np.ndarray:
        codigo = {f: i for i, f in enumerate(self.familias)}
        fam = tabla["FAMILIA"].astype(str).map(codigo).fillna(-1)
        return np.column_stack(
            [tabla["MOB"].to_numpy(), fam.to_numpy(), tabla["SCORE_ORD"].fillna(-1).to_numpy()]
        )

    def predict(self, tabla: pd.DataFrame) -> np.ndarray:
        return self.arbol.predict(self._X(tabla))
