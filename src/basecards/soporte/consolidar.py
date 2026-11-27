"""Consolida hallazgos E1 + E3 (Manual §2.6.3)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from basecards.soporte.hallazgos import Hallazgo


def _hallazgo_id(h: Hallazgo, corte: int) -> str:
    if h.regla_id == "K-01":
        payload = {
            "regla": h.regla_id,
            "corte": corte,
            "cohorte": h.detalle.get("cohorte", {}),
            "indicador": h.detalle.get("indicador"),
        }
    else:
        payload = {"regla": h.regla_id, "corte": corte, "descripcion": h.descripcion}
    digest = hashlib.sha1(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:12]
    return f"{h.regla_id}-{corte}-{digest}"


def consolidar_hallazgos(
    hallazgos: list[Hallazgo],
    *,
    corte: int,
) -> list[Hallazgo]:
    """Agrupa por (regla, corte); K-01 permanece 1 por cohorte+indicador.

    Los hallazgos de E1 ya vienen agrupados por regla. Aquí:
    - se asigna ``hallazgo_id`` estable
    - se deduplican por id
    - K-01 no se fusiona con R/P/N/C
    """
    por_id: dict[str, Hallazgo] = {}
    for h in hallazgos:
        hid = _hallazgo_id(h, corte)
        detalle = dict(h.detalle)
        detalle["hallazgo_id"] = hid
        detalle["corte"] = corte
        nuevo = Hallazgo(
            regla_id=h.regla_id,
            severidad=h.severidad,
            descripcion=h.descripcion,
            filas_afectadas=h.filas_afectadas,
            total_filas=h.total_filas,
            pct_filas=h.pct_filas,
            monto_involucrado=h.monto_involucrado,
            muestra=list(h.muestra),
            detalle=detalle,
        )
        if hid in por_id and h.regla_id != "K-01":
            # Fusionar conteos si la misma regla apareció dos veces
            prev = por_id[hid]
            filas = prev.filas_afectadas + nuevo.filas_afectadas
            por_id[hid] = Hallazgo(
                regla_id=prev.regla_id,
                severidad=prev.severidad,
                descripcion=prev.descripcion,
                filas_afectadas=filas,
                total_filas=max(prev.total_filas, nuevo.total_filas),
                pct_filas=round(100.0 * filas / max(prev.total_filas, nuevo.total_filas, 1), 4),
                monto_involucrado=prev.monto_involucrado + nuevo.monto_involucrado,
                muestra=(prev.muestra + nuevo.muestra)[:20],
                detalle=prev.detalle,
            )
        else:
            por_id[hid] = nuevo
    return list(por_id.values())


def resumen_consolidado(hallazgos: list[Hallazgo]) -> list[dict[str, Any]]:
    return [
        {
            "hallazgo_id": h.detalle.get("hallazgo_id"),
            "regla": h.regla_id,
            "severidad": h.severidad,
            "filas": h.filas_afectadas,
            "descripcion": h.descripcion,
        }
        for h in hallazgos
    ]
