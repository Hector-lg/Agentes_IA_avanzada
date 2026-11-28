"""Entrenamiento → bundle → champion → scoring del corte → API, sobre el cubo de prueba."""

from __future__ import annotations

import json
import shutil

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from basecards.api.app import cargar_estado, crear_app
from basecards.ingesta.corte import leer_corte
from basecards.modelo.bundle import (
    MPDC,
    acotar,
    cargar_mpdc,
    sha256,
    verificar_bundle,
    version_champion,
)
from basecards.modelo.config import LLAVE_COHORTE, LLAVE_FILA, cargar_config
from basecards.modelo.entrenar import decidir_promocion, main
from basecards.modelo.features import agregar_rezagos_over30, construir_tabla
from basecards.modelo.puntuar import desviacion_normalizada, puntuar
from basecards.modelo.puntuar import main as main_puntuar
from basecards.soporte.predicciones.esquema import validar_esquema_predicciones
from basecards.soporte.reglas.k_reglas import evaluar_k01

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
    args.append("--sin-mlflow")
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
    assert main([*args, "--dir-modelos", str(tmp_path / "modelos"), "--sin-mlflow"]) == 2
    assert "ERROR" in capsys.readouterr().err
    assert not (tmp_path / "modelos").exists()  # si no hay datos, no se crea nada


@pytest.mark.parametrize("version", ["champion", 99])
def test_cargar_version_inexistente(tmp_path, version):
    with pytest.raises(FileNotFoundError):
        cargar_mpdc(version, tmp_path)


# --------------------------------------------------------------------------------------------
# Scoring del corte (issue #9)
# --------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def puntuado(modelo_entrenado, tmp_path_factory):
    _, dir_modelos, dir_cortes = modelo_entrenado
    salida = tmp_path_factory.mktemp("predicciones")
    evento = puntuar(202609, dir_cortes=dir_cortes, dir_modelos=dir_modelos, dir_salida=salida)
    return evento, salida / "202609"


def test_desviacion_normalizada():
    banda = np.array([[1.0, 2.0, 4.0]] * 7 + [[0.0, 0.0, 0.0]] * 2)
    obs = np.array([2.0, 3.0, 4.0, 6.0, 1.5, 0.0, np.nan, 0.0, 0.1])
    d = desviacion_normalizada(obs, banda)
    assert np.allclose(d[:6], [0.0, 0.5, 1.0, 2.0, 0.5, 2.0]) and np.isnan(d[6])
    assert d[7] == 0.0 and d[8] == 99.0  # banda de ancho 0


def test_predicciones_cumplen_contrato_e3(puntuado, cubo):
    evento, d = puntuado
    pred = pd.read_parquet(d / "predicciones.parquet")
    validar_esquema_predicciones(pred)
    assert set(pred["indicador"]) == {"tasa_ncl", "tasa_over30"}  # config/predicciones.yaml
    assert len(pred) == 2 * len(cubo) and not pred.duplicated([*LLAVE_FILA, "indicador"]).any()
    assert (pred["q05"] <= pred["q50"]).all() and (pred["q50"] <= pred["q95"]).all()
    obs = pred["observado"]
    assert (pred["fuera_de_intervalo"] == ((obs < pred["q05"]) | (obs > pred["q95"]))).all()
    assert (pred.loc[pred["fuera_de_intervalo"], "desviacion"] >= 1).all()
    assert (pred.loc[~pred["fuera_de_intervalo"], "desviacion"].dropna() <= 1).all()
    nuevas = pred["PERF_MONTH"] == 202609
    assert (pred["fuera_de_tiempo"] == nuevas).all() and nuevas.any()
    drivers = pred[["driver_1", "driver_2", "driver_3", "contribucion_1"]]
    assert drivers[nuevas].notna().all().all()
    k01 = pred["fuera_de_intervalo"] & (pred["enraccts"] >= 200)
    assert drivers[~nuevas & ~k01].isna().all().all()
    assert (pred["modelo_nombre"] == "mpdc").all()
    assert (pred["modelo_version"] == evento["version_modelo"]).all()
    r = evento["resumen"]
    assert r["filas_cohorte_mob"] == len(cubo) and r["filas_fuera_de_tiempo"] == nuevas.sum() / 2


def test_k01_lee_las_predicciones(puntuado):
    """El Agente de Soporte arma un hallazgo K-01 por cohorte × indicador fuera de intervalo."""
    _, d = puntuado
    pred = pd.read_parquet(d / "predicciones.parquet")
    hallazgos = evaluar_k01(pred, pred_cfg={"enraccts_min": 200, "umbral_monto_critico": 5e4})
    candidatas = pred[pred["fuera_de_intervalo"] & (pred["enraccts"] >= 200)]
    assert len(hallazgos) == candidatas.groupby([*LLAVE_COHORTE, "indicador"]).ngroups > 0
    for h in hallazgos:
        assert h.detalle["modelo"]["nombre"] == "mpdc" and all(h.detalle["drivers"])


def test_evento_y_salidas_para_dashboards(puntuado, cubo):
    evento, d = puntuado
    disco = json.loads((d / "evento_predicciones_listas.json").read_text(encoding="utf-8"))
    assert disco["evento"] == "predicciones_listas" and disco["corte_id"] == 202609
    assert disco["version_modelo"] == evento["version_modelo"] == 1
    assert len(disco["archivos"]) == 4
    for nombre, info in disco["archivos"].items():
        assert sha256(d / nombre) == info["sha256"]
        assert len(pd.read_parquet(d / nombre)) == info["filas"]

    n_coh = cubo.drop_duplicates(LLAVE_COHORTE).shape[0]
    coh = pd.read_parquet(d / "cohortes_horizonte.parquet")
    assert len(coh) == n_coh and (coh["MOB"] == 12).all()
    assert set(coh["nivel_riesgo"]) <= {"bajo", "medio", "alto", "critico"}
    rk = pd.read_parquet(d / "ranking_estrategias.parquet")
    assert list(rk["ranking"]) == list(range(1, len(rk) + 1))
    assert rk["margen_por_cuenta_q50"].is_monotonic_decreasing
    assert rk["cohortes"].sum() == n_coh
    assert not rk.duplicated(["STRATEGY", "STRATEGY_SEGMENT"]).any()
    primero = rk.iloc[0]
    g = coh[coh["STRATEGY"] == primero["STRATEGY"]]
    g = g[g["STRATEGY_SEGMENT"] == primero["STRATEGY_SEGMENT"]]
    esperado = (g["margen_por_cuenta_q50"] * g["ACCOUNTS_0"]).sum() / g["ACCOUNTS_0"].sum()
    assert np.isclose(primero["margen_por_cuenta_q50"], esperado)  # promedio por cuentas
    m4 = pd.read_parquet(d / "pronostico_over30_3mob.parquet")
    assert 0 < len(m4) <= n_coh and not m4.duplicated(LLAVE_COHORTE).any()
    assert (m4["MOB_OBJETIVO"] == m4["MOB_ULTIMO"] + 3).all()
    banda = m4[["tasa_over30_q05", "tasa_over30_q50", "tasa_over30_q95"]].to_numpy()
    assert (np.diff(banda, axis=1) >= 0).all() and (banda >= 0).all() and (banda <= 1).all()


def test_puntuar_rechaza_bundle_alterado(modelo_entrenado, tmp_path, capsys):
    res, _, dir_cortes = modelo_entrenado
    copia = tmp_path / "modelos"
    shutil.copytree(res.ruta, copia / res.ruta.name)
    (copia / "champion.json").write_text(json.dumps({"version": res.version}), encoding="utf-8")
    with (copia / res.ruta.name / "modelos" / "tasa_ncl_q50.txt").open("a", encoding="utf-8") as f:
        f.write("\n# alterado\n")
    args = ["--dir-cortes", str(dir_cortes), "--dir-modelos", str(copia)]
    assert main_puntuar([*args, "--dir-salida", str(tmp_path / "salida")]) == 2
    assert "integridad" in capsys.readouterr().err
    assert not (tmp_path / "salida").exists()  # con un modelo alterado no se escribe nada


def test_cli_puntuar_y_corte_no_apto(modelo_entrenado, tmp_path, capsys):
    _, dir_modelos, dir_cortes = modelo_entrenado
    cortes = tmp_path / "cortes"
    shutil.copytree(dir_cortes, cortes)
    manifiesto = cortes / "202609" / "manifest.json"
    m = json.loads(manifiesto.read_text(encoding="utf-8"))
    m["validacion"] = {"dictamen": "no apto"}
    manifiesto.write_text(json.dumps(m), encoding="utf-8")
    args = ["--corte", "202609", "--dir-cortes", str(cortes), "--dir-modelos", str(dir_modelos)]
    args += ["--dir-salida", str(tmp_path / "salida")]
    assert main_puntuar(args) == 2 and "no apto" in capsys.readouterr().err
    assert not (tmp_path / "salida").exists()
    assert main_puntuar([*args, "--forzar"]) == 0
    resumen = json.loads(capsys.readouterr().out)
    assert resumen["corte"] == 202609 and resumen["indicadores"] == ["tasa_ncl", "tasa_over30"]
    assert (tmp_path / "salida" / "202609" / "evento_predicciones_listas.json").exists()


# --------------------------------------------------------------------------------------------
# API (issue #10)
# --------------------------------------------------------------------------------------------
# Fuera del corte; "LE Thin " lleva espacio final a propósito (N-01)
NUEVA = [202610, "DARIA 2.0", "LE Thin ", "Clasica", "Internet", "02| Young 25-30", "c.160-164"]
COHORTE_NUEVA = dict(zip(LLAVE_COHORTE, NUEVA, strict=True))
CURVAS = {
    "curva": "tasa_ncl",
    "curva_over30": "tasa_over30",
    "margen_esperado": "margen_por_cuenta",
}


@pytest.fixture(scope="module")
def api(modelo_entrenado):
    _, dir_modelos, dir_cortes = modelo_entrenado
    estado = cargar_estado(202609, dir_modelos=dir_modelos, dir_cortes=dir_cortes)
    return TestClient(crear_app(estado)), estado


@pytest.fixture(scope="module")
def cohorte_larga(modelo_entrenado):
    """La cohorte con más MOB del corte y sus filas de `construir_tabla`."""
    _, _, dir_cortes = modelo_entrenado
    tabla = construir_tabla(leer_corte(202609, dir_cortes=dir_cortes))
    llave = tabla.groupby(LLAVE_COHORTE, observed=True).size().idxmax()
    coh = {k: (int(v) if k == "VINTAGE" else str(v)) for k, v in zip(LLAVE_COHORTE, llave)}
    mask = np.logical_and.reduce([tabla[k].astype(str) == str(v) for k, v in coh.items()])
    return coh, tabla.loc[mask].sort_values("MOB").reset_index(drop=True)


def test_api_salud_y_modelo(api, cubo):
    c, _ = api
    r = c.get("/salud")
    s = r.json()
    assert r.status_code == 200 and s["estado"] == "ok" and s["integridad_ok"] is True
    assert s["corte"] == 202609 and s["modelo"]["alias"] == "champion"
    assert s["cohortes"] == cubo.drop_duplicates(LLAVE_COHORTE).shape[0]
    m = c.get("/modelo").json()
    assert m["entrenado_hasta"] == 202608 and m["horizonte_ranking_mob"] == 12
    assert set(m["nivel_riesgo"]["umbrales"]) == {"medio", "alto", "critico"}
    assert m["ficha"].endswith("model_card.md") and "metricas_backtest" in m
    rutas = set(c.get("/openapi.json").json()["paths"])
    assert rutas == {"/salud", "/modelo", "/score-cohorte", "/ranking-estrategias", "/cohortes"}


def test_api_score_coincide_con_el_scoring(api, modelo_entrenado, cohorte_larga, puntuado):
    """Para una cohorte del corte, la API da la misma banda, desviación, drivers y M4 que E2."""
    c, _ = api
    res, _, _ = modelo_entrenado
    coh, filas = cohorte_larga
    mobs = [0, 1, 2, 3, 6, 9, 12]
    r = c.post("/score-cohorte", json={"cohorte": coh, "mobs": mobs})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["cohorte_en_corte"] and j["originacion"]["fuente"] == "corte"
    assert j["modelo"]["nombre"] == "mpdc" and j["modelo"]["version"] == res.version
    esperado = MPDC(res.ruta).predecir(filas)
    for nombre, obj in CURVAS.items():
        assert [p["MOB"] for p in j[nombre]] == mobs
        for p in j[nombre]:
            banda = [p["q05"], p["q50"], p["q95"]]
            assert np.allclose(banda, esperado[obj][p["MOB"]]), (obj, p["MOB"])
            assert np.isclose(p["observado"], filas[obj].iat[p["MOB"]])

    # Sin `observado`, la desviación es la del último MOB del corte, igual que en predicciones
    ultimo = int(filas["MOB"].max())
    assert j["fuente_observado"] == "corte" and j["desviacion"]["MOB"] == ultimo
    assert {d["indicador"] for d in j["desviaciones"]} == OBJETIVOS
    _, d = puntuado
    pred = pd.read_parquet(d / "predicciones.parquet")
    mask = np.logical_and.reduce([pred[k].astype(str) == str(v) for k, v in coh.items()])
    fila = pred.loc[mask & (pred["MOB"] == ultimo) & (pred["indicador"] == "tasa_ncl")].iloc[0]
    assert np.isclose(j["desviacion"]["desviacion"], fila["desviacion"])
    assert j["desviacion"]["fuera_de_intervalo"] == fila["fuera_de_intervalo"]
    assert [x["feature"] for x in j["drivers"]] == [fila[f"driver_{k}"] for k in (1, 2, 3)]
    assert set(j["drivers_por_indicador"]) == OBJETIVOS
    p4 = j["pronostico_over30_3mob"]  # el de pronostico_over30_3mob.parquet
    assert p4["MOB_ULTIMO"] == ultimo and p4["q05"] <= p4["q50"] <= p4["q95"]


def test_api_score_con_observado(api, cohorte_larga):
    c, _ = api
    coh, _ = cohorte_larga
    observado = {"MOB": 6, "NCLAMTS": 1e6, "ENRAMTS": 1e6, "OVER30ACCTS": 0, "ENRACCTS": 50}
    j = c.post("/score-cohorte", json={"cohorte": coh, "observado": observado}).json()
    assert j["fuente_observado"] == "solicitud" and len(j["curva"]) == 13  # MOB 0..12
    assert {d["indicador"] for d in j["desviaciones"]} == {"tasa_ncl", "tasa_over30"}
    ncl = j["desviacion"]
    assert ncl["MOB"] == 6 and ncl["observado"] == 1.0 and ncl["fuera_de_intervalo"]
    assert ncl["desviacion"] > 2.5 and j["nivel_riesgo"] == "critico"  # NCL muy arriba de q95
    sin_pares = c.post("/score-cohorte", json={"cohorte": coh, "observado": {"MOB": 3}}).json()
    assert sin_pares["desviaciones"] == [] and any("par" in a for a in sin_pares["advertencias"])
    sin_drivers = c.post("/score-cohorte", json={"cohorte": coh, "incluir_drivers": False})
    assert sin_drivers.json()["drivers"] == [] and sin_drivers.json()["drivers_por_indicador"] == {}


def test_api_cohorte_nueva(api):
    c, _ = api
    j = c.post("/score-cohorte", json={"cohorte": COHORTE_NUEVA}).json()
    assert not j["cohorte_en_corte"] and j["originacion"]["fuente"] == "imputada"
    assert j["cohorte"]["STRATEGY_SEGMENT"] == "LE Thin" and len(j["curva"]) == 13
    assert j["fuente_observado"] is None and j["desviaciones"] == []
    assert j["pronostico_over30_3mob"] is None and any("imputada" in a for a in j["advertencias"])
    assert all(p["observado"] is None for p in j["curva"])
    originacion = {"ACCOUNTS": 100, "CREDITLINESAMTS": 4e6}
    j = c.post("/score-cohorte", json={"cohorte": COHORTE_NUEVA, "originacion": originacion})
    o = j.json()["originacion"]
    assert o["fuente"] == "solicitud" and o["ACCOUNTS_0"] == 100 and o["ENRAMTS_0"] is None
    rara = {**COHORTE_NUEVA, "STRATEGY": "DARIA 9.9", "SCORE_TAG": "z.999"}
    adv = c.post("/score-cohorte", json={"cohorte": rara}).json()["advertencias"]
    assert any("DARIA 9.9" in a for a in adv) and any("z.999" in a for a in adv)


@pytest.mark.parametrize(
    "cuerpo",
    [
        {"cohorte": {**COHORTE_NUEVA, "VINTAGE": 202613}},
        {"cohorte": {**COHORTE_NUEVA, "VINTAGE": 2026}},
        {"cohorte": COHORTE_NUEVA, "mobs": [-1]},
        {"cohorte": COHORTE_NUEVA, "mobs": []},
        {"cohorte": COHORTE_NUEVA, "observado": {"MOB": -1}},
        {"cohorte": {k: v for k, v in COHORTE_NUEVA.items() if k != "PRODUCT"}},
    ],
)
def test_api_solicitud_invalida_es_422(api, cuerpo):
    c, _ = api
    assert c.post("/score-cohorte", json=cuerpo).status_code == 422


def test_api_ranking_igual_al_scoring(api, puntuado):
    c, _ = api
    _, d = puntuado
    rk = pd.read_parquet(d / "ranking_estrategias.parquet")
    j = c.get("/ranking-estrategias", params={"top": 500}).json()
    assert j["total"] == len(rk) == len(j["ranking"]) and j["horizonte_mob"] == 12
    api_rk = pd.DataFrame(j["ranking"])
    assert list(api_rk["STRATEGY_SEGMENT"]) == list(rk["STRATEGY_SEGMENT"])
    assert np.allclose(api_rk["margen_por_cuenta_q50"], rk["margen_por_cuenta_q50"])
    dos = c.get("/ranking-estrategias", params={"top": 2}).json()
    assert len(dos["ranking"]) == 2 and dos["total"] == len(rk)
    fam = c.get("/ranking-estrategias", params={"familia": "DARIA", "por_segmento": False})
    filas = fam.json()["ranking"]
    assert filas and {f["STRATEGY"] for f in filas} <= {"DARIA 1.6", "DARIA 2.0"}
    assert all(f["STRATEGY_SEGMENT"] is None for f in filas)
    dos_estrategias = [("strategy", "DARIA 1.6"), ("strategy", "r.Reca champ")]
    filas = c.get("/ranking-estrategias", params=dos_estrategias).json()["ranking"]
    assert {f["STRATEGY"] for f in filas} == {"DARIA 1.6", "r.Reca champ"}
    recientes = c.get("/ranking-estrategias", params={"vintage_desde": 202601, "top": 500})
    assert sum(f["cohortes"] for f in recientes.json()["ranking"]) < rk["cohortes"].sum()
    assert c.get("/ranking-estrategias", params={"vintage_desde": 202613}).status_code == 422
    assert c.get("/ranking-estrategias", params={"top": 0}).status_code == 422


def test_api_cohortes(api, cubo):
    c, _ = api
    j = c.get("/cohortes", params={"limite": 3}).json()
    assert j["total"] == cubo.drop_duplicates(LLAVE_COHORTE).shape[0] and len(j["cohortes"]) == 3
    cuentas = [x["ACCOUNTS_0"] for x in j["cohortes"]]
    assert cuentas == sorted(cuentas, reverse=True)
    daria = c.get("/cohortes", params={"strategy": "DARIA 2.0", "limite": 500}).json()
    n_daria = cubo.loc[cubo["STRATEGY"] == "DARIA 2.0"].drop_duplicates(LLAVE_COHORTE)
    assert daria["total"] == len(n_daria)
    assert {x["STRATEGY"] for x in daria["cohortes"]} == {"DARIA 2.0"}
    # Lo que devuelve /cohortes se puede puntuar tal cual
    primera = {k: daria["cohortes"][0][k] for k in LLAVE_COHORTE}
    assert c.post("/score-cohorte", json={"cohorte": primera}).json()["cohorte_en_corte"]
    assert c.get("/cohortes", params={"limite": 0}).status_code == 422


def test_api_sin_modelo(modelo_entrenado, tmp_path, monkeypatch):
    """Con el bundle alterado o sin bundle la API arranca, pero /salud y el scoring dan 503."""
    res, dir_modelos, dir_cortes = modelo_entrenado
    copia = tmp_path / "modelos"
    shutil.copytree(res.ruta, copia / res.ruta.name)
    (copia / "champion.json").write_text(json.dumps({"version": res.version}), encoding="utf-8")
    with (copia / res.ruta.name / "modelos" / "m4.txt").open("a", encoding="utf-8") as f:
        f.write("\n# alterado\n")
    c = TestClient(crear_app(cargar_estado(202609, dir_modelos=copia, dir_cortes=dir_cortes)))
    r = c.get("/salud")
    assert r.status_code == 503 and r.json()["integridad_ok"] is False
    assert r.json()["archivos_alterados"] == ["modelos/m4.txt"]
    assert c.post("/score-cohorte", json={"cohorte": COHORTE_NUEVA}).status_code == 503

    # Desde las variables de entorno, como la levanta uvicorn
    monkeypatch.setenv("MPDC_DIR_MODELOS", str(tmp_path / "vacio"))
    with TestClient(crear_app()) as c:
        r = c.get("/salud")
        assert r.status_code == 503 and "FileNotFoundError" in r.json()["error"]
        assert c.get("/ranking-estrategias").status_code == 503
    monkeypatch.setenv("MPDC_DIR_MODELOS", str(dir_modelos))
    monkeypatch.setenv("DIR_CORTES", str(dir_cortes))
    monkeypatch.setenv("FECHA_CORTE", "202609")
    with TestClient(crear_app()) as c:
        s = c.get("/salud").json()
        assert s["estado"] == "ok" and s["integridad_ok"] is True and s["corte"] == 202609
