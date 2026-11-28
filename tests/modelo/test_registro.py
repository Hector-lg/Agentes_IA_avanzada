"""Registro en MLflow con tracking temporal: run, modelo `mpdc` y alias `champion`."""

from __future__ import annotations

import mlflow
import numpy as np
import pytest
from mlflow.tracking import MlflowClient

from basecards.modelo.bundle import MPDC
from basecards.modelo.config import LLAVE_COHORTE
from basecards.modelo.features import construir_tabla, filas_a_mob
from basecards.modelo.registro import _metricas_planas, registrar


@pytest.fixture
def tracking(tmp_path, monkeypatch):
    """MLflow en un sqlite temporal: nunca toca mlflow.db ni mlartifacts del repo."""
    uri = f"sqlite:///{(tmp_path / 'mlflow.db').as_posix()}"
    monkeypatch.setenv("MLFLOW_TRACKING_URI", uri)
    yield uri, tmp_path / "mlartifacts"
    mlflow.set_tracking_uri(None)


def test_registra_y_carga_champion(modelo_entrenado, cubo, tracking):
    res, _, _ = modelo_entrenado
    mpdc = MPDC(res.ruta)
    uri, artefactos = tracking
    run_id = registrar(res.ruta, mpdc.meta, promovido=True, dir_artefactos=artefactos)

    run = MlflowClient(tracking_uri=uri).get_run(run_id)
    assert run.data.tags["corte"] == "202609" and run.data.tags["champion"] == "True"
    assert run.data.metrics["bt_tasa_ncl_cobertura_cqr"] == pytest.approx(
        mpdc.meta["metricas"]["objetivos"]["tasa_ncl"]["cobertura_cqr"]
    )
    assert artefactos.exists()

    # Entrada mínima: llave de cohorte, MOB y originación (como la mandaría la API)
    cohortes = filas_a_mob(construir_tabla(cubo), 6).head(5)
    entrada = cohortes[[*LLAVE_COHORTE, "MOB", "ACCOUNTS_0", "CREDITLINESAMTS_0", "ENRAMTS_0"]]
    modelo = mlflow.pyfunc.load_model("models:/mpdc@champion")
    salida = modelo.predict(entrada)
    esperado = mpdc.predecir(cohortes)
    for obj, p in esperado.items():
        cols = [f"{obj}_q05", f"{obj}_q50", f"{obj}_q95"]
        assert np.allclose(salida[cols].to_numpy(), p), obj
    assert list(salida[LLAVE_COHORTE].itertuples(index=False)) == list(
        entrada[LLAVE_COHORTE].itertuples(index=False)
    )


def test_sin_promocion_no_mueve_el_alias(modelo_entrenado, tracking):
    res, _, _ = modelo_entrenado
    meta = MPDC(res.ruta).meta
    uri, artefactos = tracking
    registrar(res.ruta, meta, promovido=True, dir_artefactos=artefactos)
    registrar(res.ruta, meta, promovido=False, dir_artefactos=artefactos)
    cliente = MlflowClient(tracking_uri=uri)
    assert int(cliente.get_model_version_by_alias("mpdc", "champion").version) == 1
    assert len(cliente.search_model_versions("name='mpdc'")) == 2


def test_metricas_planas_omite_no_numericas():
    meta = {
        "metricas": {
            "objetivos": {"tasa_ncl": {"pinball_q50": 0.1, "por_familia": {}, "x": float("nan")}},
            "m4": {"mae": 0.02, "mape_celda_pct": None},
        }
    }
    assert _metricas_planas(meta) == {"bt_tasa_ncl_pinball_q50": 0.1, "bt_m4_mae": 0.02}
