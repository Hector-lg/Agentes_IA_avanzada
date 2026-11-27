"""Nodo LLM para redactar una sección con marcadores y sin cifras literales."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from langchain_core.messages import HumanMessage, SystemMessage

from basecards.presentaciones.marcadores import (
    contiene_cifras_literales,
    parsear,
)


class LLMInvocable(Protocol):
    """Subconjunto de ChatOllama usado por el nodo y por las pruebas."""

    def invoke(self, messages: list[Any], **kwargs: Any) -> Any: ...


class RedaccionError(Exception):
    """Error de dominio de redacción."""

    codigo = "E-RED-002"

    def __init__(self, mensaje: str) -> None:
        super().__init__(f"{self.codigo}: {mensaje}")
        self.mensaje = mensaje


def _contenido(respuesta: Any) -> str:
    contenido = getattr(respuesta, "content", respuesta)
    if isinstance(contenido, list):
        contenido = "".join(
            parte.get("text", "") if isinstance(parte, Mapping) else str(parte)
            for parte in contenido
        )
    if not isinstance(contenido, str) or not contenido.strip():
        raise RedaccionError("el LLM devolvió una respuesta vacía")
    return contenido.strip()


def _validar(texto: str) -> str | None:
    try:
        parsear(texto)
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    if contiene_cifras_literales(texto):
        return "la respuesta contiene cifras literales fuera de marcadores"
    return None


def _mensajes(
    objetivo: str,
    resumen_metricas: Mapping[str, Any],
    feedback: str | None = None,
) -> list[Any]:
    sistema = (
        "Redacta una sección ejecutiva de Base Cards. Devuelve únicamente texto "
        "en español, sin JSON, sin listas de herramientas y sin cifras de negocio "
        "escritas literalmente. Usa exclusivamente marcadores {{m.*}} para cifras. "
        "Puedes escribir MOB, periodos, fechas y categorías fijas autorizadas."
    )
    solicitud = (
        f"Objetivo de la sección:\n{objetivo}\n\n"
        f"Resumen de métricas disponible:\n{dict(resumen_metricas)}\n\n"
        "Escribe un texto breve, accionable y compatible con una diapositiva."
    )
    if feedback:
        solicitud += (
            f"\nLa respuesta anterior falló este control: {feedback}. Reescribe sin repetirlo."
        )
    return [SystemMessage(content=sistema), HumanMessage(content=solicitud)]


def redactar_seccion(
    objetivo: str,
    resumen_metricas: Mapping[str, Any],
    *,
    llm: LLMInvocable,
    num_ctx: int,
    max_intentos: int = 2,
) -> str:
    """Redacta una sección y reintenta como máximo una vez.

    ``num_ctx`` se envía explícitamente en cada llamada mediante ``options``,
    que es el contrato real de ``ChatOllama._chat_params`` en la versión fijada.
    """
    if max_intentos != 2:
        raise ValueError("RB-04 exige exactamente dos intentos máximos")
    feedback: str | None = None
    for intento in range(max_intentos):
        try:
            respuesta = llm.invoke(
                _mensajes(objetivo, resumen_metricas, feedback),
                options={"num_ctx": num_ctx},
            )
            texto = _contenido(respuesta)
            error = _validar(texto)
            if error is None:
                return texto
            feedback = error
        except RedaccionError as exc:
            feedback = exc.mensaje
        except Exception as exc:  # noqa: BLE001
            feedback = f"fallo controlado del LLM: {type(exc).__name__}: {exc}"
        if intento == max_intentos - 1:
            break
    raise RedaccionError(
        f"la sección no pudo cumplir el contrato tras {max_intentos} intentos: {feedback}"
    )


__all__ = ["LLMInvocable", "RedaccionError", "redactar_seccion"]
