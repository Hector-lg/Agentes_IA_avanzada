"""Compatibilidad con el import histórico del grafo de presentaciones."""

from basecards.agentes.presentaciones.grafo import (
    GraphState,
    PresentacionesDependencies,
    RunContext,
    build_graph,
    construir_grafo,
    crear_grafo,
    obtener_run_contexto,
)

# Nombre usado en la documentación inicial del repositorio.
loop_presentaciones = construir_grafo

__all__ = [
    "GraphState",
    "PresentacionesDependencies",
    "RunContext",
    "build_graph",
    "construir_grafo",
    "crear_grafo",
    "loop_presentaciones",
    "obtener_run_contexto",
]
