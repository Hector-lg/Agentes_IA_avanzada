"""E2 — Entrenamiento del MPDC (PRD §6.3–6.4, Manual §5).

Flujo de `entrenar`:
1. `cargar_corte`: lee el Parquet del corte (E1) y se niega a usarlo si la validación lo dictaminó
   `no apto`, salvo con `forzar=True`.
2. `preparar_datos`: corte anterior derivado (PERF_MONTH < corte) sin las filas que fallan las reglas
   críticas R-14/R-15, con nulos o sin exposición. Con `rapido=True` usa una fracción de las
   cohortes, estratificada por VINTAGE × STRATEGY.
3. Backtest con origen móvil (`basecards.modelo.backtest`), que además calibra la banda con CQR.
4. Modelos finales: 9 LightGBM cuantílicos (`ajustar_cuantiles`) y el auxiliar M4 (`ajustar_m4`).
5. Bundle versionado con su ficha del modelo; pasa a `champion` solo si mejora la pinball q50 del
   backtest y la cobertura CQR queda en 85–95% (Manual §5.3).
6. Registro en MLflow (`basecards.modelo.registro`), con alias `champion` si se promovió.

CLI: python -m basecards.modelo.entrenar --corte 202609 [--rapido] [--forzar] [--sin-mlflow]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd

from basecards import __version__
from basecards.ingesta.corte import leer_corte, leer_manifiesto, sumar_meses
from basecards.modelo.bundle import (
    MPDC,
    dir_modelos,
    etiqueta_cuantil,
    ordenar_cuantiles,
    sha256,
    version_champion,
)
from basecards.modelo.config import LLAVE_COHORTE, cargar_config, cargar_indicadores
from basecards.modelo.features import (
    FEATURES,
    FEATURES_M4,
    agregar_rezagos_over30,
    construir_tabla,
    filas_a_mob,
    matriz,
    niveles_categoricos,
)
from basecards.modelo.ficha import ficha_modelo
from basecards.soporte.config import cargar_reglas

log = logging.getLogger("basecards.modelo.entrenar")


class EntrenamientoError(RuntimeError):
    pass


@dataclass
class ResultadoEntrenamiento:
    version: int
    ruta: Path
    promovido: bool
    motivo_promocion: str
    metricas: dict[str, Any]
    run_id: str | None = None


# --------------------------------------------------------------------------------------------
# Datos
# --------------------------------------------------------------------------------------------
def cargar_corte(
    corte: int, *, dir_cortes: str | Path | None = None, forzar: bool = False
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Parquet y manifiesto del corte. Nunca se entrena con un corte `no apto` (salvo `forzar`)."""
    df = leer_corte(corte, dir_cortes=dir_cortes)
    try:
        manifiesto = leer_manifiesto(corte, dir_cortes=dir_cortes)
    except FileNotFoundError:
        manifiesto = {}
    dictamen = (manifiesto.get("validacion") or {}).get("dictamen")
    if dictamen == "no apto":
        if not forzar:
            raise EntrenamientoError(
                f"El corte {corte} tiene dictamen 'no apto' en la validación E1: no se entrena "
                "con él. Corrige el CSV o usa --forzar bajo tu responsabilidad."
            )
        log.warning("El corte %s es 'no apto', pero se usa porque se pidió --forzar", corte)
    elif dictamen is None:
        log.warning("El corte %s no tiene dictamen E1 (corre la ingesta con --validar)", corte)
    return df, manifiesto


def preparar_datos(
    df: pd.DataFrame, corte: int, cfg: dict[str, Any], *, rapido: bool = False, semilla: int = 42
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Tabla de entrenamiento y reporte de cuántas filas se excluyen por cada motivo."""
    mcfg = cfg["modelo"]
    tabla = construir_tabla(df, cfg)
    hasta = sumar_meses(corte, -1)
    info: dict[str, Any] = {"filas_corte": len(tabla), "entrenado_hasta": int(hasta)}

    # Corte anterior derivado (S-07): las filas con PERF_MONTH = corte no se ven al entrenar
    m_corte = tabla["PERF_MONTH"] > hasta
    tol = float(cargar_reglas().get("tolerancia_absoluta_montos", 0.01))
    m_r14 = (tabla["NCLAMTS"] - (tabla["GCLAMTS"] - tabla["RECOVERYAMTS"])).abs() > tol
    gastos = tabla["EXPENSES_ACQ"] + tabla["EXPENSES_ECM_VAR"] + tabla["EXPENSES_ECM_FIX"]
    m_r15 = (tabla["EXPENSES_TOTAL"] - gastos).abs() > tol
    numeradores = [o["numerador"] for o in mcfg["objetivos"].values()]
    m_nulos = tabla[[*LLAVE_COHORTE, "MOB", *numeradores]].isna().any(axis=1)
    expo = mcfg["exposicion_minima"]
    m_expo = tabla[expo["columna"]].fillna(0) < float(expo["valor"])

    info["filas_excluidas"] = {
        "perf_month_del_corte_actual": int(m_corte.sum()),
        "regla_critica_R14": int((m_r14 & ~m_corte).sum()),
        "regla_critica_R15": int((m_r15 & ~m_corte).sum()),
        "nulos": int((m_nulos & ~m_corte).sum()),
        f"exposicion_{expo['columna']}_menor_a_{expo['valor']}": int((m_expo & ~m_corte).sum()),
    }
    ent = tabla.loc[~(m_corte | m_r14 | m_r15 | m_nulos | m_expo)].copy()

    if rapido:
        # Muestra estratificada de cohortes completas (Manual §5.1)
        frac = float(mcfg["fraccion_rapido"])
        coh = ent.drop_duplicates(LLAVE_COHORTE)[LLAVE_COHORTE]
        muestra = coh.groupby(["VINTAGE", "STRATEGY"], observed=True, group_keys=False).sample(
            frac=frac, random_state=semilla
        )
        ent = ent.merge(muestra, on=LLAVE_COHORTE, how="inner")
        info["rapido"] = {"fraccion_cohortes": frac}

    info["filas_entrenamiento"] = len(ent)
    info["cohortes_entrenamiento"] = int(ent.drop_duplicates(LLAVE_COHORTE).shape[0])
    return ent.reset_index(drop=True), info


# --------------------------------------------------------------------------------------------
# Modelos
# --------------------------------------------------------------------------------------------
def ajustar_cuantiles(
    tabla: pd.DataFrame,
    niveles: dict[str, list[str]],
    cfg: dict[str, Any],
    n_estimators: int | None = None,
) -> dict[str, list[lgb.Booster]]:
    """Un `LGBMRegressor(objective="quantile")` por indicador y cuantil, ponderado por exposición."""
    mcfg = cfg["modelo"]
    params = dict(mcfg["lgbm"])
    if n_estimators:
        params["n_estimators"] = n_estimators
    modelos: dict[str, list[lgb.Booster]] = {}
    for obj, meta in mcfg["objetivos"].items():
        sub = tabla.loc[tabla[obj].notna() & (tabla[meta["peso"]].fillna(0) > 0)]
        X = matriz(sub, niveles, FEATURES)
        y = sub[obj].to_numpy()
        w = sub[meta["peso"]].to_numpy(dtype="float64")
        modelos[obj] = []
        for q in mcfg["cuantiles"]:
            t0 = time.perf_counter()
            m = lgb.LGBMRegressor(objective="quantile", alpha=q, **params)
            m.fit(X, y, sample_weight=w)
            modelos[obj].append(m.booster_)
            log.info(
                "  %s %s: %d filas, %.1fs",
                obj,
                etiqueta_cuantil(q),
                len(sub),
                time.perf_counter() - t0,
            )
    return modelos


def predecir_cuantiles(
    modelos: dict[str, list[lgb.Booster]], tabla: pd.DataFrame, niveles: dict[str, list[str]]
) -> dict[str, np.ndarray]:
    """Matriz (filas × cuantiles) por indicador, con los cuantiles ordenados por fila."""
    X = matriz(tabla, niveles, FEATURES)
    return {
        obj: ordenar_cuantiles(np.column_stack([b.predict(X) for b in bs]))
        for obj, bs in modelos.items()
    }


def mascara_m4(tabla_m4: pd.DataFrame) -> pd.Series:
    """Filas útiles para M4: tienen los 3 rezagos de Over30, el objetivo en t+3 y exposición."""
    return (
        tabla_m4["O30_OBJETIVO"].notna()
        & tabla_m4["O30_T2"].notna()
        & (tabla_m4["PESO_OBJETIVO"].fillna(0) > 0)
    )


def ajustar_m4(
    tabla_m4: pd.DataFrame, niveles: dict[str, list[str]], cfg: dict[str, Any]
) -> lgb.Booster:
    """M4: tasa Over30 en t+3 con rezagos t, t−1, t−2 y pendiente (LightGBM, pérdida L1)."""
    sub = tabla_m4.loc[mascara_m4(tabla_m4)]
    m = lgb.LGBMRegressor(**cfg["modelo"]["m4"]["lgbm"])
    m.fit(
        matriz(sub, niveles, FEATURES_M4),
        sub["O30_OBJETIVO"].to_numpy(),
        sample_weight=sub["PESO_OBJETIVO"].to_numpy(dtype="float64"),
    )
    return m.booster_


# --------------------------------------------------------------------------------------------
# Promoción y guardado
# --------------------------------------------------------------------------------------------
def decidir_promocion(resumen: dict[str, Any], cfg: dict[str, Any], base: Path) -> tuple[bool, str]:
    """`champion` solo si la cobertura CQR queda en rango y mejora la pinball q50 del actual."""
    p = cfg["modelo"]["promocion"]
    lo, hi = p["cobertura_min"], p["cobertura_max"]
    cobs = {o: r["cobertura_cqr"] for o, r in resumen["objetivos"].items()}
    fuera = {o: round(c, 4) for o, c in cobs.items() if not lo <= c <= hi}
    if fuera:
        return False, f"cobertura CQR fuera de {lo:.0%}–{hi:.0%}: {fuera}"
    actual = version_champion(base)
    if actual is None:
        return True, f"primer modelo; cobertura CQR en {lo:.0%}–{hi:.0%}"
    previo = json.loads((base / f"v{actual}" / "bundle.json").read_text(encoding="utf-8"))
    comunes = [o for o in resumen["objetivos"] if o in previo["metricas"]["objetivos"]]
    if not comunes:
        return False, f"no hay indicadores comparables con v{actual}"
    rel = float(
        np.mean(
            [
                resumen["objetivos"][o]["pinball_q50"]
                / previo["metricas"]["objetivos"][o]["pinball_q50"]
                for o in comunes
            ]
        )
    )
    if rel < 1.0:
        return True, f"mejora la pinball q50 de v{actual} (relativa {rel:.4f}) y la cobertura"
    return False, f"no mejora la pinball q50 de v{actual} (relativa {rel:.4f})"


def _siguiente_version(base: Path) -> int:
    vs = [int(p.name[1:]) for p in base.glob("v*") if p.name[1:].isdigit()]
    return max(vs, default=0) + 1


def guardar_bundle(
    base: Path,
    modelos: dict[str, list[lgb.Booster]],
    m4: lgb.Booster,
    meta: dict[str, Any],
    detalle_backtest: dict[str, Any],
) -> Path:
    """Escribe v{N}/ con los modelos, sus SHA-256, el backtest y la ficha del modelo."""
    ruta = base / f"v{meta['version']}"
    (ruta / "modelos").mkdir(parents=True, exist_ok=False)
    archivos = {
        f"modelos/{obj}_{etiqueta_cuantil(q)}.txt": b
        for obj, bs in modelos.items()
        for q, b in zip(meta["cuantiles"], bs, strict=True)
    }
    archivos["modelos/m4.txt"] = m4
    meta["hashes"] = {}
    for nombre, b in archivos.items():
        b.save_model(str(ruta / nombre))
        meta["hashes"][nombre] = sha256(ruta / nombre)
    escribir = {
        "bundle.json": json.dumps(meta, ensure_ascii=False, indent=2),
        "backtest.json": json.dumps(detalle_backtest, ensure_ascii=False, indent=2),
        "model_card.md": ficha_modelo(meta),
    }
    for nombre, texto in escribir.items():
        (ruta / nombre).write_text(texto, encoding="utf-8")
    return ruta


def marcar_champion(base: Path, version: int, motivo: str) -> None:
    """Mueve el alias `champion` a `version` (bundle.json de cada versión y champion.json)."""
    for p in base.glob("v*/bundle.json"):
        m = json.loads(p.read_text(encoding="utf-8"))
        es = int(m["version"]) == int(version)
        if m.get("es_champion") != es:
            m["es_champion"] = es
            p.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    puntero = {
        "version": int(version),
        "motivo": motivo,
        "fecha": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (base / "champion.json").write_text(
        json.dumps(puntero, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# --------------------------------------------------------------------------------------------
# Orquestación
# --------------------------------------------------------------------------------------------
def entrenar(
    corte: int,
    *,
    df: pd.DataFrame | None = None,
    dir_cortes: str | Path | None = None,
    dir_base: str | Path | None = None,
    rapido: bool = False,
    n_estimators: int | None = None,
    forzar: bool = False,
    mlflow: bool = True,
) -> ResultadoEntrenamiento:
    """Corte → backtest con CQR → modelos finales → bundle versionado (y `champion` si procede).

    Con `mlflow=True` también registra el run y el modelo pyfunc (`basecards.modelo.registro`).
    """
    # Import diferido: backtest importa las funciones de este módulo
    from basecards.modelo.backtest import backtest

    t0 = time.perf_counter()
    cfg = cargar_config()
    mcfg = cfg["modelo"]
    base = dir_modelos(dir_base)

    manifiesto: dict[str, Any] = {}
    if df is None:
        df, manifiesto = cargar_corte(corte, dir_cortes=dir_cortes, forzar=forzar)
    ent, info = preparar_datos(df, corte, cfg, rapido=rapido)
    hasta = info["entrenado_hasta"]
    log.info(
        "Entrenamiento: %d filas, %d cohortes, hasta %s",
        info["filas_entrenamiento"],
        info["cohortes_entrenamiento"],
        hasta,
    )

    log.info("Backtest con origen móvil…")
    bt = backtest(ent, hasta, cfg, n_estimators)

    log.info("Modelos finales…")
    niveles = niveles_categoricos(ent)
    modelos = ajustar_cuantiles(ent, niveles, cfg, n_estimators)
    m4cfg = mcfg["m4"]
    m4 = ajustar_m4(agregar_rezagos_over30(ent, m4cfg["rezagos"], m4cfg["adelanto"]), niveles, cfg)

    # Umbrales del nivel de riesgo: percentiles de la tasa NCL esperada (q50) al MOB horizonte
    h = int(mcfg["horizonte_ranking_mob"])
    ncl_h = predecir_cuantiles(modelos, filas_a_mob(ent, h), niveles)["tasa_ncl"][:, 1]
    pct = mcfg["nivel_riesgo"]["percentiles"]
    umbrales = {k: float(np.quantile(ncl_h, v)) for k, v in pct.items()}

    base.mkdir(parents=True, exist_ok=True)
    version = _siguiente_version(base)
    meta: dict[str, Any] = {
        "version": version,
        "creado": datetime.now(UTC).isoformat(timespec="seconds"),
        "version_basecards": __version__,
        "corte": int(corte),
        "entrenado_hasta": int(hasta),
        "rapido": bool(rapido),
        "acumulado": bool(cargar_indicadores().get("acumulado", False)),
        "features": FEATURES,
        "features_m4": FEATURES_M4,
        "niveles": niveles,
        "cuantiles": mcfg["cuantiles"],
        "objetivos": mcfg["objetivos"],
        "lgbm": {**mcfg["lgbm"], **({"n_estimators": n_estimators} if n_estimators else {})},
        "cqr": {**mcfg["cqr"], "offsets": bt["cqr_offsets"]},
        "m4": {**m4cfg, **bt["m4_residuos"]},
        "nivel_riesgo": {"horizonte_mob": h, "percentiles": pct, "umbrales": umbrales},
        "horizonte_ranking_mob": h,
        "promocion_cfg": mcfg["promocion"],
        "datos": {
            **info,
            "sha256_csv": (manifiesto.get("fuente") or {}).get("sha256"),
            "sha256_parquet": (manifiesto.get("parquet") or {}).get("sha256"),
            "dictamen_e1": (manifiesto.get("validacion") or {}).get("dictamen"),
        },
        "metricas": bt["resumen"],
        "es_champion": False,
    }
    promover, motivo = decidir_promocion(bt["resumen"], cfg, base)
    meta["promocion"] = {"promovido": promover, "motivo": motivo}
    ruta = guardar_bundle(base, modelos, m4, meta, {"por_origen": bt["por_origen"]})
    if promover:
        marcar_champion(base, version, motivo)
    log.info(
        "Bundle v%d en %s (%.0fs). Champion: %s (%s)",
        version,
        ruta,
        time.perf_counter() - t0,
        promover,
        motivo,
    )

    # El bundle recién guardado se puede cargar y predecir
    MPDC(ruta).predecir(ent.head(5))

    run_id = None
    if mlflow:
        try:
            from basecards.modelo.registro import registrar

            run_id = registrar(ruta, meta, promover)
        except Exception as e:  # noqa: BLE001
            # MLflow es trazabilidad: si falla, el bundle local sigue siendo válido
            log.warning("No se pudo registrar en MLflow: %s", e)
    return ResultadoEntrenamiento(version, ruta, promover, motivo, bt["resumen"], run_id)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="basecards.modelo.entrenar", description="Entrena el MPDC")
    p.add_argument("--corte", type=int, default=202609)
    p.add_argument(
        "--dir-cortes", default=None, help="Raíz de los cortes (por defecto data/cortes)"
    )
    p.add_argument("--dir-modelos", default=None, help="Raíz de los bundles (modelo.dir_modelos)")
    p.add_argument("--rapido", action="store_true", help="Muestra estratificada de cohortes")
    p.add_argument("--n-estimators", type=int, default=None, help="Sobrescribe n_estimators")
    p.add_argument("--forzar", action="store_true", help="Entrena aunque el dictamen sea 'no apto'")
    p.add_argument("--sin-mlflow", action="store_true", help="No registra el modelo en MLflow")
    args = p.parse_args(argv)
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        res = entrenar(
            args.corte,
            dir_cortes=args.dir_cortes,
            dir_base=args.dir_modelos,
            rapido=args.rapido,
            n_estimators=args.n_estimators,
            forzar=args.forzar,
            mlflow=not args.sin_mlflow,
        )
    except (EntrenamientoError, FileNotFoundError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    resumen = {
        "version": res.version,
        "ruta": str(res.ruta),
        "champion": res.promovido,
        "motivo": res.motivo_promocion,
        "metricas": res.metricas["objetivos"],
        "m4": res.metricas.get("m4"),
        "mlflow_run": res.run_id,
    }
    print(json.dumps(resumen, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
