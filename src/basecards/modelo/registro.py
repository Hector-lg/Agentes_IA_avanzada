"""Registro del MPDC en MLflow local (PRD §6.4, RN-06).

Cada entrenamiento guarda un run con parámetros, métricas del backtest, hash del corte, la ficha
del modelo y el modelo pyfunc `mpdc` en el Model Registry. Si el modelo se promovió, esa versión
recibe el alias `champion`, así que `mlflow.pyfunc.load_model("models:/mpdc@champion")` siempre
carga el modelo vigente.

Tracking: `MLFLOW_TRACKING_URI` o, por defecto, `sqlite:///<repo>/mlflow.db`, con los artefactos en
`<repo>/mlartifacts`. Para ver la UI: mlflow ui --backend-store-uri sqlite:///mlflow.db
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

import mlflow
import pandas as pd
from mlflow.tracking import MlflowClient

from basecards.modelo.config import ROOT, cargar_config

DEPENDENCIAS_PYFUNC = ["lightgbm==4.7.0", "pandas==3.1.1", "numpy==2.5.4", "pyyaml==6.0.3"]


class MPDCPyfunc(mlflow.pyfunc.PythonModel):
    """Entrada: la llave de cohorte, MOB y la originación (ACCOUNTS_0, CREDITLINESAMTS_0,
    ENRAMTS_0). Salida: la misma tabla con {indicador}_q05, _q50 y _q95 calibrados."""

    def load_context(self, context: Any) -> None:
        from basecards.modelo.bundle import MPDC

        self.mpdc = MPDC(context.artifacts["bundle"])

    def predict(
        self, context: Any, model_input: pd.DataFrame, params: dict | None = None
    ) -> pd.DataFrame:
        from basecards.modelo.features import tabla_desde_cohortes

        pred = self.mpdc.predecir(tabla_desde_cohortes(model_input))
        out = model_input.copy()
        for obj, p in pred.items():
            out[f"{obj}_q05"], out[f"{obj}_q50"], out[f"{obj}_q95"] = p[:, 0], p[:, 1], p[:, 2]
        return out


def configurar_tracking(tracking_uri: str | None = None) -> str:
    uri = (
        tracking_uri
        or os.environ.get("MLFLOW_TRACKING_URI")
        or f"sqlite:///{(ROOT / 'mlflow.db').as_posix()}"
    )
    mlflow.set_tracking_uri(uri)
    return uri


def _metricas_planas(meta: dict[str, Any]) -> dict[str, float]:
    """Métricas numéricas del backtest como bt_{indicador}_{métrica} (MLflow no guarda dicts)."""
    grupos = {**meta["metricas"]["objetivos"], "m4": meta["metricas"].get("m4") or {}}
    return {
        f"bt_{grupo}_{k}": float(v)
        for grupo, r in grupos.items()
        for k, v in r.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    }


def registrar(
    ruta_bundle: Path,
    meta: dict[str, Any],
    promovido: bool,
    *,
    tracking_uri: str | None = None,
    dir_artefactos: str | Path | None = None,
) -> str:
    """Registra el bundle como run y como versión del modelo `mpdc`; devuelve el run_id."""
    cfg = cargar_config()["modelo"]
    configurar_tracking(tracking_uri)
    nombre_exp = cfg["experimento_mlflow"]
    if mlflow.get_experiment_by_name(nombre_exp) is None:
        artefactos = Path(dir_artefactos) if dir_artefactos else ROOT / "mlartifacts"
        mlflow.create_experiment(nombre_exp, artifact_location=artefactos.resolve().as_uri())
    mlflow.set_experiment(nombre_exp)

    with mlflow.start_run(run_name=f"mpdc-v{meta['version']}-corte{meta['corte']}") as run:
        mlflow.set_tags(
            {
                "corte": meta["corte"],
                "entrenado_hasta": meta["entrenado_hasta"],
                "sha256_csv": meta["datos"].get("sha256_csv") or "",
                "sha256_parquet": meta["datos"].get("sha256_parquet") or "",
                "dictamen_e1": meta["datos"].get("dictamen_e1") or "",
                "rapido": meta["rapido"],
                "version_bundle": meta["version"],
                "champion": promovido,
            }
        )
        mlflow.log_params({f"lgbm_{k}": v for k, v in meta["lgbm"].items()})
        mlflow.log_params(
            {
                "cuantiles": meta["cuantiles"],
                "features": ",".join(meta["features"]),
                "acumulado": meta["acumulado"],
                "filas_entrenamiento": meta["datos"]["filas_entrenamiento"],
                "origenes_backtest": meta["metricas"]["origenes"],
            }
        )
        mlflow.log_metrics(_metricas_planas(meta))
        mlflow.log_artifact(str(ruta_bundle / "model_card.md"))
        mlflow.log_artifact(str(ruta_bundle / "backtest.json"))
        info = mlflow.pyfunc.log_model(
            name="mpdc",
            python_model=MPDCPyfunc(),
            artifacts={"bundle": str(ruta_bundle)},
            code_paths=[str(ROOT / "src" / "basecards")],
            pip_requirements=DEPENDENCIAS_PYFUNC,
            registered_model_name=cfg["nombre_registro"],
        )
        if promovido and info.registered_model_version is not None:
            MlflowClient().set_registered_model_alias(
                cfg["nombre_registro"], "champion", str(info.registered_model_version)
            )
        return run.info.run_id
