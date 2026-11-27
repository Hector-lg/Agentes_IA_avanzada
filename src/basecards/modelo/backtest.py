"""Backtest del MPDC con origen móvil (PRD §6.4) contra los baselines interpretables (PRD §6.3).

En cada origen se entrena con PERF_MONTH ≤ origen y se evalúa en los `horizonte` meses de desempeño
siguientes, que el modelo no vio. Los orígenes retroceden `paso` meses a partir del último que
todavía deja una ventana de evaluación completa dentro de los datos de entrenamiento.

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
from basecards.modelo.config import cargar_config
from basecards.modelo.entrenar import (
    EntrenamientoError,
    ajustar_cuantiles,
    cargar_corte,
    etiqueta_cuantil,
    predecir_cuantiles,
    preparar_datos,
)
from basecards.modelo.features import niveles_categoricos

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
) -> dict[str, Any]:
    """Métricas del MPDC y de los dos baselines para un indicador en la ventana de evaluación."""
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
    return r


def backtest(
    tabla: pd.DataFrame, hasta: int, cfg: dict[str, Any], n_estimators: int | None = None
) -> dict[str, Any]:
    """Entrena y evalúa el MPDC en cada origen; devuelve el detalle por origen y el resumen."""
    mcfg = cfg["modelo"]
    h = mcfg["backtest"]["horizonte"]
    por_origen: list[dict[str, Any]] = []
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
        por_origen.append(
            {
                "origen": int(origen),
                "evaluado_hasta": int(fin),
                "filas_entrenamiento": len(ent),
                "objetivos": {
                    obj: evaluar_objetivo(ent, ev, pred[obj], obj, meta["peso"], mcfg["cuantiles"])
                    for obj, meta in mcfg["objetivos"].items()
                },
                "duracion_s": round(time.perf_counter() - t0, 1),
            }
        )
    if not por_origen:
        raise EntrenamientoError("No hay suficientes meses de desempeño para el backtest")
    return {"resumen": resumir(por_origen, list(mcfg["objetivos"])), "por_origen": por_origen}


def resumir(por_origen: list[dict[str, Any]], objetivos: list[str]) -> dict[str, Any]:
    """Promedio de cada métrica entre orígenes y mejora de la pinball q50 contra los baselines."""
    resumen: dict[str, Any] = {"origenes": [o["origen"] for o in por_origen], "objetivos": {}}
    for obj in objetivos:
        filas = [o["objetivos"][obj] for o in por_origen]
        r = {
            k: float(np.nanmean([f[k] for f in filas if f.get(k) is not None]))
            for k in filas[0]
            if k != "n"
        }
        r["n"] = int(sum(f["n"] for f in filas))
        r["mejora_pinball_q50_vs_curva_pct"] = 100 * (1 - r["pinball_q50"] / r["pinball_q50_curva"])
        r["mejora_pinball_q50_vs_arbol_pct"] = 100 * (1 - r["pinball_q50"] / r["pinball_q50_arbol"])
        resumen["objetivos"][obj] = r
    return resumen


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
