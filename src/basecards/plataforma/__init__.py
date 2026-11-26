"""Plataforma común (E0): configuración, cliente LLM, loop ReAct y seguridad.

Expone la configuración tipada por perfil de hardware. Cambiar de modelo o de
perfil debe ser solo configuración (``.env``), nunca código (PRD §8.1).
"""

from __future__ import annotations

from basecards.plataforma.config import Perfil, PerfilHardware, Settings, obtener_settings

__all__ = ["Perfil", "PerfilHardware", "Settings", "obtener_settings"]
