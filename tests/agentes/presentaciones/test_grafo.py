from __future__ import annotations

import shutil
import threading
from pathlib import Path
from types import SimpleNamespace

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from basecards.agentes.presentaciones.grafo import (
    PresentacionesDependencies,
    construir_grafo,
)

RAIZ_REPO = Path(__file__).resolve().parents[3]
SKILL = RAIZ_REPO / "skills" / "aprobadas" / "seguimiento-cosechas"


class LLMFalso:
    def invoke(self, messages, **kwargs):
        return SimpleNamespace(content="Hallazgo ejecutivo accionable.")


def _config(*roles: str, thread_id: str = "hilo-1") -> dict:
    return {
        "configurable": {
            "thread_id": thread_id,
            "user_id": "analista-1",
            "roles": list(roles),
            "area": "data-science-cards",
        },
        "max_concurrency": 4,
    }


def _dependencias(tmp_path: Path, *, verificacion=None, llamadas=None):
    metricas = {
        "{{m.tasa_ncl.202401.MOB12|pct2}}": 0.0234,
        "{{m.volumen.202401.MOB0|int}}": 1200,
    }
    barrera = threading.Barrier(2)

    def validar(_ruta):
        return SimpleNamespace(valido=True, version="1.0.0", errores=[], advertencias=[])

    def calcular(*args, **kwargs):
        return metricas

    def graficas_generar(**kwargs):
        grafica_id = kwargs["grafica_id"]
        if grafica_id in {"curvas_over30", "curvas_ncl"}:
            barrera.wait(timeout=2)
        if llamadas is not None:
            llamadas.append(grafica_id)
        if grafica_id == "fuera_intervalo":
            return {"tabla": {"columnas": ["VINTAGE"], "filas": [[202401]]}}
        return {"categorias": ["202401"], "series": {"Observado": [1.0]}}

    def ensamblar(_skill, salida, **kwargs):
        ruta = Path(salida)
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_bytes(b"pptx-falso")
        return ruta

    def verificar(_archivo, _metricas):
        if verificacion is not None:
            return verificacion
        return SimpleNamespace(
            archivo=Path(_archivo),
            discrepancias=0,
            detalle=[],
            cifras_encontradas=0,
            cifras_esperadas=2,
        )

    def publicar(origen, destino):
        Path(destino).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(origen, destino)
        return str(destino)

    def convertir(origen, destino, **kwargs):
        Path(destino).write_bytes(b"%PDF-falso")

    return PresentacionesDependencies(
        skill_validar=validar,
        metricas_calcular=calcular,
        graficas_generar=graficas_generar,
        pptx_ensamblar=ensamblar,
        documento_verificar_cifras=verificar,
        documento_convertir=convertir,
        publicar=publicar,
        llm=LLMFalso(),
    )


def _entrada(tmp_path: Path, *, skill: dict | None = None) -> dict:
    return {
        "skill": skill or {"ruta": str(SKILL), "origen": "aprobadas"},
        "cohorte": "202401",
        "mob_min": 0,
        "mob_max": 12,
        "datos": str(tmp_path / "datos.parquet"),
        "ruta_pptx": str(tmp_path / "borrador.pptx"),
        "ruta_salida": str(tmp_path / "publicado.pptx"),
        "ruta_pdf": str(tmp_path / "publicado.pdf"),
        "llm_num_ctx": 16384,
    }


def test_flujo_feliz_pausa_y_reanuda_publicacion(tmp_path: Path) -> None:
    deps = _dependencias(tmp_path)
    graph = construir_grafo(deps, checkpointer=InMemorySaver())
    config = _config("analista")

    pausado = graph.invoke(_entrada(tmp_path), config=config)

    assert "__interrupt__" in pausado
    assert not (tmp_path / "publicado.pptx").exists()

    terminado = graph.invoke(
        Command(
            resume={
                "aprobado": True,
                "analista_id": "analista-1",
                "comentarios": "Conforme",
                "timestamp": "2026-11-27T12:00:00Z",
            }
        ),
        config=config,
    )

    assert terminado["ruta_salida"].endswith("publicado.pptx")
    assert terminado["ruta_pdf"].endswith("publicado.pdf")
    assert (tmp_path / "publicado.pptx").is_file()
    assert (tmp_path / "publicado.pdf").is_file()


def test_rechazo_publicacion_no_publica_ni_convierte(tmp_path: Path) -> None:
    deps = _dependencias(tmp_path)
    graph = construir_grafo(deps, checkpointer=InMemorySaver())
    config = _config("analista", thread_id="hilo-rechazo")

    graph.invoke(_entrada(tmp_path), config=config)
    resultado = graph.invoke(
        Command(
            resume={
                "aprobado": False,
                "analista_id": "analista-1",
                "comentarios": "Revisar",
                "timestamp": "2026-11-27T12:00:00Z",
            }
        ),
        config=config,
    )

    assert resultado["aprobado"] is False
    assert not (tmp_path / "publicado.pptx").exists()
    assert not (tmp_path / "publicado.pdf").exists()


def test_skill_nueva_pausa_antes_de_metricas(tmp_path: Path) -> None:
    metricas_llamadas = []
    deps = _dependencias(tmp_path)
    original = deps.metricas_calcular
    deps = PresentacionesDependencies(
        **{
            **deps.__dict__,
            "metricas_calcular": lambda *args, **kwargs: (
                metricas_llamadas.append(1) or original(*args, **kwargs)
            ),
        }
    )
    graph = construir_grafo(deps, checkpointer=InMemorySaver())
    config = _config("lider_riesgo", "analista", thread_id="hilo-skill")
    skill = {"ruta": str(SKILL), "origen": "borradores", "version": "1.1.0"}

    pausado = graph.invoke(_entrada(tmp_path, skill=skill), config=config)

    assert "__interrupt__" in pausado
    assert metricas_llamadas == []


def test_discrepancia_de_tabla_termina_antes_de_publicar(tmp_path: Path) -> None:
    verificacion = SimpleNamespace(
        archivo=tmp_path / "borrador.pptx",
        discrepancias=1,
        detalle=[
            SimpleNamespace(
                ubicacion="diapositiva 6, tabla, fila 1",
                origen="tabla",
                esperado=1,
                encontrado=2,
                marcador=None,
            )
        ],
    )
    deps = _dependencias(tmp_path, verificacion=verificacion)
    graph = construir_grafo(deps, checkpointer=InMemorySaver())

    resultado = graph.invoke(_entrada(tmp_path), config=_config("analista", thread_id="hilo-error"))

    assert resultado["error"]["origen"] == "verificar_cifras"
    assert not (tmp_path / "publicado.pptx").exists()


def test_send_lanza_over30_y_ncl_en_las_ramas_de_graficas(tmp_path: Path) -> None:
    llamadas: list[str] = []
    deps = _dependencias(tmp_path, llamadas=llamadas)
    graph = construir_grafo(deps, checkpointer=InMemorySaver())

    graph.invoke(_entrada(tmp_path), config=_config("analista", thread_id="hilo-send"))

    assert {"curvas_over30", "curvas_ncl"} <= set(llamadas)
    assert len(llamadas) == 4


def test_estado_no_contiene_identidad(tmp_path: Path) -> None:
    deps = _dependencias(tmp_path)
    graph = construir_grafo(deps, checkpointer=InMemorySaver())

    pausado = graph.invoke(
        _entrada(tmp_path), config=_config("analista", thread_id="hilo-identidad")
    )
    estado = pausado["__interrupt__"][0].value

    assert "user_id" not in estado
    assert "roles" not in estado
    assert estado["tipo"] == "aprobacion_publicacion"
