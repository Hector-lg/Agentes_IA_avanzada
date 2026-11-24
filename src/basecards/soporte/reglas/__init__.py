"""Reglas deterministas R-, P-, C- del Agente de Soporte."""

from basecards.soporte.reglas.c_reglas import evaluar_c
from basecards.soporte.reglas.p_reglas import evaluar_p
from basecards.soporte.reglas.r_reglas import evaluar_esquema, evaluar_r

__all__ = ["evaluar_esquema", "evaluar_r", "evaluar_p", "evaluar_c"]
