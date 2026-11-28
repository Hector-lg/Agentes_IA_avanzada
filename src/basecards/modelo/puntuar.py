"""E2 — Scoring del corte con el modelo champion (PRD §6.5–6.6).

Escribe en `data/predicciones/{corte}/` (la carpeta de `ruta_parquet` en config/predicciones.yaml):

- predicciones.parquet            tabla `predicciones_cohorte` del contrato E2 → E3, en formato
                                  largo: una fila por cohorte × MOB × indicador con q05/q50/q95,
                                  observado, desviación normalizada, `fuera_de_intervalo`,
                                  exposición, 3 drivers TreeSHAP y versión del modelo.
- cohortes_horizonte.parquet      banda de cada cohorte al MOB 12 y su nivel de riesgo.
- ranking_estrategias.parquet     estrategia × segmento por margen esperado al MOB 12.
- pronostico_over30_3mob.parquet  M4 desde el último MOB observado de cada cohorte.
- evento_predicciones_listas.json evento `predicciones_listas(corte_id, version_modelo)` para E3.

Antes de puntuar se verifica el SHA-256 de cada archivo del bundle. Con un corte `no apto` en la
validación E1 no se puntúa (PRD §4: E1 pasa a E2 solo sin fallas críticas), salvo con `forzar`.

CLI: python -m basecards.modelo.puntuar --corte 202609 [--version N] [--forzar]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from basecards.ingesta.corte import leer_corte, leer_manifiesto, perf_month, sha256_archivo
from basecards.modelo.bundle import MPDC, ruta_version, verificar_bundle
from basecards.modelo.config import LLAVE_COHORTE, LLAVE_FILA, resolver
from basecards.modelo.features import agregar_rezagos_over30, construir_tabla, filas_a_mob
from basecards.soporte.config import cargar_predicciones_cfg
from basecards.soporte.predicciones.esquema import validar_esquema_predicciones

log = logging.getLogger("basecards.modelo.puntuar")

MODELO_NOMBRE = "mpdc"
RUTA_PARQUET = "data/predicciones/{corte}/predicciones.parquet"
NOMBRE_EVENTO = "evento_predicciones_listas.json"
N_DRIVERS = 3
# Desviación de una fila fuera de una banda de ancho 0 (por ejemplo q05 = q50 = 0 por el recorte)
TOPE_DESVIACION = 99.0


class PuntuacionError(RuntimeError):
    pass


# --------------------------------------------------------------------------------------------
# Entradas
# --------------------------------------------------------------------------------------------
def dir_predicciones(corte: int, dir_base: str | Path | None = None) -> Path:
    """`dir_base/{corte}`; por defecto, la carpeta de `ruta_parquet` del contrato E2 → E3."""
    if dir_base is not None:
        return Path(dir_base) / str(int(corte))
    plantilla = str(cargar_predicciones_cfg().get("ruta_parquet", RUTA_PARQUET))
    return resolver(plantilla.format(corte=int(corte))).parent


def cargar_corte_apto(
    corte: int, *, dir_cortes: str | Path | None = None, forzar: bool = False
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Parquet y manifiesto del corte; un corte `no apto` en E1 no se puntúa (salvo `forzar`)."""
    df = leer_corte(corte, dir_cortes=dir_cortes)
    try:
        manifiesto = leer_manifiesto(corte, dir_cortes=dir_cortes)
    except FileNotFoundError:
        manifiesto = {}
    dictamen = (manifiesto.get("validacion") or {}).get("dictamen")
    if dictamen == "no apto":
        if not forzar:
            raise PuntuacionError(
                f"El corte {corte} tiene dictamen 'no apto' en la validación E1: no pasa a E2. "
                "Corrige el CSV o usa --forzar bajo tu responsabilidad."
            )
        log.warning("El corte %s es 'no apto', pero se puntúa porque se pidió --forzar", corte)
    elif dictamen is None:
        log.warning("El corte %s no tiene dictamen E1 (corre la ingesta con --validar)", corte)
    return df, manifiesto


def indicadores_contrato(mpdc: MPDC, pcfg: dict[str, Any]) -> list[str]:
    """Indicadores que pide el contrato (`indicadores` en config/predicciones.yaml).

    K-01 compara NCL y Over30 (Manual §2.6.3); si el contrato no los lista, van todos los del MPDC.
    """
    pedidos = list(pcfg.get("indicadores") or mpdc.objetivos)
    faltan = [i for i in pedidos if i not in mpdc.objetivos]
    if faltan:
        raise PuntuacionError(
            f"El contrato pide {faltan}, pero el MPDC v{mpdc.version} no los predice"
        )
    return pedidos


# --------------------------------------------------------------------------------------------
# Salidas
# --------------------------------------------------------------------------------------------
def desviacion_normalizada(obs: np.ndarray, banda: np.ndarray) -> np.ndarray:
    """PRD §6.6: (obs − q50)/(q95 − q50) hacia arriba y (q50 − obs)/(q50 − q05) hacia abajo.

    Nunca es negativa y vale 1 en el borde de la banda, así que la fila está fuera de intervalo
    si y solo si la desviación pasa de 1. Si la banda no tiene ancho de ese lado, vale 0 en q50 y
    `TOPE_DESVIACION` fuera de él. Sin observado, NaN.
    """
    obs = np.asarray(obs, dtype="float64")
    q05, q50, q95 = banda[:, 0], banda[:, 1], banda[:, 2]
    arriba = obs >= q50
    dif = np.where(arriba, obs - q50, q50 - obs)
    escala = np.where(arriba, q95 - q50, q50 - q05)
    with np.errstate(divide="ignore", invalid="ignore"):
        d = np.where(escala > 1e-12, dif / escala, np.where(dif > 1e-12, TOPE_DESVIACION, 0.0))
    return np.where(np.isnan(obs), np.nan, np.clip(d, 0.0, TOPE_DESVIACION))


def llave_como_texto(df: pd.DataFrame) -> pd.DataFrame:
    """Las dimensiones de catálogo salen como texto, igual que en el mock del contrato."""
    for c in LLAVE_COHORTE:
        if c in df.columns and not pd.api.types.is_numeric_dtype(df[c]):
            df[c] = df[c].astype(str)
    return df


def _columnas_drivers(
    mpdc: MPDC, tabla: pd.DataFrame, indicador: str, filas: np.ndarray
) -> dict[str, np.ndarray]:
    """driver_k (feature, con su valor si es categórica) y contribucion_k en las `filas` dadas."""
    n = len(tabla)
    cols: dict[str, np.ndarray] = {}
    for k in range(1, N_DRIVERS + 1):
        cols[f"driver_{k}"] = np.full(n, None, dtype=object)
        cols[f"contribucion_{k}"] = np.full(n, np.nan)
    if len(filas):
        for i, items in zip(
            filas, mpdc.drivers(tabla.iloc[filas], indicador, N_DRIVERS), strict=True
        ):
            for k, item in enumerate(items, start=1):
                cols[f"driver_{k}"][i] = item["feature"]
                cols[f"contribucion_{k}"][i] = item["contribucion"]
    return cols


def tabla_predicciones(
    mpdc: MPDC,
    tabla: pd.DataFrame,
    pred: dict[str, np.ndarray],
    indicadores: list[str],
    *,
    enraccts_min: int,
) -> pd.DataFrame:
    """`predicciones_cohorte` en formato largo (una fila por cohorte × MOB × indicador).

    Llevan drivers las filas nuevas del corte (`fuera_de_tiempo`, PERF_MONTH posterior a lo que vio
    el modelo) y las que K-01 va a revisar: fuera de intervalo con ENRACCTS ≥ `enraccts_min`.
    """
    tabla = tabla.reset_index(drop=True)
    base = llave_como_texto(tabla[LLAVE_FILA].copy())
    fuera_de_tiempo = (tabla["PERF_MONTH"] > int(mpdc.meta["entrenado_hasta"])).to_numpy()
    enraccts = pd.to_numeric(tabla["ENRACCTS"]).fillna(0).astype("int64").to_numpy()
    nclamts = pd.to_numeric(tabla["NCLAMTS"]).astype("float64").to_numpy()
    partes = []
    for ind in indicadores:
        banda = pred[ind]
        obs = tabla[ind].to_numpy(dtype="float64")
        fuera = (obs < banda[:, 0]) | (obs > banda[:, 2])  # NaN → False
        con_drivers = np.flatnonzero(fuera_de_tiempo | (fuera & (enraccts >= enraccts_min)))
        drivers = _columnas_drivers(mpdc, tabla, ind, con_drivers)
        parte = base.copy()
        parte["indicador"] = ind
        parte["q05"], parte["q50"], parte["q95"] = banda[:, 0], banda[:, 1], banda[:, 2]
        parte["observado"] = obs
        parte["desviacion"] = desviacion_normalizada(obs, banda)
        parte["fuera_de_intervalo"] = fuera
        parte["enraccts"] = enraccts
        parte["nclamts"] = nclamts
        for k in range(1, N_DRIVERS + 1):
            parte[f"driver_{k}"] = pd.Series(drivers[f"driver_{k}"], dtype="str")
        parte["modelo_nombre"] = MODELO_NOMBRE
        parte["modelo_version"] = mpdc.version
        parte["PERF_MONTH"] = tabla["PERF_MONTH"].to_numpy()
        parte["fuera_de_tiempo"] = fuera_de_tiempo
        for k in range(1, N_DRIVERS + 1):
            parte[f"contribucion_{k}"] = drivers[f"contribucion_{k}"]
        partes.append(parte)
    return pd.concat(partes, ignore_index=True)


def cohortes_horizonte(mpdc: MPDC, tabla: pd.DataFrame) -> pd.DataFrame:
    """Una fila por cohorte: banda de los 3 indicadores al MOB horizonte, observado y nivel de riesgo.

    Las cohortes que aún no llegan a ese MOB se proyectan con `filas_a_mob`; su observado queda NaN.
    """
    h = int(mpdc.meta["horizonte_ranking_mob"])
    base = filas_a_mob(tabla, h)
    out = base[[*LLAVE_COHORTE, "FAMILIA", "ACCOUNTS_0", "MOB"]].copy()
    for obj, p in mpdc.predecir(base).items():
        out[f"{obj}_q05"], out[f"{obj}_q50"], out[f"{obj}_q95"] = p[:, 0], p[:, 1], p[:, 2]
    obs = tabla.loc[tabla["MOB"] == h, [*LLAVE_COHORTE, *mpdc.objetivos]]
    obs = obs.rename(columns={o: f"{o}_observado" for o in mpdc.objetivos})
    out = out.merge(obs, on=LLAVE_COHORTE, how="left", validate="one_to_one")
    out["nivel_riesgo"] = [mpdc.nivel_riesgo(v) for v in out["tasa_ncl_q50"]]
    return llave_como_texto(out)


def ranking_estrategias(coh: pd.DataFrame, por: list[str] | None = None) -> pd.DataFrame:
    """Estrategia × segmento ordenados por margen esperado al MOB horizonte (PRD §6.5).

    Cada columna es el promedio de las cohortes ponderado por sus cuentas originadas (ACCOUNTS_0),
    o sea, una razón de sumas. El margen ya descuenta la NCL, así que está ajustado por riesgo.
    """
    por = por or ["STRATEGY", "STRATEGY_SEGMENT"]
    cols = [c for c in coh.columns if c.endswith(("_q05", "_q50", "_q95", "_observado"))]
    w = coh["ACCOUNTS_0"].astype("float64")
    tmp = coh[por].copy()
    tmp["_cuentas"] = w
    for c in cols:
        valido = coh[c].notna()
        tmp[c] = coh[c].where(valido, 0.0) * w
        tmp[f"_w_{c}"] = w.where(valido, 0.0)
    g = tmp.groupby(por, observed=True, sort=False)
    suma = g.sum(numeric_only=True)
    out = pd.DataFrame(index=suma.index)
    out["cohortes"] = g.size()
    out["cohortes_con_observado"] = (
        coh.assign(_obs=coh["margen_por_cuenta_observado"].notna())
        .groupby(por, observed=True, sort=False)["_obs"]
        .sum()
    )
    out["cuentas"] = suma["_cuentas"].astype("int64")
    for c in cols:
        out[c] = suma[c] / suma[f"_w_{c}"].where(suma[f"_w_{c}"] > 0)
    out = out.reset_index().sort_values("margen_por_cuenta_q50", ascending=False)
    out.insert(0, "ranking", np.arange(1, len(out) + 1))
    return out.reset_index(drop=True)


def pronostico_m4(mpdc: MPDC, tabla: pd.DataFrame) -> pd.DataFrame:
    """M4 desde el último MOB observado de cada cohorte: tasa Over30 3 MOB adelante con su banda.

    Quedan fuera las cohortes sin 3 MOB contiguos al final (por ejemplo, las de MOB 0–1).
    """
    m4 = mpdc.meta["m4"]
    t = agregar_rezagos_over30(tabla, m4["rezagos"], m4["adelanto"])
    ultimo = t.drop_duplicates(LLAVE_COHORTE, keep="last")
    ultimo = ultimo.loc[ultimo["O30_T2"].notna()].reset_index(drop=True)
    out = ultimo[[*LLAVE_COHORTE, "MOB", "PERF_MONTH", "O30_T"]].rename(
        columns={"MOB": "MOB_ULTIMO", "O30_T": "tasa_over30_ultimo"}
    )
    out["MOB_OBJETIVO"] = out["MOB_ULTIMO"] + int(m4["adelanto"])
    out["PERF_MONTH_OBJETIVO"] = perf_month(out["VINTAGE"], out["MOB_OBJETIVO"])
    p = mpdc.pronosticar_over30(ultimo)
    if p is None:
        raise PuntuacionError(f"El MPDC v{mpdc.version} no trae el modelo M4 (modelos/m4.txt)")
    for j, q in enumerate(("q05", "q50", "q95")):
        out[f"tasa_over30_{q}"] = p[:, j]
    return llave_como_texto(out)


def _pct(mascara: pd.Series) -> float | None:
    return round(100 * float(mascara.mean()), 2) if len(mascara) else None


def resumen_predicciones(largo: pd.DataFrame, enraccts_min: int) -> dict[str, Any]:
    """Por indicador: % fuera de intervalo (todas las filas y las nuevas) y candidatas a K-01."""
    out = {}
    for ind, g in largo.groupby("indicador", sort=False):
        g = g.loc[g["observado"].notna()]
        nuevas = g.loc[g["fuera_de_tiempo"]]
        out[ind] = {
            "filas_con_observado": len(g),
            "pct_fuera_de_intervalo": _pct(g["fuera_de_intervalo"]),
            "filas_nuevas": len(nuevas),
            "pct_fuera_de_intervalo_filas_nuevas": _pct(nuevas["fuera_de_intervalo"]),
            "filas_con_drivers": int(g["driver_1"].notna().sum()),
            "candidatas_k01": int(
                (g["fuera_de_intervalo"] & (g["enraccts"] >= enraccts_min)).sum()
            ),
        }
    return out


# --------------------------------------------------------------------------------------------
# Orquestación
# --------------------------------------------------------------------------------------------
def puntuar(
    corte: int,
    *,
    version: int | str = "champion",
    df: pd.DataFrame | None = None,
    dir_cortes: str | Path | None = None,
    dir_modelos: str | Path | None = None,
    dir_salida: str | Path | None = None,
    forzar: bool = False,
) -> dict[str, Any]:
    """Corte → bundle verificado → 4 Parquet de salida → evento `predicciones_listas`."""
    t0 = time.perf_counter()
    ruta = ruta_version(version, dir_modelos)
    alterados = verificar_bundle(ruta)
    if alterados:
        raise PuntuacionError(
            f"El bundle {ruta} no pasa la verificación de integridad (faltan o cambiaron: "
            f"{alterados}). No se puntúa con un modelo alterado."
        )
    mpdc = MPDC(ruta)
    manifiesto: dict[str, Any] = {}
    if df is None:
        df, manifiesto = cargar_corte_apto(corte, dir_cortes=dir_cortes, forzar=forzar)
    pcfg = cargar_predicciones_cfg()
    indicadores = indicadores_contrato(mpdc, pcfg)
    enraccts_min = int(pcfg.get("enraccts_min", 200))

    salida = dir_predicciones(corte, dir_salida)
    salida.mkdir(parents=True, exist_ok=True)
    # El evento anuncia salidas completas: el de una corrida anterior se borra antes de escribir
    (salida / NOMBRE_EVENTO).unlink(missing_ok=True)

    tabla = construir_tabla(df)
    hasta = int(mpdc.meta["entrenado_hasta"])
    nuevas = int((tabla["PERF_MONTH"] > hasta).sum())
    log.info(
        "Puntuando el corte %s con MPDC v%d: %d filas, %d posteriores a %s",
        corte,
        mpdc.version,
        len(tabla),
        nuevas,
        hasta,
    )
    if not nuevas:
        log.warning("Ninguna fila es posterior a %s: no hay filas fuera de tiempo", hasta)
    pred = mpdc.predecir(tabla)
    largo = tabla_predicciones(mpdc, tabla, pred, indicadores, enraccts_min=enraccts_min)
    validar_esquema_predicciones(largo)
    coh = cohortes_horizonte(mpdc, tabla)
    salidas = {
        "predicciones.parquet": largo,
        "cohortes_horizonte.parquet": coh,
        "ranking_estrategias.parquet": ranking_estrategias(coh),
        "pronostico_over30_3mob.parquet": pronostico_m4(mpdc, tabla),
    }
    archivos = {}
    for nombre, tabla_salida in salidas.items():
        tabla_salida.to_parquet(salida / nombre, index=False, compression="zstd")
        archivos[nombre] = {"filas": len(tabla_salida), "sha256": sha256_archivo(salida / nombre)}
        log.info("  %s: %d filas", nombre, len(tabla_salida))

    resumen = {
        "corte": int(corte),
        "version_modelo": mpdc.version,
        "entrenado_hasta": hasta,
        "indicadores": indicadores,
        "filas_cohorte_mob": len(tabla),
        "cohortes": len(coh),
        "filas_predicciones": len(largo),
        "filas_fuera_de_tiempo": nuevas,
        "por_indicador": resumen_predicciones(largo, enraccts_min),
        "estrategias_rankeadas": archivos["ranking_estrategias.parquet"]["filas"],
        "cohortes_pronostico_m4": archivos["pronostico_over30_3mob.parquet"]["filas"],
        "duracion_s": round(time.perf_counter() - t0, 1),
    }
    evento = {
        "evento": "predicciones_listas",
        "corte_id": int(corte),
        "version_modelo": mpdc.version,
        "modelo_nombre": MODELO_NOMBRE,
        "creado": datetime.now(UTC).isoformat(timespec="seconds"),
        "dictamen_e1": (manifiesto.get("validacion") or {}).get("dictamen"),
        "archivos": archivos,
        "resumen": resumen,
    }
    (salida / NOMBRE_EVENTO).write_text(
        json.dumps(evento, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("Evento predicciones_listas en %s (%.0fs)", salida, resumen["duracion_s"])
    return evento


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="basecards.modelo.puntuar", description="Puntúa un corte con el MPDC"
    )
    p.add_argument("--corte", type=int, default=202609)
    p.add_argument("--version", default="champion", help="Número de versión o 'champion'")
    p.add_argument(
        "--dir-cortes", default=None, help="Raíz de los cortes (por defecto data/cortes)"
    )
    p.add_argument("--dir-modelos", default=None, help="Raíz de los bundles (modelo.dir_modelos)")
    p.add_argument(
        "--dir-salida", default=None, help="Raíz de las salidas (por defecto data/predicciones)"
    )
    p.add_argument("--forzar", action="store_true", help="Puntúa aunque el dictamen sea 'no apto'")
    args = p.parse_args(argv)
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    version = args.version if args.version == "champion" else int(args.version)
    try:
        evento = puntuar(
            args.corte,
            version=version,
            dir_cortes=args.dir_cortes,
            dir_modelos=args.dir_modelos,
            dir_salida=args.dir_salida,
            forzar=args.forzar,
        )
    except (PuntuacionError, FileNotFoundError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(json.dumps(evento["resumen"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
