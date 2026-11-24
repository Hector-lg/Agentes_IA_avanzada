"""Pipeline E1: carga → normalización N-01 → esquema → reglas → anomalías → dictamen."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from basecards.soporte.carga import (
    cargar_csv,
    castear_tipos,
    corte_anterior_derivado,
    normalizar_catalogos,
)
from basecards.soporte.config import cargar_reglas
from basecards.soporte.hallazgos import Hallazgo, es_critico
from basecards.soporte.reglas import evaluar_c, evaluar_esquema, evaluar_p, evaluar_r


@dataclass
class ResultadoE1:
    fecha_corte: int
    dictamen: str  # "apto" | "no apto"
    filas: int
    filas_normalizadas_n01: int
    hallazgos: list[Hallazgo] = field(default_factory=list)
    detalle: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fecha_corte": self.fecha_corte,
            "dictamen": self.dictamen,
            "filas": self.filas,
            "filas_normalizadas_n01": self.filas_normalizadas_n01,
            "hallazgos": [h.to_dict() for h in self.hallazgos],
            "detalle": self.detalle,
        }


def _dictamen(hallazgos: list[Hallazgo], reglas: dict[str, Any]) -> str:
    criticas_esquema = set(reglas.get("criticas_esquema", []))
    for h in hallazgos:
        if h.regla_id in criticas_esquema:
            return "no apto"
        if es_critico(h.severidad) and h.regla_id.startswith("R-"):
            return "no apto"
    return "apto"


def validar_dataframe(
    df: pd.DataFrame,
    fecha_corte: int | None = None,
    *,
    df_anterior: pd.DataFrame | None = None,
    derivar_corte_anterior: bool = True,
) -> ResultadoE1:
    """Valida un DataFrame ya cargado (útil en tests)."""
    reglas = cargar_reglas()
    corte = int(fecha_corte or reglas.get("corte_default", 202609))

    df_norm, n_n01 = normalizar_catalogos(df)
    # N-01 se registra como info si hubo filas tocadas
    hallazgos: list[Hallazgo] = []
    if n_n01 > 0:
        hallazgos.append(
            Hallazgo(
                regla_id="N-01",
                severidad=reglas["severidades"]["N-01"],
                descripcion="Se recortaron espacios finales en columnas de catálogo",
                filas_afectadas=n_n01,
                total_filas=len(df_norm),
                pct_filas=round(100.0 * n_n01 / max(len(df_norm), 1), 4),
                detalle={"accion": "strip"},
            )
        )

    # Esquema sobre datos aún en string/parcialmente casteados
    hallazgos_esquema = evaluar_esquema(df_norm, reglas)
    hallazgos.extend(hallazgos_esquema)
    criticas = set(reglas.get("criticas_esquema", []))
    if any(h.regla_id in criticas for h in hallazgos_esquema):
        return ResultadoE1(
            fecha_corte=corte,
            dictamen="no apto",
            filas=len(df_norm),
            filas_normalizadas_n01=n_n01,
            hallazgos=hallazgos,
            detalle={"etapa": "esquema"},
        )

    tipado = castear_tipos(df_norm, reglas)
    hallazgos.extend(evaluar_r(tipado, corte, reglas, incluir_esquema=False))
    hallazgos.extend(evaluar_p(tipado, reglas))

    anterior = df_anterior
    if anterior is None and derivar_corte_anterior:
        anterior = corte_anterior_derivado(tipado, corte)
    hallazgos.extend(evaluar_c(tipado, anterior, reglas))

    return ResultadoE1(
        fecha_corte=corte,
        dictamen=_dictamen(hallazgos, reglas),
        filas=len(tipado),
        filas_normalizadas_n01=n_n01,
        hallazgos=hallazgos,
        detalle={"etapa": "completo", "reglas_disparadas": sorted({h.regla_id for h in hallazgos})},
    )


def validar_corte(
    ruta_csv: str | Path,
    fecha_corte: int | None = None,
    *,
    derivar_corte_anterior: bool = True,
) -> ResultadoE1:
    """Punto de entrada E1: CSV → hallazgos + dictamen preliminar."""
    df = cargar_csv(ruta_csv)
    return validar_dataframe(
        df,
        fecha_corte=fecha_corte,
        derivar_corte_anterior=derivar_corte_anterior,
    )


def resumen_hallazgos(resultado: ResultadoE1) -> list[dict[str, Any]]:
    """Vista compacta para reportar a maestros / demos."""
    return [
        {
            "regla": h.regla_id,
            "severidad": h.severidad,
            "filas": h.filas_afectadas,
            "pct": h.pct_filas,
            "descripcion": h.descripcion,
        }
        for h in resultado.hallazgos
    ]
