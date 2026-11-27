"""Entrenamiento → bundle versionado → champion, sobre el cubo de prueba (pocos árboles)."""

from __future__ import annotations

import json
import shutil

import numpy as np
import pytest

from basecards.modelo.bundle import (
    MPDC,
    acotar,
    cargar_mpdc,
    verificar_bundle,
    version_champion,
)
from basecards.modelo.config import cargar_config
from basecards.modelo.entrenar import decidir_promocion, main
from basecards.modelo.features import agregar_rezagos_over30, construir_tabla

OBJETIVOS = {"tasa_ncl", "tasa_over30", "margen_por_cuenta"}


def test_entrenamiento_deja_bundle_champion(modelo_entrenado):
    res, dir_modelos, _ = modelo_entrenado
    assert res.promovido and version_champion(dir_modelos) == res.version
    m = cargar_mpdc("champion", dir_modelos)
    assert m.ruta == res.ruta and m.verificar_integridad() == []
    assert set(m.objetivos) == OBJETIVOS and m.info()["alias"] == "champion"
    assert len(list((res.ruta / "modelos").glob("*.txt"))) == 10  # 9 cuantílicos + M4
    assert m.meta["entrenado_hasta"] == 202608
    assert m.meta["datos"]["filas_excluidas"]["perf_month_del_corte_actual"] > 0
    for obj, r in res.metricas["objetivos"].items():
        assert 0.85 <= r["cobertura_cqr"] <= 0.95, obj  # condición para ser champion
    ficha = (res.ruta / "model_card.md").read_text(encoding="utf-8")
    assert ficha.startswith(f"# Ficha del modelo — MPDC v{res.version}")
    assert "## Limitaciones" in ficha and "Promovido a `champion`" in ficha


def test_prediccion_calibrada_y_acotada(modelo_entrenado, cubo):
    res, _, _ = modelo_entrenado
    m = MPDC(res.ruta)
    tabla = construir_tabla(cubo)
    sin_cqr, con_cqr = m.predecir(tabla, cqr=False), m.predecir(tabla)
    for obj in OBJETIVOS:
        p = con_cqr[obj]
        assert p.shape == (len(tabla), 3) and (np.diff(p, axis=1) >= 0).all(), obj
        assert np.allclose(p[:, 1], sin_cqr[obj][:, 1])  # CQR no mueve la mediana
    assert (con_cqr["tasa_ncl"] >= 0).all()
    assert (con_cqr["tasa_over30"] >= 0).all() and (con_cqr["tasa_over30"] <= 1).all()
    drivers = m.drivers(tabla.head(4), "tasa_ncl")
    assert len(drivers) == 4 and all(len(d) == 3 for d in drivers)
    m4 = m.pronosticar_over30(agregar_rezagos_over30(tabla).dropna(subset=["O30_T2"]).head(20))
    assert m4.shape == (20, 3) and (m4 >= 0).all() and (m4 <= 1).all()
    u = m.meta["nivel_riesgo"]["umbrales"]
    assert u["medio"] <= u["alto"] <= u["critico"]
    assert m.nivel_riesgo(u["critico"]) == "critico" and m.nivel_riesgo(-1.0) == "bajo"


def test_integridad_detecta_archivo_alterado(modelo_entrenado, tmp_path):
    res, _, _ = modelo_entrenado
    copia = tmp_path / res.ruta.name
    shutil.copytree(res.ruta, copia)
    with (copia / "modelos" / "m4.txt").open("a", encoding="utf-8") as f:
        f.write("\n# alterado\n")
    (copia / "modelos" / "tasa_ncl_q05.txt").unlink()
    assert verificar_bundle(res.ruta) == []
    assert sorted(verificar_bundle(copia)) == ["modelos/m4.txt", "modelos/tasa_ncl_q05.txt"]


def test_decidir_promocion(tmp_path):
    cfg = cargar_config()

    def resumen(cobertura: float, pinball: float) -> dict:
        r = {"cobertura_cqr": cobertura, "pinball_q50": pinball}
        return {"objetivos": {o: dict(r) for o in OBJETIVOS}}

    assert decidir_promocion(resumen(0.90, 1.0), cfg, tmp_path)[0]  # primer modelo
    ok, motivo = decidir_promocion(resumen(0.80, 1.0), cfg, tmp_path)
    assert not ok and "cobertura" in motivo
    (tmp_path / "v1").mkdir()
    (tmp_path / "v1" / "bundle.json").write_text(
        json.dumps({"metricas": resumen(0.9, 1.0)}), encoding="utf-8"
    )
    (tmp_path / "champion.json").write_text(json.dumps({"version": 1}), encoding="utf-8")
    assert decidir_promocion(resumen(0.90, 0.9), cfg, tmp_path)[0]  # mejora la pinball
    ok, motivo = decidir_promocion(resumen(0.90, 1.1), cfg, tmp_path)
    assert not ok and "no mejora" in motivo


def test_acotar():
    assert np.allclose(acotar(np.array([[-0.1, 0.5, 1.2]]), [0, 1]), [[0, 0.5, 1]])
    assert np.allclose(acotar(np.array([[-0.1, 0.5, 9.0]]), [0, None]), [[0, 0.5, 9.0]])
    assert np.allclose(acotar(np.array([[-5.0, 1.0, 9.0]]), None), [[-5.0, 1.0, 9.0]])


def test_cli_y_segunda_version(modelo_entrenado, tmp_path, capsys):
    """La CLI deja v1 champion; un modelo peor (2 árboles) crea v2 sin quitarle el alias a v1."""
    _, _, dir_cortes = modelo_entrenado
    args = ["--corte", "202609", "--dir-cortes", str(dir_cortes), "--dir-modelos", str(tmp_path)]
    assert main([*args, "--n-estimators", "25"]) == 0
    primera = json.loads(capsys.readouterr().out)
    assert primera["version"] == 1 and primera["champion"] is True
    assert main([*args, "--n-estimators", "2"]) == 0
    segunda = json.loads(capsys.readouterr().out)
    assert segunda["version"] == 2 and segunda["champion"] is False
    assert version_champion(tmp_path) == 1
    v2 = json.loads((tmp_path / "v2" / "bundle.json").read_text(encoding="utf-8"))
    assert v2["promocion"]["promovido"] is False and v2["es_champion"] is False


def test_cli_rechaza_corte_inexistente(tmp_path, capsys):
    args = ["--corte", "202609", "--dir-cortes", str(tmp_path / "no_existe")]
    assert main([*args, "--dir-modelos", str(tmp_path / "modelos")]) == 2
    assert "ERROR" in capsys.readouterr().err
    assert not (tmp_path / "modelos").exists()  # si no hay datos, no se crea nada


@pytest.mark.parametrize("version", ["champion", 99])
def test_cargar_version_inexistente(tmp_path, version):
    with pytest.raises(FileNotFoundError):
        cargar_mpdc(version, tmp_path)
