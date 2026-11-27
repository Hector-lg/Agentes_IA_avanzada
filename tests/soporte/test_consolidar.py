from __future__ import annotations

from basecards.soporte.consolidar import consolidar_hallazgos
from basecards.soporte.hallazgos import Hallazgo
from basecards.soporte.predicciones.mock import generar_predicciones_mock
from basecards.soporte.reglas.k_reglas import evaluar_k01


def _h(regla: str, desc: str = "x") -> Hallazgo:
    return Hallazgo(
        regla_id=regla,
        severidad="media",
        descripcion=desc,
        filas_afectadas=2,
        total_filas=10,
        pct_filas=20.0,
    )


def test_consolidar_asigna_ids_y_no_mezcla_k01():
    pred = generar_predicciones_mock(forzar_fuera=True)
    k01 = evaluar_k01(pred)
    e1 = [_h("R-05"), _h("P-01")]
    out = consolidar_hallazgos([*e1, *k01], corte=202609)
    ids = [h.detalle["hallazgo_id"] for h in out]
    assert len(ids) == len(set(ids))
    assert any(h.regla_id == "K-01" for h in out)
    assert any(h.regla_id == "R-05" for h in out)
    # K-01 ids distintos de R/P
    assert all(h.detalle["hallazgo_id"].startswith("K-01-") for h in out if h.regla_id == "K-01")


def test_consolidar_dedup_misma_regla_e1():
    a = _h("R-14", "NCL roto")
    b = _h("R-14", "NCL roto")
    out = consolidar_hallazgos([a, b], corte=202609)
    assert len([h for h in out if h.regla_id == "R-14"]) == 1
