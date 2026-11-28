"""API del MPDC (E2, RN-02): `POST /score-cohorte` y consultas para el Copiloto (E5).

Levantar:   uvicorn basecards.api.app:app --host 127.0.0.1 --port 8000   (documentación en /docs)
Variables:  FECHA_CORTE (por defecto, el corte del modelo), MPDC_VERSION (champion o un número),
            MPDC_DIR_MODELOS (modelo.dir_modelos) y DIR_CORTES (ingesta.dir_cortes).

De solo lectura. Al arrancar verifica el SHA-256 del bundle (si no pasa, no carga el modelo) y
precalcula originación, observados, banda al MOB 12 y M4 de cada cohorte del corte, así que cada
solicitud solo predice los MOB de una cohorte. JWT y `policy.check` los agrega E0.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response

from basecards import __version__
from basecards.api import esquemas
from basecards.api.esquemas import AAAAMM, SolicitudScore
from basecards.ingesta.corte import leer_corte, leer_manifiesto
from basecards.modelo import puntuar
from basecards.modelo.bundle import MPDC, ruta_version, verificar_bundle
from basecards.modelo.config import LLAVE_COHORTE, LLAVE_FILA
from basecards.modelo.features import ORIGINACION, construir_tabla, tabla_desde_cohortes

log = logging.getLogger("basecards.api")

NIVELES = ["bajo", "medio", "alto", "critico"]
Q = ("q05", "q50", "q95")
# Lado de la banda que es malo: arriba en las tasas de riesgo, abajo en el margen
SENTIDO_MALO = {"tasa_ncl": 1, "tasa_over30": 1, "margen_por_cuenta": -1}
# Mismo corte que la severidad crítica de K-01 (PRD §6.6)
DESVIACION_CRITICA = 2.5


@dataclass
class Estado:
    """Lo que la API carga al arrancar. Sin `mpdc`, los endpoints del modelo responden 503."""

    mpdc: MPDC | None = None
    corte: int | None = None
    dictamen_e1: str | None = None
    alterados: list[str] | None = None  # archivos del bundle que no pasan el SHA-256
    originacion: pd.DataFrame | None = None  # una fila por cohorte, en el orden de `indice`
    indice: dict[tuple, int] = field(default_factory=dict)  # llave de cohorte → fila
    tramos: np.ndarray | None = None  # filas [inicio, fin) de cada cohorte en `observados`
    observados: dict[str, np.ndarray] = field(default_factory=dict)  # MOB y cada indicador
    horizonte: pd.DataFrame | None = None  # banda de cada cohorte al MOB horizonte
    m4: dict[tuple, dict[str, Any]] = field(default_factory=dict)  # llave → pronóstico M4
    segundos_carga: float | None = None
    error: str | None = None

    def observados_de(self, i: int) -> dict[str, dict[int, float]]:
        """{indicador: {MOB: valor}} de la cohorte `i`, sin los NaN."""
        tramo = slice(*self.tramos[i])
        mobs = self.observados["MOB"][tramo].tolist()
        return {
            o: {m: v for m, v in zip(mobs, self.observados[o][tramo].tolist()) if not np.isnan(v)}
            for o in self.mpdc.objetivos
        }


def _llaves(df: pd.DataFrame) -> list[tuple]:
    return list(zip(*(df[c].tolist() for c in LLAVE_COHORTE), strict=True))


def cargar_estado(
    corte: int | None = None,
    *,
    version: int | str | None = None,
    dir_modelos: str | Path | None = None,
    dir_cortes: str | Path | None = None,
    df: pd.DataFrame | None = None,
) -> Estado:
    """Bundle verificado + corte → `Estado`. Los argumentos que faltan salen de las variables."""
    t0 = time.perf_counter()
    version = version or os.environ.get("MPDC_VERSION") or "champion"
    version = version if version == "champion" else int(version)
    ruta = ruta_version(version, dir_modelos or os.environ.get("MPDC_DIR_MODELOS"))
    alterados = verificar_bundle(ruta)
    if alterados:
        error = f"El bundle {ruta} no pasa la verificación de integridad: {alterados}"
        return Estado(alterados=alterados, error=error)
    mpdc = MPDC(ruta)
    corte = int(corte or os.environ.get("FECHA_CORTE") or mpdc.meta["corte"])
    dictamen = None
    if df is None:
        dir_cortes = dir_cortes or os.environ.get("DIR_CORTES")
        df = leer_corte(corte, dir_cortes=dir_cortes)
        try:
            validacion = leer_manifiesto(corte, dir_cortes=dir_cortes).get("validacion") or {}
            dictamen = validacion.get("dictamen")
        except FileNotFoundError:
            pass
    tabla = construir_tabla(df).sort_values(LLAVE_FILA)
    m4 = puntuar.pronostico_m4(mpdc, tabla).rename(columns={f"tasa_over30_{q}": q for q in Q})

    # Ordenada por llave, cada cohorte ocupa las filas contiguas [inicio, fin)
    cols = [*LLAVE_FILA, "FAMILIA", "MOB_ORIGEN", *ORIGINACION, *mpdc.objetivos]
    obs = puntuar.llave_como_texto(tabla[cols].reset_index(drop=True))
    inicio = np.flatnonzero(~obs.duplicated(LLAVE_COHORTE).to_numpy())
    fin = np.r_[inicio[1:], len(obs)]
    orig = obs.iloc[fin - 1].drop(columns=mpdc.objetivos).rename(columns={"MOB": "MOB_ULTIMO"})
    e = Estado(
        mpdc=mpdc,
        corte=corte,
        dictamen_e1=dictamen,
        alterados=[],
        originacion=orig.reset_index(drop=True),
        indice={k: i for i, k in enumerate(_llaves(orig))},
        tramos=np.column_stack([inicio, fin]),
        observados={c: obs[c].to_numpy() for c in ["MOB", *mpdc.objetivos]},
        horizonte=puntuar.cohortes_horizonte(mpdc, tabla),
        m4=dict(zip(_llaves(m4), m4.drop(columns=LLAVE_COHORTE).to_dict("records"), strict=True)),
    )
    e.segundos_carga = round(time.perf_counter() - t0, 1)
    log.info("MPDC v%d y corte %s cargados en %.1fs", mpdc.version, corte, e.segundos_carga)
    return e


def estado_desde_entorno() -> Estado:
    """Como `cargar_estado()`, pero si falla la API arranca igual y `/salud` dice por qué."""
    try:
        return cargar_estado()
    except Exception as ex:  # noqa: BLE001
        log.error("No se pudo cargar el MPDC: %s: %s", type(ex).__name__, ex)
        return Estado(error=f"{type(ex).__name__}: {ex}")


def _estado(request: Request) -> Estado:
    return request.app.state.estado


def _con_modelo(e: Annotated[Estado, Depends(_estado)]) -> Estado:
    if e.mpdc is None:
        raise HTTPException(status_code=503, detail=f"Modelo no disponible: {e.error}")
    return e


EstadoDep = Annotated[Estado, Depends(_estado)]
ModeloDep = Annotated[Estado, Depends(_con_modelo)]
router = APIRouter()


def _registros(df: pd.DataFrame) -> list[dict[str, Any]]:
    return df.astype(object).where(df.notna(), None).to_dict("records")


# --------------------------------------------------------------------------------------------
# Consultas
# --------------------------------------------------------------------------------------------
@router.get("/salud")
def salud(e: EstadoDep, response: Response) -> dict[str, Any]:
    """Estado de la API: 200 con el modelo cargado; 503 si no se pudo cargar."""
    integridad_ok = None if e.alterados is None else not e.alterados
    if e.mpdc is None:
        response.status_code = 503
        return {
            "estado": "sin_modelo",
            "error": e.error,
            "integridad_ok": integridad_ok,
            "archivos_alterados": e.alterados or [],
        }
    return {
        "estado": "ok",
        "modelo": e.mpdc.info(),
        "corte": e.corte,
        "dictamen_e1": e.dictamen_e1,
        "cohortes": len(e.originacion),
        "integridad_ok": integridad_ok,
        "segundos_carga": e.segundos_carga,
    }


@router.get("/modelo")
def modelo(e: ModeloDep) -> dict[str, Any]:
    """Metadatos del champion: datos, métricas del backtest, umbrales de riesgo y su ficha."""
    m = e.mpdc.meta
    claves = ["features", "cuantiles", "objetivos", "datos", "promocion", "nivel_riesgo"]
    return {
        **e.mpdc.info(),
        **{k: m.get(k) for k in [*claves, "horizonte_ranking_mob"]},
        "metricas_backtest": m["metricas"],
        "ficha": str(e.mpdc.ruta / "model_card.md"),
    }


@router.get("/ranking-estrategias", response_model=esquemas.RespuestaRanking)
def ranking(
    e: ModeloDep,
    vintage_desde: Annotated[AAAAMM | None, Query(examples=[202511])] = None,
    vintage_hasta: Annotated[AAAAMM | None, Query(examples=[202609])] = None,
    strategy: Annotated[list[str] | None, Query()] = None,
    familia: Annotated[str | None, Query(examples=["DARIA"])] = None,
    por_segmento: bool = True,
    top: Annotated[int, Query(ge=1, le=500)] = 50,
) -> dict[str, Any]:
    """Estrategias (× segmento) por margen esperado al MOB 12 (`modelo_comparar_estrategias`)."""
    coh = e.horizonte
    if vintage_desde is not None:
        coh = coh.loc[coh["VINTAGE"] >= vintage_desde]
    if vintage_hasta is not None:
        coh = coh.loc[coh["VINTAGE"] <= vintage_hasta]
    if strategy:
        coh = coh.loc[coh["STRATEGY"].isin([s.strip() for s in strategy])]
    if familia:
        coh = coh.loc[coh["FAMILIA"] == familia.strip()]
    por = ["STRATEGY", "STRATEGY_SEGMENT"] if por_segmento else ["STRATEGY"]
    r = puntuar.ranking_estrategias(coh, por) if len(coh) else pd.DataFrame()
    h = int(e.mpdc.meta["horizonte_ranking_mob"])
    filas = _registros(r.head(top))
    return {"modelo": e.mpdc.info(), "horizonte_mob": h, "total": len(r), "ranking": filas}


@router.get("/cohortes", response_model=esquemas.RespuestaCohortes)
def cohortes(
    e: ModeloDep,
    strategy: str | None = None,
    segmento: str | None = None,
    vintage: Annotated[AAAAMM | None, Query()] = None,
    limite: Annotated[int, Query(ge=1, le=500)] = 20,
) -> dict[str, Any]:
    """Llaves de cohortes del corte, de más a menos cuentas, para descubrir qué puntuar."""
    o = e.originacion
    if strategy:
        o = o.loc[o["STRATEGY"] == strategy.strip()]
    if segmento:
        o = o.loc[o["STRATEGY_SEGMENT"] == segmento.strip()]
    if vintage is not None:
        o = o.loc[o["VINTAGE"] == vintage]
    o = o.sort_values("ACCOUNTS_0", ascending=False, kind="stable")
    cols = [*LLAVE_COHORTE, "FAMILIA", "ACCOUNTS_0", "MOB_ULTIMO"]
    return {"total": len(o), "cohortes": _registros(o[cols].head(limite))}


# --------------------------------------------------------------------------------------------
# POST /score-cohorte
# --------------------------------------------------------------------------------------------
def _originacion(
    e: Estado, sol: SolicitudScore, i: int | None, adv: list[str]
) -> tuple[dict[str, Any], str]:
    """Originación de la solicitud, la del corte o, si la cohorte es nueva, imputada."""
    if sol.originacion is not None:
        o = sol.originacion
        enr = np.nan if o.ENRAMTS is None else o.ENRAMTS
        orig = dict(zip(ORIGINACION, (o.ACCOUNTS, o.CREDITLINESAMTS, enr), strict=True))
        return orig | {"MOB_ORIGEN": 0}, "solicitud"
    o = e.originacion
    if i is not None:
        orig = {k: float(o[k].iat[i]) for k in ORIGINACION}
        return orig | {"MOB_ORIGEN": int(o["MOB_ORIGEN"].iat[i])}, "corte"
    # Cohorte fuera del corte: mediana de las parecidas (estrategia × segmento × producto,
    # luego estrategia y, si no hay, todas)
    c = sol.cohorte
    misma = o["STRATEGY"] == c.STRATEGY
    for filtro in (
        misma & (o["STRATEGY_SEGMENT"] == c.STRATEGY_SEGMENT) & (o["PRODUCT"] == c.PRODUCT),
        misma,
        pd.Series(True, index=o.index),
    ):
        sub = o.loc[filtro]
        if len(sub):
            break
    adv.append(
        f"La cohorte no está en el corte: originación imputada con la mediana de {len(sub)} "
        "cohortes parecidas (o envíala en `originacion`)"
    )
    return {k: float(sub[k].median()) for k in ORIGINACION} | {"MOB_ORIGEN": 0}, "imputada"


def _observado_solicitud(sol: SolicitudScore, objetivos: dict[str, Any]) -> dict[str, float]:
    """Indicadores que el `observado` permite calcular: numerador y denominador > 0 (config)."""
    out = {}
    for obj, meta in objetivos.items():
        num = getattr(sol.observado, meta["numerador"], None)
        den = getattr(sol.observado, meta["denominador"], None)
        if num is not None and den:
            out[obj] = num / den
    return out


def _banda(q: np.ndarray) -> dict[str, float]:
    return dict(zip(Q, map(float, q), strict=True))


def _escalar(nivel: str, minimo: str) -> str:
    return NIVELES[max(NIVELES.index(nivel), NIVELES.index(minimo))]


@router.post("/score-cohorte", response_model=esquemas.RespuestaScore)
def score_cohorte(sol: SolicitudScore, e: ModeloDep) -> dict[str, Any]:
    """Curva esperada de NCL, Over30 y margen de una cohorte, con desviación, drivers y M4.

    La desviación se calcula con el `observado` de la solicitud o, si no viene y la cohorte está
    en el corte, con su último MOB observado. Si el observado sale de la banda del lado malo
    (arriba en las tasas, abajo en el margen), el nivel de riesgo sube a `alto`, o a `critico`
    si la desviación pasa de 2.5.
    """
    mpdc = e.mpdc
    adv: list[str] = []
    coh = sol.cohorte.model_dump()
    llave = tuple(coh[c] for c in LLAVE_COHORTE)
    i = e.indice.get(llave)
    if e.dictamen_e1 == "no apto":
        adv.append(f"El corte {e.corte} tiene dictamen 'no apto' en la validación E1")
    for col, vistos in mpdc.niveles.items():
        if col in coh and coh[col] not in vistos:
            adv.append(f"{col}='{coh[col]}' no se vio al entrenar (se trata como desconocido)")
    orig, fuente_orig = _originacion(e, sol, i, adv)

    obs_corte = e.observados_de(i) if i is not None else {}
    mob_obs, valores, fuente_obs = None, {}, None
    if sol.observado is not None:
        mob_obs, valores = sol.observado.MOB, _observado_solicitud(sol, mpdc.meta["objetivos"])
        fuente_obs = "solicitud"
        if not valores:
            adv.append("`observado` no trae ningún par numerador/denominador completo")
    elif i is not None:
        mob_obs, fuente_obs = int(e.originacion["MOB_ULTIMO"].iat[i]), "corte"
        valores = {o: v[mob_obs] for o, v in obs_corte.items() if mob_obs in v}

    h = int(mpdc.meta["horizonte_ranking_mob"])
    mobs = sol.mobs or list(range(max(h, mob_obs or 0) + 1))
    todos = sorted({*mobs, h, *([] if mob_obs is None else [mob_obs])})
    pos = {m: k for k, m in enumerate(todos)}
    tabla = tabla_desde_cohortes(pd.DataFrame([{**coh, "MOB": m, **orig} for m in todos]))
    if tabla["SCORE_ORD"].isna().any():
        adv.append(f"SCORE_TAG='{coh['SCORE_TAG']}' no está en el catálogo de su familia")
    pred = mpdc.predecir(tabla)

    def curva(obj: str) -> list[dict[str, Any]]:
        p, o = pred[obj], obs_corte.get(obj, {})
        return [{"MOB": m, **_banda(p[pos[m]]), "observado": o.get(m)} for m in mobs]

    ncl_h = pred["tasa_ncl"][pos[h]]
    nivel = mpdc.nivel_riesgo(float(ncl_h[1]))
    desviaciones = []
    for obj, v in valores.items():
        q = pred[obj][pos[mob_obs]]
        d = float(puntuar.desviacion_normalizada(np.array([v]), q[None, :])[0])
        fila = {"indicador": obj, "MOB": mob_obs, "observado": v, **_banda(q)}
        desviaciones.append({**fila, "desviacion": d, "fuera_de_intervalo": v < q[0] or v > q[2]})
        malo = v > q[2] if SENTIDO_MALO.get(obj, 1) > 0 else v < q[0]
        if malo:
            nivel = _escalar(nivel, "critico" if d > DESVIACION_CRITICA else "alto")

    drivers: dict[str, list[dict[str, Any]]] = {}
    if sol.incluir_drivers:
        ref = tabla.iloc[[pos[mob_obs if mob_obs is not None else max(mobs)]]]
        drivers = {obj: mpdc.drivers(ref, obj)[0] for obj in mpdc.objetivos}

    umbrales = mpdc.meta["nivel_riesgo"]["umbrales"]
    orig_json = {k: None if pd.isna(v) else v for k, v in orig.items()}
    return {
        "modelo": mpdc.info(),
        "cohorte": coh,
        "cohorte_en_corte": i is not None,
        "originacion": {"fuente": fuente_orig, **orig_json},
        "curva": curva("tasa_ncl"),
        "curva_over30": curva("tasa_over30"),
        "margen_esperado": curva("margen_por_cuenta"),
        "nivel_riesgo": nivel,
        "ncl_esperada_horizonte": {"MOB": h, **_banda(ncl_h), "umbrales": umbrales},
        "fuente_observado": fuente_obs,
        "desviacion": next((d for d in desviaciones if d["indicador"] == "tasa_ncl"), None),
        "desviaciones": desviaciones,
        "drivers": drivers.get("tasa_ncl", []),
        "drivers_por_indicador": drivers,
        "pronostico_over30_3mob": e.m4.get(llave),
        "advertencias": adv,
    }


def crear_app(estado: Estado | None = None) -> FastAPI:
    """App con el `estado` dado (pruebas) o, si no se da, cargado de las variables al arrancar."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if estado is None:
            app.state.estado = estado_desde_entorno()
        yield

    app = FastAPI(
        title="MPDC — API de la curva esperada por cohorte (Base Cards)",
        version=__version__,
        description="Curva esperada de NCL, Over30 y margen por cohorte y MOB (E2, RN-02).",
        lifespan=lifespan,
    )
    app.state.estado = estado if estado is not None else Estado(error="el modelo aún no carga")
    app.include_router(router)
    return app


app = crear_app()
