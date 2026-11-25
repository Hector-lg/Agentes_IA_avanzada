from __future__ import annotations

import pytest

from basecards.soporte.sintetico import generar_cubo_sintetico


@pytest.fixture
def cubo_ok():
    return generar_cubo_sintetico(fecha_corte=202609, mobs_por_cohorte=3)
