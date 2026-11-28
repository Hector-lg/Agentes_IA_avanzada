"""Ficha del modelo (`model_card.md`) con la estructura de SR 11-7 (PRD §6.4, RN-06).

Se genera desde `bundle.json` al guardar cada versión: propósito, datos, metodología, desempeño
en el backtest, limitaciones y monitoreo.
"""

from __future__ import annotations

from typing import Any


def _tabla_desempeno(r: dict[str, Any]) -> str:
    filas = [
        f"| {obj} | {m['pinball_q05']:.5g} | {m['pinball_q50']:.5g} | {m['pinball_q95']:.5g} | "
        f"{m['mae']:.5g} | {m['rmse']:.5g} | {m['cobertura_cruda']:.1%} | {m['cobertura_cqr']:.1%} | "
        f"{m['mejora_pinball_q50_vs_curva_pct']:+.1f}% | {m['mejora_pinball_q50_vs_arbol_pct']:+.1f}% |"
        for obj, m in r.items()
    ]
    return "\n".join(filas)


def _tabla_familias(r: dict[str, Any]) -> str:
    filas = [
        f"| {obj} | {fam} | {c:.1%} |"
        for obj, m in r.items()
        for fam, c in sorted(m["cobertura_cqr_por_familia"].items())
    ]
    return "\n".join(filas)


def ficha_modelo(meta: dict[str, Any]) -> str:
    r = meta["metricas"]["objetivos"]
    m4 = meta["metricas"].get("m4", {})
    nan = float("nan")
    excl = "\n".join(f"  - {k}: {v:,}" for k, v in meta["datos"]["filas_excluidas"].items())
    spearman = r.get("margen_por_cuenta", {}).get("spearman_estrategia_segmento", nan)
    p = meta["promocion_cfg"]
    return f"""# Ficha del modelo — MPDC v{meta["version"]}

Estructura SR 11-7: propósito, datos, metodología, desempeño, limitaciones y monitoreo.

## Propósito
Modelo de Pronóstico de Desempeño por Cohorte (MPDC). Para cada cohorte (7 dimensiones) y MOB
predice la tasa de pérdida neta (`NCLAMTS / ENRAMTS`), la tasa Over30 (`OVER30ACCTS / ENRACCTS`)
y el margen por cuenta, con intervalo 5–95. Alimenta la regla K-01 (E3), el Copiloto (E5), las
presentaciones (E6) y los dashboards.

## Datos
- Corte: **{meta["corte"]}** · SHA-256 del CSV fuente: `{meta["datos"].get("sha256_csv") or "n/d"}`
- Dictamen de la validación E1: {meta["datos"].get("dictamen_e1") or "sin dictamen"}
- Entrenado con el corte anterior derivado: PERF_MONTH ≤ **{meta["entrenado_hasta"]}**
- Filas del corte: {meta["datos"]["filas_corte"]:,} · de entrenamiento: \
{meta["datos"]["filas_entrenamiento"]:,} · cohortes: {meta["datos"]["cohortes_entrenamiento"]:,}
- Filas excluidas:
{excl}
- Series acumuladas (S-01): `acumulado={str(meta["acumulado"]).lower()}`
- Modo rápido (muestra de cohortes): {"sí" if meta["rapido"] else "no"}

## Metodología
- LightGBM cuantílico (pérdida pinball) en q05, q50 y q95: un modelo por indicador y cuantil (9).
- Pesos: ENRAMTS (NCL), ENRACCTS (Over30) y ACCOUNTS (margen).
- Features: {", ".join(meta["features"])}.
- Sin fuga de información: ninguna feature usa métricas del MOB predicho ni de MOB posteriores.
- Calibración CQR por familia × tramo de MOB {meta["cqr"]["tramos_mob"]} con residuos fuera de tiempo.
- M4: Over30 a 3 MOB con rezagos t, t−1, t−2 y pendiente (LightGBM L1); intervalo empírico.
- Baselines: curva promedio por MOB y familia; árbol de regresión de profundidad 6.

## Desempeño (backtest con origen móvil, orígenes {meta["metricas"]["origenes"]}, horizonte 6 meses)

| Indicador | Pinball q05 | Pinball q50 | Pinball q95 | MAE | RMSE | Cobertura cruda | Cobertura CQR | Mejora vs curva | Mejora vs árbol |
|---|---|---|---|---|---|---|---|---|---|
{_tabla_desempeno(r)}

La cobertura CQR se mide con validación cruzada por origen (se calibra sin el origen evaluado).
Spearman del ranking estrategia × segmento por margen: **{spearman:.3f}**.

Cobertura CQR por familia de estrategia:

| Indicador | Familia | Cobertura |
|---|---|---|
{_tabla_familias(r)}

M4 — Over30 a 3 MOB:
- MAPE agregado (estrategia × cosecha × MOB): {m4.get("mape_agregado_pct", nan):.1f}%
- WAPE por celda: {m4.get("wape_pct", nan):.1f}%
- MAPE por celda (observado > 0): {m4.get("mape_celda_pct", nan):.1f}%

## Promoción
{"Promovido a `champion`" if meta["promocion"]["promovido"] else "No promovido"}: \
{meta["promocion"]["motivo"]}.

## Limitaciones
- Datos sintéticos (S-08): las métricas validan el método, no la cartera real de Banamex.
- Cohortes muy granulares (mediana de 8 cuentas con saldo): el ruido por celda es alto y la banda
  5–95 a nivel celda es ancha; las agregaciones (razón de sumas) son mucho más estables.
- Cosechas jóvenes (DARIA 2.0 solo tiene MOB 0–1): la curva se apoya en lo aprendido de otras
  estrategias, y CQR amplía la banda en el tramo de MOB temprano.
- VINTAGE no es feature categórica para poder pronosticar cosechas nuevas; un cambio de nivel de
  una cosecha completa aparece como desviación (K-01), que es lo que se busca detectar.

## Monitoreo
- Cobertura del intervalo por corte (objetivo 90% ± 5 pp) y deriva de features (PSI por dimensión).
- Reentrenar con cada corte apto; promoción a `champion` solo si mejora la pinball q50 y la
  cobertura queda en {p["cobertura_min"]:.0%}–{p["cobertura_max"]:.0%}.

## Responsable
Integrante 1 — Núcleo de analítica. Generado automáticamente el {meta["creado"]}.
"""
