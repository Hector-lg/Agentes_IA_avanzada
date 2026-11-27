from __future__ import annotations

import pytest

from basecards.soporte.predicciones import cargar_predicciones, validar_esquema_predicciones
from basecards.soporte.predicciones.esquema import COLUMNAS_REQUERIDAS
from basecards.soporte.predicciones.mock import (
    generar_predicciones_mock,
    guardar_predicciones_mock,
)
from basecards.soporte.sintetico import generar_cubo_sintetico


def test_mock_cumple_contrato_columnas():
    pred = generar_predicciones_mock(forzar_fuera=True)
    validar_esquema_predicciones(pred)
    assert set(COLUMNAS_REQUERIDAS).issubset(pred.columns)


def test_mock_tiene_dentro_y_fuera():
    pred = generar_predicciones_mock(forzar_fuera=True)
    assert pred["fuera_de_intervalo"].any()
    assert (~pred["fuera_de_intervalo"]).any()


def test_cargar_predicciones_mock_persiste():
    ruta = guardar_predicciones_mock(corte=202609)
    assert ruta.exists()
    df = cargar_predicciones(202609, fuente="mock")
    assert len(df) > 0
    validar_esquema_predicciones(df)


def test_esquema_falla_si_falta_columna():
    pred = generar_predicciones_mock(forzar_fuera=False).drop(columns=["q50"])
    with pytest.raises(ValueError, match="faltan columnas"):
        validar_esquema_predicciones(pred)


def test_mock_alineado_a_cubo():
    cubo = generar_cubo_sintetico(mobs_por_cohorte=2)
    pred = generar_predicciones_mock(cubo, forzar_fuera=False)
    assert set(pred["VINTAGE"]).issubset(set(cubo["VINTAGE"]))
    assert pred["indicador"].isin(["tasa_ncl", "tasa_over30"]).all()
    assert not pred["fuera_de_intervalo"].any()
