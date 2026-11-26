"""Pipeline E3: comparar curva esperada (K-01) + consolidar con E1."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from basecards.soporte.consolidar import consolidar_hallazgos
from basecards.soporte.hallazgos import Hallazgo
from basecards.soporte.pipeline_e1 import ResultadoE1, validar_dataframe
from basecards.soporte.predicciones import cargar_predicciones
from basecards.soporte.reglas.k_reglas import evaluar_k01


@dataclass
class ResultadoE3:
    fecha_corte: int
    dictamen_e1: str
    hallazgos: list[Hallazgo] = field(default_factory=list)
    hallazgos_k01: list[Hallazgo] = field(default_factory=list)
    detalle: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fecha_corte": self.fecha_corte,
            "dictamen_e1": self.dictamen_e1,
            "hallazgos": [h.to_dict() for h in self.hallazgos],
            "hallazgos_k01": [h.to_dict() for h in self.hallazgos_k01],
            "detalle": self.detalle,
        }


def comparar_curva_esperada(
    predicciones: pd.DataFrame | None = None,
    *,
    corte: int = 202609,
    fuente: str | None = None,
) -> list[Hallazgo]:
    """Nodo E3: lee predicciones y genera hallazgos K-01."""
    pred = predicciones if predicciones is not None else cargar_predicciones(corte, fuente=fuente)
    return evaluar_k01(pred)


def validar_corte_e1_e3(
    cubo: pd.DataFrame,
    *,
    fecha_corte: int = 202609,
    predicciones: pd.DataFrame | None = None,
    fuente_predicciones: str = "mock",
    derivar_corte_anterior: bool = False,
) -> ResultadoE3:
    """Corre E1 sobre el cubo y E3 (K-01) sobre predicciones; consolida hallazgos."""
    e1: ResultadoE1 = validar_dataframe(
        cubo,
        fecha_corte=fecha_corte,
        derivar_corte_anterior=derivar_corte_anterior,
    )
    pred = (
        predicciones
        if predicciones is not None
        else cargar_predicciones(fecha_corte, fuente=fuente_predicciones)
    )
    k01 = evaluar_k01(pred)
    consolidados = consolidar_hallazgos([*e1.hallazgos, *k01], corte=fecha_corte)
    return ResultadoE3(
        fecha_corte=fecha_corte,
        dictamen_e1=e1.dictamen,
        hallazgos=consolidados,
        hallazgos_k01=[h for h in consolidados if h.regla_id == "K-01"],
        detalle={
            "n_e1": len(e1.hallazgos),
            "n_k01": len(k01),
            "n_consolidado": len(consolidados),
            "reglas": sorted({h.regla_id for h in consolidados}),
        },
    )
