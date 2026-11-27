"""Backtest del MPDC con origen móvil (PRD §6.4) contra los baselines interpretables (PRD §6.3).

En cada origen se entrena con PERF_MONTH ≤ origen y se evalúa en los `horizonte` meses de desempeño
siguientes, que el modelo no vio. Los orígenes retroceden `paso` meses a partir del último que
todavía deja una ventana de evaluación completa dentro de los datos de entrenamiento.

Los residuos fuera de tiempo sirven para dos cosas: calibrar la banda 5–95 con CQR
(`basecards.modelo.calibracion`) y medir el modelo auxiliar M4 (Over30 a 3 MOB).

CLI: python -m basecards.modelo.backtest --corte 202609 [--rapido] [--n-estimators N] [--forzar]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from basecards.ingesta.corte import sumar_meses
from basecards.modelo import metricas as M
from basecards.modelo.baselines import BaselineArbol, BaselineCurva
from basecards.modelo.calibracion import (
    cobertura_validacion_cruzada,
    no_conformidad,
    offsets_cqr,
    tramo_de_mob,
)
from basecards.modelo.config import cargar_config
from basecards.modelo.entrenar import (
    EntrenamientoError,
    ajustar_cuantiles,
    ajustar_m4,
    cargar_corte,
    etiqueta_cuantil,
    mascara_m4,
    predecir_cuantiles,
    preparar_datos,
)
from basecards.modelo.features import (
    FEATURES_M4,
    agregar_rezagos_over30,
    matriz,
    niveles_categoricos,
)

log = logging.getLogger("basecards.modelo.backtest")


def origenes_backtest(hasta: int, cfg: dict[str, Any]) -> list[int]:
    """Orígenes del más reciente al más antiguo; el primero deja `horizonte` meses hasta `hasta`."""
    b = cfg["modelo"]["backtest"]
    return [sumar_meses(hasta, -(b["horizonte"] + b["paso"] * k)) for k in range(b["n_origenes"])]


def evaluar_objetivo(
    ent: pd.DataFrame,
    ev: pd.DataFrame,
    pred: np.ndarray,
    obj: str,
    peso: str,
    cuantiles: list[float],
) -> tuple[dict[str, Any], pd.DataFrame]:
    """Métricas del MPDC y de los baselines para un indicador, y los puntajes para CQR."""
    mask = (ev[obj].notna() & (ev[peso] > 0)).to_numpy()
    sub = ev.loc[mask]
    y = sub[obj].to_numpy()
    w = sub[peso].to_numpy(dtype="float64")
    p = pred[mask]
    curva = BaselineCurva(cuantiles).fit(ent, obj, peso).predict(sub)
    arbol = BaselineArbol().fit(ent, obj, peso).predict(sub)
    i50 = cuantiles.index(0.5)
    r: dict[str, Any] = {"n": len(y)}
    for i, q in enumerate(cuantiles):
        r[f"pinball_{etiqueta_cuantil(q)}"] = M.pinball(y, p[:, i], q, w)
        r[f"pinball_{etiqueta_cuantil(q)}_curva"] = M.pinball(y, curva[:, i], q, w)
    r["pinball_q50_arbol"] = M.pinball(y, arbol, 0.5, w)
    r["mae"] = M.mae(y, p[:, i50], w)
    r["rmse"] = M.rmse(y, p[:, i50], w)
    r["mae_curva"] = M.mae(y, curva[:, i50], w)
    r["mae_arbol"] = M.mae(y, arbol, w)
    r["cobertura_cruda"] = M.cobertura(y, p[:, 0], p[:, -1])
    r["cobertura_cruda_ponderada"] = M.cobertura(y, p[:, 0], p[:, -1], w)
    if obj == "margen_por_cuenta":
        r["spearman_estrategia_segmento"] = M.spearman_ranking(
            sub.assign(_pred=p[:, i50]), "_pred", obj, peso, ["STRATEGY", "STRATEGY_SEGMENT"]
        )
    puntajes = pd.DataFrame(
        {
            "MOB": sub["MOB"].to_numpy(),
            "familia": sub["FAMILIA"].astype(str).to_numpy(),
            "e": no_conformidad(y, p[:, 0], p[:, -1]).astype("float32"),
            "w": w.astype("float32"),
        }
    )
    return r, puntajes


def evaluar_m4(
    tabla_m4: pd.DataFrame,
    origen: int,
    hasta: int,
    niveles: dict[str, list[str]],
    cfg: dict[str, Any],
) -> tuple[dict[str, Any], pd.DataFrame] | None:
    """M4 entrenado con lo observado hasta el origen; pronostica Over30 3 MOB después de él."""
    util = mascara_m4(tabla_m4)
    ent = tabla_m4.loc[util & (tabla_m4["PERF_MONTH_OBJETIVO"] <= origen)]
    ev = tabla_m4.loc[
        util
        & (tabla_m4["PERF_MONTH"] <= origen)
        & (tabla_m4["PERF_MONTH_OBJETIVO"] > origen)
        & (tabla_m4["PERF_MONTH_OBJETIVO"] <= hasta)
    ]
    if ent.empty or ev.empty:
        return None
    p = ajustar_m4(ent, niveles, cfg).predict(matriz(ev, niveles, FEATURES_M4))
    y = ev["O30_OBJETIVO"].to_numpy()
    w = ev["PESO_OBJETIVO"].to_numpy(dtype="float64")
    pos = y > 0
    mob_objetivo = ev["MOB"] + cfg["modelo"]["m4"]["adelanto"]
    metricas = {
        "n": len(y),
        "mae": M.mae(y, p, w),
        "wape_pct": float(100 * np.sum(w * np.abs(y - p)) / max(np.sum(w * y), 1e-12)),
        "mape_celda_pct": float(100 * np.mean(np.abs(y[pos] - p[pos]) / y[pos]))
        if pos.any()
        else None,
        "mape_agregado_pct": M.mape_agregado(
            ev.assign(_p=p, MOB_OBJ=mob_objetivo),
            "_p",
            "O30_OBJETIVO",
            "PESO_OBJETIVO",
            ["STRATEGY", "VINTAGE", "MOB_OBJ"],
        ),
    }
    return metricas, pd.DataFrame({"MOB": ev["MOB"].to_numpy(), "residuo": y - p})


def backtest(
    tabla: pd.DataFrame, hasta: int, cfg: dict[str, Any], n_estimators: int | None = None
) -> dict[str, Any]:
    """Entrena y evalúa el MPDC y M4 en cada origen; calibra la banda con CQR.

    Devuelve el detalle por origen, el resumen, los desplazamientos de CQR por indicador y los
    cuantiles de los residuos de M4 por tramo de MOB (su intervalo empírico).
    """
    mcfg = cfg["modelo"]
    h = mcfg["backtest"]["horizonte"]
    m4cfg = mcfg["m4"]
    tabla_m4 = agregar_rezagos_over30(tabla, m4cfg["rezagos"], m4cfg["adelanto"])
    por_origen: list[dict[str, Any]] = []
    puntajes: list[pd.DataFrame] = []
    residuos_m4: list[pd.DataFrame] = []
    for origen in origenes_backtest(hasta, cfg):
        t0 = time.perf_counter()
        ent = tabla.loc[tabla["PERF_MONTH"] <= origen]
        fin = sumar_meses(origen, h)
        ev = tabla.loc[(tabla["PERF_MONTH"] > origen) & (tabla["PERF_MONTH"] <= fin)]
        if ent.empty or ev.empty:
            continue
        log.info(
            "Origen %s: %d filas de entrenamiento, %d de evaluación", origen, len(ent), len(ev)
        )
        niveles = niveles_categoricos(ent)
        pred = predecir_cuantiles(ajustar_cuantiles(ent, niveles, cfg, n_estimators), ev, niveles)
        res_o: dict[str, Any] = {
            "origen": int(origen),
            "evaluado_hasta": int(fin),
            "filas_entrenamiento": len(ent),
            "objetivos": {},
        }
        for obj, meta in mcfg["objetivos"].items():
            r, e = evaluar_objetivo(ent, ev, pred[obj], obj, meta["peso"], mcfg["cuantiles"])
            res_o["objetivos"][obj] = r
            puntajes.append(e.assign(origen=origen, objetivo=obj))
        m4 = evaluar_m4(tabla_m4, origen, hasta, niveles, cfg)
        if m4 is not None:
            res_o["m4"] = m4[0]
            residuos_m4.append(m4[1])
        res_o["duracion_s"] = round(time.perf_counter() - t0, 1)
        por_origen.append(res_o)
    if not por_origen:
        raise EntrenamientoError("No hay suficientes meses de desempeño para el backtest")

    tramos = mcfg["cqr"]["tramos_mob"]
    qs = mcfg["cuantiles"]
    alpha = 1 - (qs[-1] - qs[0])
    e_todos = pd.concat(puntajes, ignore_index=True)
    e_todos["tramo"] = tramo_de_mob(e_todos["MOB"].to_numpy(), tramos)
    resumen = resumir(por_origen, list(mcfg["objetivos"]))
    offsets = {}
    for obj in mcfg["objetivos"]:
        e_obj = e_todos.loc[e_todos["objetivo"] == obj]
        offsets[obj] = offsets_cqr(e_obj, len(tramos), alpha)
        resumen["objetivos"][obj].update(cobertura_validacion_cruzada(e_obj, len(tramos), alpha))
    return {
        "resumen": resumen,
        "por_origen": por_origen,
        "cqr_offsets": offsets,
        "m4_residuos": cuantiles_residuos_m4(residuos_m4, tramos),
    }


def cuantiles_residuos_m4(
    residuos: list[pd.DataFrame], tramos: list[list[int]]
) -> dict[str, list[float]]:
    """Cuantiles 5 y 95 de los residuos de M4 por tramo de MOB: su intervalo empírico."""
    r = (
        pd.concat(residuos, ignore_index=True)
        if residuos
        else pd.DataFrame(columns=["MOB", "residuo"])
    )
    tramo = tramo_de_mob(r["MOB"].to_numpy(), tramos)
    out: dict[str, list[float]] = {"residuo_q05": [], "residuo_q95": []}
    for t in range(len(tramos)):
        v = r.loc[tramo == t, "residuo"].to_numpy(dtype="float64")
        out["residuo_q05"].append(float(np.quantile(v, 0.05)) if len(v) else 0.0)
        out["residuo_q95"].append(float(np.quantile(v, 0.95)) if len(v) else 0.0)
    return out


def resumir(por_origen: list[dict[str, Any]], objetivos: list[str]) -> dict[str, Any]:
    """Promedio de cada métrica entre orígenes y mejora de la pinball q50 contra los baselines."""
    resumen: dict[str, Any] = {"origenes": [o["origen"] for o in por_origen], "objetivos": {}}
    for obj in objetivos:
        filas = [o["objetivos"][obj] for o in por_origen]
        resumen["objetivos"][obj] = _promediar(filas)
        r = resumen["objetivos"][obj]
        r["mejora_pinball_q50_vs_curva_pct"] = 100 * (1 - r["pinball_q50"] / r["pinball_q50_curva"])
        r["mejora_pinball_q50_vs_arbol_pct"] = 100 * (1 - r["pinball_q50"] / r["pinball_q50_arbol"])
    m4 = [o["m4"] for o in por_origen if "m4" in o]
    if m4:
        resumen["m4"] = _promediar(m4)
    return resumen


def _promediar(filas: list[dict[str, Any]]) -> dict[str, Any]:
    """Promedio entre orígenes de cada métrica (ignora los None); `n` se suma."""
    r: dict[str, Any] = {
        k: float(np.nanmean([f[k] for f in filas if f.get(k) is not None]))
        for k in filas[0]
        if k != "n"
    }
    r["n"] = int(sum(f["n"] for f in filas))
    return r


def correr_backtest(
    corte: int,
    *,
    df: pd.DataFrame | None = None,
    dir_cortes: str | Path | None = None,
    rapido: bool = False,
    n_estimators: int | None = None,
    forzar: bool = False,
) -> dict[str, Any]:
    """Corte → datos de entrenamiento → backtest. Con `df` no se lee el Parquet ni el dictamen."""
    t0 = time.perf_counter()
    cfg = cargar_config()
    if df is None:
        df, _ = cargar_corte(corte, dir_cortes=dir_cortes, forzar=forzar)
    ent, info = preparar_datos(df, corte, cfg, rapido=rapido)
    log.info(
        "Entrenamiento: %d filas, %d cohortes, hasta %s",
        info["filas_entrenamiento"],
        info["cohortes_entrenamiento"],
        info["entrenado_hasta"],
    )
    bt = backtest(ent, info["entrenado_hasta"], cfg, n_estimators)
    return {
        "corte": int(corte),
        "datos": info,
        **bt,
        "duracion_s": round(time.perf_counter() - t0, 1),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="basecards.modelo.backtest", description="Backtest del MPDC con origen móvil"
    )
    p.add_argument("--corte", type=int, default=202609)
    p.add_argument(
        "--dir-cortes", default=None, help="Raíz de los cortes (por defecto data/cortes)"
    )
    p.add_argument("--rapido", action="store_true", help="Muestra estratificada de cohortes")
    p.add_argument("--n-estimators", type=int, default=None, help="Sobrescribe n_estimators")
    p.add_argument("--forzar", action="store_true", help="Usa el corte aunque sea 'no apto'")
    p.add_argument("--salida", default=None, help="Guarda el resultado completo en este JSON")
    args = p.parse_args(argv)
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        res = correr_backtest(
            args.corte,
            dir_cortes=args.dir_cortes,
            rapido=args.rapido,
            n_estimators=args.n_estimators,
            forzar=args.forzar,
        )
    except (EntrenamientoError, FileNotFoundError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    if args.salida:
        Path(args.salida).write_text(
            json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(
        json.dumps(
            {"datos": res["datos"], **res["resumen"], "duracion_s": res["duracion_s"]},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
