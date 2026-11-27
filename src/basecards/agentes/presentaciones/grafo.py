"""Grafo explícito del pipeline E6 de presentaciones.

El orden de negocio lo fija ``StateGraph``. Las herramientas existentes se
inyectan mediante ``PresentacionesDependencies`` para conservar sus contratos,
probar el flujo con mocks y evitar que el LLM controle el enrutamiento.
"""

from __future__ import annotations

import hashlib
import operator
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from basecards.presentaciones.nodos.redactar import RedaccionError, redactar_seccion


class RunContext(TypedDict):
    """Identidad confiable de la ejecución, fuera del estado de negocio."""

    user_id: str
    roles: tuple[str, ...]
    area: str


def _merge_error(
    anterior: dict[str, Any] | None,
    nuevo: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Permite que ramas paralelas devuelvan ``None`` sin conflicto."""
    return nuevo if nuevo is not None else anterior


class GraphState(TypedDict, total=False):
    """Estado serializable del pipeline de presentaciones."""

    skill: dict[str, Any] | str
    cohorte: str
    mob_min: int
    mob_max: int
    datos: str | None
    tabla: str | None
    ruta_pptx: str | None
    ruta_salida: str | None
    ruta_pdf: str | None
    metricas: dict[str, float | int]
    graficas: Annotated[list[dict[str, Any]], operator.add]
    tablas: dict[str, Any]
    secciones: list[dict[str, Any]]
    secciones_reintento: list[dict[str, Any]]
    secciones_redactadas: Annotated[list[dict[str, Any]], operator.add]
    verificacion: dict[str, Any] | None
    aprobado: bool | None
    aprobacion_skill: bool | None
    auditoria_skill: dict[str, Any] | None
    auditoria_publicacion: dict[str, Any] | None
    miniaturas: list[str]
    solicitar_miniaturas: bool
    intentos_redaccion: int
    llm_num_ctx: int
    error: Annotated[dict[str, Any] | None, _merge_error]

    # Claves internas usadas por las cargas de Send.
    grafica_id: str
    seccion_actual: dict[str, Any]
    feedback_redaccion: str | None


Tool = Callable[..., Any]


@dataclass(frozen=True)
class PresentacionesDependencies:
    """Contratos de las herramientas usadas por el grafo.

    Todas las funciones reciben las mismas firmas públicas que las herramientas
    de ``basecards.herramientas``. ``graficas_generar`` todavía no existe en la
    capa base, por lo que se inyecta explícitamente.
    """

    skill_validar: Tool
    metricas_calcular: Tool
    graficas_generar: Tool
    pptx_ensamblar: Tool
    documento_verificar_cifras: Tool
    documento_convertir: Tool
    documento_miniaturas: Tool | None = None
    publicar: Tool | None = None
    llm: Any | None = None
    llm_factory: Callable[[int], Any] | None = None
    sandbox_runner: Any | None = None


def obtener_run_contexto(config: RunnableConfig) -> RunContext:
    """Lee y valida identidad desde ``config['configurable']``."""
    configurable = config.get("configurable", {}) if config else {}
    user_id = configurable.get("user_id")
    roles = configurable.get("roles", ())
    area = configurable.get("area")
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("falta configurable.user_id")
    if isinstance(roles, str):
        roles = (roles,)
    elif isinstance(roles, list):
        roles = tuple(roles)
    if not isinstance(roles, tuple) or not all(isinstance(rol, str) for rol in roles):
        raise ValueError("configurable.roles debe ser una tupla o lista de textos")
    if not isinstance(area, str) or not area.strip():
        raise ValueError("falta configurable.area")
    return {"user_id": user_id, "roles": roles, "area": area}


def _error(codigo: str, mensaje: str, origen: str, *, reintentable: bool = False) -> dict[str, Any]:
    return {
        "codigo": codigo,
        "mensaje": mensaje,
        "origen": origen,
        "reintentable": reintentable,
    }


def _capturar_error(exc: Exception, origen: str) -> dict[str, Any]:
    codigo = getattr(exc, "codigo", "E-GRA-001")
    mensaje = getattr(exc, "mensaje", str(exc))
    return _error(str(codigo), str(mensaje), origen)


def _skill_path(skill: dict[str, Any] | str) -> Path:
    ruta = skill.get("ruta") if isinstance(skill, dict) else skill
    if not isinstance(ruta, str) or not ruta.strip():
        raise ValueError("skill.ruta debe ser una ruta")
    return Path(ruta).expanduser().resolve()


def _skill_dict(skill: dict[str, Any] | str, ruta: Path) -> dict[str, Any]:
    if isinstance(skill, dict):
        return {**skill, "ruta": str(ruta)}
    return {"nombre": ruta.name, "ruta": str(ruta)}


def _resultado_valido(resultado: Any) -> bool:
    return bool(resultado.get("valido")) if isinstance(resultado, dict) else bool(resultado.valido)


def _resultado_dict(resultado: Any) -> dict[str, Any]:
    if isinstance(resultado, dict):
        return dict(resultado)
    if hasattr(resultado, "model_dump"):
        return resultado.model_dump(mode="json")
    if is_dataclass(resultado):
        return asdict(resultado)
    return {"valido": _resultado_valido(resultado)}


def _hash_template(ruta: Path) -> str | None:
    plantilla = ruta / "template.pptx"
    if not plantilla.is_file():
        return None
    return f"sha256:{hashlib.sha256(plantilla.read_bytes()).hexdigest()}"


def _skill_requiere_aprobacion(skill: dict[str, Any], ruta: Path) -> bool:
    if bool(skill.get("requiere_aprobacion")):
        return True
    if skill.get("origen") == "borradores":
        return True
    baseline = skill.get("baseline_aprobada")
    if not isinstance(baseline, dict):
        return False
    if (
        baseline.get("hash_contenido")
        and skill.get("hash_contenido")
        and baseline["hash_contenido"] != skill["hash_contenido"]
    ):
        return True
    hash_template = _hash_template(ruta)
    if baseline.get("hash_template") and hash_template != baseline["hash_template"]:
        return True
    return bool(baseline.get("version") and skill.get("version") != baseline["version"])


def _serializar_verificacion(resultado: Any) -> dict[str, Any]:
    if isinstance(resultado, dict):
        datos = dict(resultado)
    elif hasattr(resultado, "__dict__"):
        datos = dict(resultado.__dict__)
    else:
        datos = {"ok": bool(getattr(resultado, "ok", False))}
    detalle = datos.get("detalle", [])
    serializado = []
    for item in detalle:
        if isinstance(item, dict):
            serializado.append(dict(item))
        elif is_dataclass(item):
            serializado.append(asdict(item))
        else:
            serializado.append(
                {
                    "ubicacion": getattr(item, "ubicacion", ""),
                    "origen": getattr(item, "origen", "texto"),
                    "esperado": getattr(item, "esperado", None),
                    "encontrado": getattr(item, "encontrado", None),
                    "marcador": getattr(item, "marcador", None),
                }
            )
    datos["detalle"] = serializado
    datos["discrepancias"] = int(datos.get("discrepancias", len(serializado)) or len(serializado))
    datos["ok"] = datos["discrepancias"] == 0
    archivo = datos.get("archivo")
    if isinstance(archivo, Path):
        datos["archivo"] = str(archivo)
    return datos


def _metricas_resumen(metricas: dict[str, float | int]) -> dict[str, Any]:
    # El resumen conserva marcadores y valores deterministas, pero no datos del cubo.
    return {str(clave): valor for clave, valor in metricas.items()}


def _secciones_para_redactar(state: GraphState) -> list[dict[str, Any]]:
    reintento = state.get("secciones_reintento") or []
    return reintento or state.get("secciones", [])


def _route_graficas(state: GraphState) -> list[Send]:
    ids = ("volumen_cosecha", "curvas_over30", "curvas_ncl", "fuera_intervalo")
    return [
        Send(
            "generar_grafica",
            {
                "grafica_id": grafica_id,
                "skill": state.get("skill"),
                "cohorte": state.get("cohorte"),
                "metricas": state.get("metricas", {}),
                "datos": state.get("datos"),
            },
        )
        for grafica_id in ids
    ]


def _route_redaccion(state: GraphState) -> list[Send]:
    metricas = _metricas_resumen(state.get("metricas", {}))
    return [
        Send(
            "redactar_seccion",
            {
                "seccion_actual": seccion,
                "metricas": metricas,
                "feedback_redaccion": state.get("feedback_redaccion"),
            },
        )
        for seccion in _secciones_para_redactar(state)
    ]


def _section_id_from_location(ubicacion: str, secciones: list[dict[str, Any]]) -> str | None:
    match = re.search(r"diapositiva\s+(\d+)", ubicacion, re.IGNORECASE)
    if match:
        indice = int(match.group(1)) - 1
        ordenadas = sorted(secciones, key=lambda item: int(item.get("orden", 0)))
        if 0 <= indice < len(ordenadas):
            return str(ordenadas[indice].get("id"))
    return None


def _contenido_desde_redaccion(
    redactadas: list[dict[str, Any]],
    cohorte: str,
) -> dict[str, dict[str, Any]]:
    ultimas: dict[str, dict[str, Any]] = {}
    for item in redactadas:
        identificador = item.get("id")
        if identificador:
            ultimas[str(identificador)] = item

    contenido: dict[str, dict[str, Any]] = {}
    for identificador, item in ultimas.items():
        texto = str(item.get("texto", "")).strip()
        if identificador == "portada":
            contenido[identificador] = {"titulo": texto, "subtitulo": f"Corte {cohorte}"}
        elif identificador == "resumen_ejecutivo":
            contenido[identificador] = {"titulo": "Resumen ejecutivo", "vinetas": [texto]}
        elif identificador == "siguientes_pasos":
            contenido[identificador] = {"titulo": "Siguientes pasos", "vinetas": [texto]}
        else:
            contenido[identificador] = {"titulo": texto}
    return contenido


def _graficas_y_tablas(state: GraphState) -> tuple[dict[str, Any], dict[str, Any]]:
    graficas: dict[str, Any] = {}
    tablas = dict(state.get("tablas", {}))
    for resultado in state.get("graficas", []):
        identificador = resultado.get("grafica_id")
        datos = resultado.get("datos")
        if not identificador:
            continue
        if isinstance(datos, dict) and "tabla" in datos:
            tablas[str(identificador)] = datos["tabla"]
        else:
            graficas[str(identificador)] = datos
    return graficas, tablas


def _default_publicar(origen: str, destino: str) -> str:
    fuente = Path(origen).expanduser().resolve()
    salida = Path(destino).expanduser().resolve()
    if not fuente.is_file():
        raise FileNotFoundError(f"no existe el borrador {fuente}")
    salida.parent.mkdir(parents=True, exist_ok=True)
    fuente.replace(salida)
    return str(salida)


def _route_after_verificacion(state: GraphState) -> str:
    verificacion = state.get("verificacion") or {}
    discrepancias = int(verificacion.get("discrepancias", 0) or 0)
    if discrepancias == 0:
        return "preparar_revision"
    detalle = verificacion.get("detalle", [])
    origenes = {str(item.get("origen", "")) for item in detalle if isinstance(item, dict)}
    if origenes and origenes <= {"texto"} and state.get("intentos_redaccion", 0) < 2:
        return "preparar_reintento"
    return "error"


def construir_grafo(
    dependencias: PresentacionesDependencies,
    *,
    checkpointer: Any = None,
    llm_num_ctx: int = 16384,
) -> Any:
    """Construye y compila el grafo de presentaciones.

    El checkpointer se inyecta para que producción use Postgres y las pruebas
    usen ``InMemorySaver`` sin cambiar el grafo.
    """

    def validar_skill(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        try:
            ruta = _skill_path(state["skill"])
            resultado = dependencias.skill_validar(ruta)
            resultado_serializado = _resultado_dict(resultado)
            if not _resultado_valido(resultado):
                hallazgos = resultado_serializado.get("errores", [])
                mensaje = f"la skill no valida: {hallazgos}"
                return {"error": _error("E-SKL-001", mensaje, "validar_skill")}
            skill = _skill_dict(state["skill"], ruta)
            skill.update(
                {
                    "version": resultado_serializado.get("version"),
                    "validacion": resultado_serializado,
                    "requiere_aprobacion": _skill_requiere_aprobacion(skill, ruta),
                }
            )
            if skill.get("origen") == "borradores":
                obtener_run_contexto(config)
            return {"skill": skill, "error": None}
        except Exception as exc:  # noqa: BLE001
            return {"error": _capturar_error(exc, "validar_skill")}

    def ruta_despues_validar(state: GraphState) -> str:
        if state.get("error"):
            return "error"
        if isinstance(state.get("skill"), dict) and state["skill"].get("requiere_aprobacion"):
            return "interrupt_skill"
        return "calcular_metricas"

    def interrupt_skill_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        try:
            contexto = obtener_run_contexto(config)
        except Exception as exc:  # noqa: BLE001
            return {"error": _capturar_error(exc, "interrupt_skill")}
        if "lider_riesgo" not in contexto["roles"]:
            return {
                "error": _error(
                    "E-AUT-001",
                    "la aprobación de una skill requiere el rol lider_riesgo",
                    "interrupt_skill",
                )
            }
        skill = state.get("skill", {})
        payload = {
            "tipo": "aprobacion_skill",
            "skill": skill.get("nombre", _skill_path(skill).name)
            if isinstance(skill, dict)
            else _skill_path(skill).name,
            "diff": skill.get("diff", {}) if isinstance(skill, dict) else {},
            "version_propuesta": skill.get("version") if isinstance(skill, dict) else None,
            "deck_ejemplo": skill.get("deck_ejemplo") if isinstance(skill, dict) else None,
            "requiere_rol": "lider_riesgo",
        }
        decision = interrupt(payload)
        if not isinstance(decision, dict) or not isinstance(decision.get("aprobado"), bool):
            return {
                "error": _error(
                    "E-AUT-002",
                    "la respuesta de aprobación de skill no tiene formato válido",
                    "interrupt_skill",
                )
            }
        actor = decision.get("analista_id")
        if actor != contexto["user_id"]:
            return {
                "error": _error(
                    "E-AUT-003",
                    "analista_id no coincide con la identidad de la ejecución",
                    "interrupt_skill",
                )
            }
        return {
            "aprobacion_skill": decision["aprobado"],
            "auditoria_skill": {
                "analista_id": actor,
                "comentarios": decision.get("comentarios"),
                "timestamp": decision.get("timestamp"),
                "aprobado": decision["aprobado"],
            },
            "error": None,
        }

    def ruta_despues_skill(state: GraphState) -> str:
        if state.get("error"):
            return "error"
        return "calcular_metricas" if state.get("aprobacion_skill") else "fin"

    def calcular_metricas(state: GraphState) -> dict[str, Any]:
        try:
            ruta = _skill_path(state["skill"])
            metricas = dependencias.metricas_calcular(
                ruta,
                state["cohorte"],
                state["mob_min"],
                state["mob_max"],
                datos=state.get("datos"),
                tabla=state.get("tabla"),
            )
            return {"metricas": dict(metricas), "error": None}
        except Exception as exc:  # noqa: BLE001
            return {"error": _capturar_error(exc, "calcular_metricas")}

    def generar_grafica(state: GraphState) -> dict[str, Any]:
        try:
            ruta = _skill_path(state["skill"])
            grafica_id = state["grafica_id"]
            datos = dependencias.graficas_generar(
                skill_ruta=ruta,
                grafica_id=grafica_id,
                cohorte=state["cohorte"],
                metricas=state.get("metricas", {}),
                datos=state.get("datos"),
            )
            return {"graficas": [{"grafica_id": grafica_id, "datos": datos}], "error": None}
        except Exception as exc:  # noqa: BLE001
            return {"error": _capturar_error(exc, "generar_graficas")}

    def preparar_secciones(state: GraphState) -> dict[str, Any]:
        try:
            ruta = _skill_path(state["skill"])
            texto = (ruta / "SKILL.md").read_text(encoding="utf-8")
            dentro = False
            secciones: list[dict[str, Any]] = []
            for linea in texto.splitlines():
                limpia = linea.strip()
                if limpia.lower().startswith("## secciones"):
                    dentro = True
                    continue
                if dentro and limpia.startswith("## "):
                    break
                if not dentro:
                    continue
                match = re.match(r"^(\d+)\.\s+(.+)$", limpia)
                if match:
                    objetivo = match.group(2).strip()
                    recurso = re.search(r"`([a-z_][a-z0-9_]*)`", objetivo)
                    identificador = (
                        recurso.group(1)
                        if recurso
                        else ("portada" if match.group(1) == "1" else f"seccion_{match.group(1)}")
                    )
                    secciones.append(
                        {
                            "id": identificador,
                            "orden": int(match.group(1)),
                            "objetivo": objetivo,
                            "recurso": recurso.group(1) if recurso else None,
                        }
                    )
            if not secciones:
                raise ValueError("SKILL.md no declara secciones")
            return {"secciones": secciones, "secciones_reintento": [], "error": None}
        except Exception as exc:  # noqa: BLE001
            return {"error": _capturar_error(exc, "preparar_secciones")}

    def redactar_worker(state: GraphState) -> dict[str, Any]:
        seccion = state.get("seccion_actual", {})
        try:
            if dependencias.llm_factory is not None:
                llm = dependencias.llm_factory(state.get("llm_num_ctx", llm_num_ctx))
            else:
                llm = dependencias.llm
            if llm is None:
                raise RuntimeError("no se inyectó un LLM para redactar_seccion")
            texto = redactar_seccion(
                str(seccion.get("objetivo", "")),
                state.get("metricas", {}),
                llm=llm,
                num_ctx=state.get("llm_num_ctx", llm_num_ctx),
            )
            return {
                "secciones_redactadas": [{"id": seccion.get("id"), "texto": texto}],
                "error": None,
            }
        except RedaccionError as exc:
            return {"error": _error(exc.codigo, exc.mensaje, "redactar_seccion")}
        except Exception as exc:  # noqa: BLE001
            return {"error": _capturar_error(exc, "redactar_seccion")}

    def ensamblar_pptx(state: GraphState) -> dict[str, Any]:
        try:
            ruta_skill = _skill_path(state["skill"])
            ruta_pptx = state.get("ruta_pptx")
            if not ruta_pptx:
                raise ValueError("falta ruta_pptx para el borrador")
            graficas, tablas = _graficas_y_tablas(state)
            contenido = _contenido_desde_redaccion(
                state.get("secciones_redactadas", []), state["cohorte"]
            )
            salida = dependencias.pptx_ensamblar(
                ruta_skill,
                ruta_pptx,
                metricas=state.get("metricas", {}),
                contenido=contenido,
                graficas=graficas,
                tablas=tablas,
                corte=state["cohorte"],
            )
            return {"ruta_pptx": str(salida), "tablas": tablas, "error": None}
        except Exception as exc:  # noqa: BLE001
            return {"error": _capturar_error(exc, "ensamblar_pptx")}

    def verificar_cifras(state: GraphState) -> dict[str, Any]:
        try:
            resultado = dependencias.documento_verificar_cifras(
                state["ruta_pptx"], state.get("metricas", {})
            )
            verificacion = _serializar_verificacion(resultado)
            discrepancias = int(verificacion.get("discrepancias", 0) or 0)
            origenes = {
                str(item.get("origen", ""))
                for item in verificacion.get("detalle", [])
                if isinstance(item, dict)
            }
            error = None
            if discrepancias and not (origenes <= {"texto"}):
                error = _error(
                    "E-GRA-002",
                    "hay discrepancias en tabla, celda o gráfica",
                    "verificar_cifras",
                )
            elif discrepancias and state.get("intentos_redaccion", 0) >= 2:
                error = _error(
                    "E-RED-002",
                    "persisten discrepancias de texto después de dos reintentos",
                    "verificar_cifras",
                )
            return {"verificacion": verificacion, "error": error}
        except Exception as exc:  # noqa: BLE001
            return {"error": _capturar_error(exc, "verificar_cifras")}

    def preparar_reintento(state: GraphState) -> dict[str, Any]:
        verificacion = state.get("verificacion") or {}
        ids: set[str] = set()
        for item in verificacion.get("detalle", []):
            if isinstance(item, dict):
                identificador = _section_id_from_location(
                    str(item.get("ubicacion", "")), state.get("secciones", [])
                )
                if identificador:
                    ids.add(identificador)
        objetivo = state.get("secciones", [])
        seleccionadas = [item for item in objetivo if not ids or item.get("id") in ids]
        feedback = "; ".join(
            str(item.get("ubicacion", "")) for item in verificacion.get("detalle", [])
        )
        return {
            "secciones_reintento": seleccionadas,
            "feedback_redaccion": feedback or "revisa las cifras del texto",
            "intentos_redaccion": state.get("intentos_redaccion", 0) + 1,
            "error": None,
        }

    def preparar_revision(state: GraphState) -> dict[str, Any]:
        if not state.get("solicitar_miniaturas") or dependencias.documento_miniaturas is None:
            return {"error": None}
        try:
            miniaturas = dependencias.documento_miniaturas(
                state["ruta_pptx"],
                str(Path(state["ruta_pptx"]).parent / "miniaturas"),
                runner=dependencias.sandbox_runner,
            )
            return {"miniaturas": [str(ruta) for ruta in miniaturas], "error": None}
        except Exception as exc:  # noqa: BLE001
            return {"error": _capturar_error(exc, "preparar_revision")}

    def interrupt_publicacion(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        try:
            contexto = obtener_run_contexto(config)
        except Exception as exc:  # noqa: BLE001
            return {"error": _capturar_error(exc, "interrupt_publicacion")}
        if "analista" not in contexto["roles"]:
            return {
                "error": _error(
                    "E-AUT-001",
                    "la publicación requiere el rol analista",
                    "interrupt_publicacion",
                )
            }
        payload = {
            "tipo": "aprobacion_publicacion",
            "ruta": state.get("ruta_pptx"),
            "formato": "pptx",
            "verificacion": state.get("verificacion"),
            "miniaturas": state.get("miniaturas", []),
            "requiere_rol": "analista",
        }
        decision = interrupt(payload)
        if not isinstance(decision, dict) or not isinstance(decision.get("aprobado"), bool):
            return {
                "error": _error(
                    "E-AUT-002",
                    "la respuesta de publicación no tiene formato válido",
                    "interrupt_publicacion",
                )
            }
        if decision.get("analista_id") != contexto["user_id"]:
            return {
                "error": _error(
                    "E-AUT-003",
                    "analista_id no coincide con la identidad de la ejecución",
                    "interrupt_publicacion",
                )
            }
        return {
            "aprobado": decision["aprobado"],
            "auditoria_publicacion": {
                "analista_id": decision.get("analista_id"),
                "comentarios": decision.get("comentarios"),
                "timestamp": decision.get("timestamp"),
                "aprobado": decision["aprobado"],
            },
            "error": None,
        }

    def ruta_publicacion(state: GraphState) -> str:
        if state.get("error"):
            return "error"
        return "publicar" if state.get("aprobado") else "fin"

    def publicar(state: GraphState) -> dict[str, Any]:
        try:
            destino = state.get("ruta_salida")
            if not destino:
                raise ValueError("falta ruta_salida para publicar")
            publicar_fn = dependencias.publicar or _default_publicar
            salida = publicar_fn(state["ruta_pptx"], destino)
            return {"ruta_salida": str(salida), "error": None}
        except Exception as exc:  # noqa: BLE001
            return {"error": _capturar_error(exc, "publicar")}

    def convertir_pdf(state: GraphState) -> dict[str, Any]:
        try:
            ruta_pdf = state.get("ruta_pdf") or str(Path(state["ruta_salida"]).with_suffix(".pdf"))
            dependencias.documento_convertir(
                state["ruta_salida"],
                ruta_pdf,
                runner=dependencias.sandbox_runner,
            )
            return {"ruta_pdf": ruta_pdf, "error": None}
        except Exception as exc:  # noqa: BLE001
            return {"error": _capturar_error(exc, "convertir_pdf")}

    def error_node(state: GraphState) -> dict[str, Any]:
        return {"error": state.get("error") or _error("E-GRA-001", "flujo rechazado", "grafo")}

    builder = StateGraph(GraphState)
    builder.add_node("validar_skill", validar_skill)
    builder.add_node("interrupt_skill", interrupt_skill_node)
    builder.add_node("calcular_metricas", calcular_metricas)
    builder.add_node("planificar_graficas", lambda state: {})
    builder.add_node("generar_grafica", generar_grafica)
    builder.add_node("preparar_secciones", preparar_secciones)
    builder.add_node("planificar_redaccion", lambda state: {})
    builder.add_node("redactar_seccion", redactar_worker)
    builder.add_node("ensamblar_pptx", ensamblar_pptx)
    builder.add_node("verificar_cifras", verificar_cifras)
    builder.add_node("preparar_reintento", preparar_reintento)
    builder.add_node("preparar_revision", preparar_revision)
    builder.add_node("interrupt_publicacion", interrupt_publicacion)
    builder.add_node("publicar", publicar)
    builder.add_node("convertir_pdf", convertir_pdf)
    builder.add_node("error", error_node)

    builder.add_edge(START, "validar_skill")
    builder.add_conditional_edges(
        "validar_skill",
        ruta_despues_validar,
        {
            "interrupt_skill": "interrupt_skill",
            "calcular_metricas": "calcular_metricas",
            "error": "error",
        },
    )
    builder.add_conditional_edges(
        "interrupt_skill",
        ruta_despues_skill,
        {"calcular_metricas": "calcular_metricas", "error": "error", "fin": END},
    )
    builder.add_edge("calcular_metricas", "planificar_graficas")
    builder.add_conditional_edges("planificar_graficas", _route_graficas)
    builder.add_edge("generar_grafica", "preparar_secciones")
    builder.add_edge("preparar_secciones", "planificar_redaccion")
    builder.add_conditional_edges("planificar_redaccion", _route_redaccion)
    builder.add_edge("redactar_seccion", "ensamblar_pptx")
    builder.add_edge("ensamblar_pptx", "verificar_cifras")
    builder.add_conditional_edges(
        "verificar_cifras",
        _route_after_verificacion,
        {
            "preparar_revision": "preparar_revision",
            "preparar_reintento": "preparar_reintento",
            "error": "error",
        },
    )
    builder.add_edge("preparar_reintento", "planificar_redaccion")
    builder.add_edge("preparar_revision", "interrupt_publicacion")
    builder.add_conditional_edges(
        "interrupt_publicacion",
        ruta_publicacion,
        {"publicar": "publicar", "error": "error", "fin": END},
    )
    builder.add_edge("publicar", "convertir_pdf")
    builder.add_edge("convertir_pdf", END)
    builder.add_edge("error", END)
    return builder.compile(checkpointer=checkpointer)


# Nombres de conveniencia para consumidores existentes y pruebas.
build_graph = construir_grafo
crear_grafo = construir_grafo

__all__ = [
    "GraphState",
    "PresentacionesDependencies",
    "RunContext",
    "build_graph",
    "construir_grafo",
    "crear_grafo",
    "obtener_run_contexto",
]
