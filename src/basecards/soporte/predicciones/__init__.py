"""Carga de predicciones_cohorte (mock hoy; parquet/postgres en Día 6)."""

from basecards.soporte.predicciones.esquema import COLUMNAS_REQUERIDAS, validar_esquema_predicciones
from basecards.soporte.predicciones.loader import cargar_predicciones

__all__ = [
    "COLUMNAS_REQUERIDAS",
    "cargar_predicciones",
    "validar_esquema_predicciones",
]
