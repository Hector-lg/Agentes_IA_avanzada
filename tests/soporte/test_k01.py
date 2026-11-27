from __future__ import annotations

from basecards.soporte.pipeline_e3 import comparar_curva_esperada, validar_corte_e1_e3
from basecards.soporte.predicciones.mock import generar_predicciones_mock
from basecards.soporte.reglas.k_reglas import evaluar_k01
from basecards.soporte.sintetico import generar_cubo_sintetico


def test_k01_detecta_fuera_de_intervalo():
    pred = generar_predicciones_mock(forzar_fuera=True)
    hallazgos = evaluar_k01(pred)
    assert any(h.regla_id == "K-01" for h in hallazgos)
    assert all("cohorte" in h.detalle for h in hallazgos)


def test_k01_no_dispara_sin_exposicion():
    pred = generar_predicciones_mock(forzar_fuera=True)
    pred = pred.copy()
    pred["enraccts"] = 50  # bajo el mínimo 200
    # Mantener fuera_de_intervalo true pero sin exposición
    pred["fuera_de_intervalo"] = True
    hallazgos = evaluar_k01(pred)
    assert hallazgos == []


def test_k01_no_dispara_dentro_de_intervalo():
    pred = generar_predicciones_mock(forzar_fuera=False)
    # Forzar todo dentro
    pred = pred.copy()
    pred["fuera_de_intervalo"] = False
    pred["observado"] = pred["q50"]
    pred["desviacion"] = 0.0
    assert evaluar_k01(pred) == []


def test_comparar_curva_esperada_api():
    pred = generar_predicciones_mock(forzar_fuera=True)
    hallazgos = comparar_curva_esperada(pred)
    assert len(hallazgos) >= 1


def test_severidad_critica_por_desviacion():
    pred = generar_predicciones_mock(forzar_fuera=True)
    # Amplificar desviación
    pred = pred.loc[pred["fuera_de_intervalo"]].copy()
    pred["desviacion"] = 3.0
    pred["enraccts"] = 250
    hallazgos = evaluar_k01(pred)
    assert hallazgos
    assert any(h.severidad == "critica" for h in hallazgos)


def test_pipeline_e1_e3_consolida():
    cubo = generar_cubo_sintetico(mobs_por_cohorte=2)
    pred = generar_predicciones_mock(cubo, forzar_fuera=True)
    res = validar_corte_e1_e3(cubo, predicciones=pred, derivar_corte_anterior=False)
    assert res.hallazgos_k01
    assert all(h.detalle.get("hallazgo_id") for h in res.hallazgos)
    assert "K-01" in res.detalle["reglas"]
