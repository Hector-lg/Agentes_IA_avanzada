"""Bundle versionado del MPDC: los modelos en texto de LightGBM y `bundle.json` (Manual §5.3, §7.4).

Estructura en disco (`artefactos/modelos/mpdc/`):
    v{N}/bundle.json          metadatos, niveles categóricos, CQR, umbrales, métricas y hashes
    v{N}/modelos/*.txt        9 modelos cuantílicos + M4 (formato texto de LightGBM)
    v{N}/backtest.json        detalle del backtest por origen
    v{N}/model_card.md        ficha del modelo (SR 11-7)
    champion.json             puntero a la versión con alias `champion`

La clase `MPDC` carga una versión y predice la curva esperada calibrada; la usan el scoring del
corte, la API y el modelo pyfunc de MLflow.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd

from basecards.modelo.calibracion import aplicar_cqr, offsets_por_fila, tramo_de_mob
from basecards.modelo.config import cargar_config, resolver
from basecards.modelo.features import FEATURES, FEATURES_M4, matriz


def etiqueta_cuantil(q: float) -> str:
    """0.05 → "q05", 0.5 → "q50", 0.95 → "q95"."""
    return f"q{round(q * 100):02d}"


def ordenar_cuantiles(pred: np.ndarray) -> np.ndarray:
    """Si los cuantiles se cruzan (q05 > q50 o q50 > q95), se ordenan por fila (Manual §5.2)."""
    return np.sort(pred, axis=1)


def acotar(pred: np.ndarray, rango: list[float | None] | None) -> np.ndarray:
    """Recorta los cuantiles al rango válido del indicador (por ejemplo, una tasa no es negativa)."""
    if not rango:
        return pred
    lo, hi = rango
    return np.clip(pred, -np.inf if lo is None else lo, np.inf if hi is None else hi)


def dir_modelos(dir_base: str | Path | None = None) -> Path:
    return resolver(dir_base or cargar_config()["modelo"]["dir_modelos"])


def sha256(ruta: Path) -> str:
    return hashlib.sha256(ruta.read_bytes()).hexdigest()


class MPDC:
    """Modelo de Pronóstico de Desempeño por Cohorte cargado desde un bundle."""

    def __init__(self, ruta: str | Path):
        self.ruta = Path(ruta)
        self.meta: dict[str, Any] = json.loads(
            (self.ruta / "bundle.json").read_text(encoding="utf-8")
        )
        self.niveles: dict[str, list[str]] = self.meta["niveles"]
        self.cuantiles: list[float] = self.meta["cuantiles"]
        self.tramos: list[list[int]] = self.meta["cqr"]["tramos_mob"]
        self.modelos: dict[str, list[lgb.Booster]] = {
            obj: [
                lgb.Booster(
                    model_file=str(self.ruta / "modelos" / f"{obj}_{etiqueta_cuantil(q)}.txt")
                )
                for q in self.cuantiles
            ]
            for obj in self.meta["objetivos"]
        }
        ruta_m4 = self.ruta / "modelos" / "m4.txt"
        self.m4 = lgb.Booster(model_file=str(ruta_m4)) if ruta_m4.exists() else None

    # ---- identificación -------------------------------------------------------------------
    @property
    def version(self) -> int:
        return int(self.meta["version"])

    @property
    def objetivos(self) -> list[str]:
        return list(self.meta["objetivos"])

    def info(self) -> dict[str, Any]:
        return {
            "nombre": "mpdc",
            "version": self.version,
            "alias": "champion" if self.meta.get("es_champion") else None,
            "corte": self.meta["corte"],
            "entrenado_hasta": self.meta["entrenado_hasta"],
        }

    def verificar_integridad(self) -> list[str]:
        return verificar_bundle(self.ruta)

    # ---- predicción -----------------------------------------------------------------------
    def predecir(self, tabla: pd.DataFrame, cqr: bool = True) -> dict[str, np.ndarray]:
        """Por indicador, un arreglo (n, 3) con q05, q50 y q95, calibrados con CQR si `cqr`."""
        X = matriz(tabla, self.niveles, FEATURES)
        tramo = tramo_de_mob(tabla["MOB"].to_numpy(), self.tramos)
        familia = tabla["FAMILIA"].astype(str).to_numpy()
        out = {}
        for obj, modelos in self.modelos.items():
            pred = ordenar_cuantiles(np.column_stack([m.predict(X) for m in modelos]))
            if cqr and self.meta["cqr"].get("activo", True):
                q = offsets_por_fila(tramo, familia, self.meta["cqr"]["offsets"][obj])
                pred = aplicar_cqr(pred, q)
            out[obj] = acotar(pred, self.meta["objetivos"][obj].get("rango"))
        return out

    def drivers(
        self, tabla: pd.DataFrame, objetivo: str, top: int = 3
    ) -> list[list[dict[str, Any]]]:
        """Los `top` drivers de cada fila con TreeSHAP (pred_contrib de LightGBM) del modelo q50."""
        X = matriz(tabla, self.niveles, FEATURES)
        i50 = self.cuantiles.index(0.5)
        contrib = self.modelos[objetivo][i50].predict(X, pred_contrib=True)[:, :-1]
        nombres = list(X.columns)
        orden = np.argsort(-np.abs(contrib), axis=1)[:, :top]
        out = []
        for fila, idx in enumerate(orden):
            items = []
            for j in idx:
                f = nombres[j]
                etiqueta = f"{f}={X.iat[fila, j]}" if f in self.niveles else f
                items.append(
                    {"feature": etiqueta, "contribucion": round(float(contrib[fila, j]), 6)}
                )
            out.append(items)
        return out

    def pronosticar_over30(self, tabla_m4: pd.DataFrame) -> np.ndarray | None:
        """M4: tasa Over30 en t+3 con el intervalo empírico del backtest. Arreglo (n, 3) en [0, 1]."""
        if self.m4 is None:
            return None
        p = self.m4.predict(matriz(tabla_m4, self.niveles, FEATURES_M4))
        m4 = self.meta["m4"]
        tr = tramo_de_mob(tabla_m4["MOB"].to_numpy(), self.tramos)
        pred = np.column_stack(
            [p + np.asarray(m4["residuo_q05"])[tr], p, p + np.asarray(m4["residuo_q95"])[tr]]
        )
        return np.clip(ordenar_cuantiles(pred), 0.0, 1.0)

    def nivel_riesgo(self, ncl_q50_horizonte: float) -> str:
        """bajo / medio / alto / crítico según la tasa NCL esperada al MOB horizonte."""
        u = self.meta["nivel_riesgo"]["umbrales"]
        for nivel in ("critico", "alto", "medio"):
            if ncl_q50_horizonte >= u[nivel]:
                return nivel
        return "bajo"


def verificar_bundle(ruta: str | Path) -> list[str]:
    """Archivos del bundle que faltan o cuyo SHA-256 no coincide con el registrado al entrenar.

    No carga los modelos, así que sirve también para revisar un bundle dañado (Manual §7.4).
    """
    ruta = Path(ruta)
    meta = json.loads((ruta / "bundle.json").read_text(encoding="utf-8"))
    return [
        nombre
        for nombre, h in meta.get("hashes", {}).items()
        if not (ruta / nombre).exists() or sha256(ruta / nombre) != h
    ]


def version_champion(dir_base: str | Path | None = None) -> int | None:
    p = dir_modelos(dir_base) / "champion.json"
    if not p.exists():
        return None
    return int(json.loads(p.read_text(encoding="utf-8"))["version"])


def ruta_version(version: int | str, dir_base: str | Path | None = None) -> Path:
    if version == "champion":
        v = version_champion(dir_base)
        if v is None:
            raise FileNotFoundError(
                "No hay modelo champion. Corre primero: "
                "python -m basecards.modelo.entrenar --corte 202609"
            )
        version = v
    return dir_modelos(dir_base) / f"v{int(version)}"


def cargar_mpdc(version: int | str = "champion", dir_base: str | Path | None = None) -> MPDC:
    return MPDC(ruta_version(version, dir_base))
