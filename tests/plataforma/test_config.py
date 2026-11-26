"""Pruebas de ``basecards.plataforma.config`` (issue 01).

Verifican que los valores por defecto salgan del perfil S/M/L (PRD §8.1), que
una variable de entorno gane sobre el perfil y que los secretos no se impriman.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from basecards.plataforma.config import Perfil, PerfilHardware, Settings, obtener_settings

#: variables que puede leer ``Settings``; se limpian para aislar cada prueba.
_VARIABLES = (
    "PERFIL",
    "LLM_PROVIDER",
    "OLLAMA_BASE_URL",
    "LLM_MODEL_MAIN",
    "LLM_MODEL_FAST",
    "EMBED_MODEL",
    "EMBED_DIM",
    "LLM_MAX_CONCURRENCY",
    "OLLAMA_CONTEXT_LENGTH",
    "OLLAMA_NUM_PARALLEL",
    "OLLAMA_MAX_LOADED_MODELS",
    "OLLAMA_KEEP_ALIVE",
    "OLLAMA_FLASH_ATTENTION",
    "OLLAMA_KV_CACHE_TYPE",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "APP_DB_URL",
    "AGENT_DB_URL",
    "REDIS_URL",
    "KC_ADMIN",
    "KC_ADMIN_PASSWORD",
    "OIDC_ISSUER",
    "OIDC_CLIENT_ID",
    "OIDC_CLIENT_SECRET",
    "STREAMLIT_COOKIE_SECRET",
    "CONFIANZA_MIN",
    "MAX_PASOS_REACT",
    "LLM_NUM_CTX",
    "LLM_CTX_ALERTA",
)


@pytest.fixture(autouse=True)
def entorno_limpio(monkeypatch: pytest.MonkeyPatch) -> None:
    """Quita del entorno las variables de configuración y limpia la caché."""
    for nombre in _VARIABLES:
        monkeypatch.delenv(nombre, raising=False)
    obtener_settings.cache_clear()


def _settings(**entorno: str) -> Settings:
    """Crea ``Settings`` ignorando cualquier ``.env`` del disco."""
    return Settings(_env_file=None, **entorno)  # type: ignore[call-arg]


def test_defaults_perfil_s_sin_env() -> None:
    s = _settings()
    assert s.perfil is Perfil.S
    assert s.llm_provider == "ollama"
    assert s.llm_model_main == "qwen3.5:9b"
    assert s.llm_model_fast == "qwen3.5:9b"
    assert s.embed_model == "qwen3-embedding:0.6b"
    assert s.llm_num_ctx == 16384
    assert s.ollama_context_length == 16384
    assert s.llm_max_concurrency == 1
    assert s.ollama_num_parallel == 1
    assert s.ollama_max_loaded_models == 1


def test_defaults_agentes() -> None:
    s = _settings()
    assert s.confianza_min == 0.6
    assert s.max_pasos_react == 8
    assert s.llm_ctx_alerta == 0.8


@pytest.mark.parametrize(
    ("perfil", "modelo", "num_ctx", "concurrencia", "proveedor"),
    [
        ("S", "qwen3.5:9b", 16384, 1, "ollama"),
        ("M", "qwen3.8:27b", 32768, 4, "ollama"),
        ("L", "gpt-oss:120b", 65536, 4, "vllm"),
    ],
)
def test_perfil_cambia_modelo_contexto_y_concurrencia(
    monkeypatch: pytest.MonkeyPatch,
    perfil: str,
    modelo: str,
    num_ctx: int,
    concurrencia: int,
    proveedor: str,
) -> None:
    monkeypatch.setenv("PERFIL", perfil)
    s = _settings()
    assert s.perfil is Perfil(perfil)
    assert s.llm_model_main == modelo
    assert s.llm_model_fast == modelo
    assert s.llm_num_ctx == num_ctx
    assert s.ollama_context_length == num_ctx
    assert s.llm_max_concurrency == concurrencia
    assert s.ollama_num_parallel == concurrencia
    assert s.llm_provider == proveedor


@pytest.mark.parametrize(("perfil", "esperado"), [("S", 1), ("M", 2), ("L", 2)])
def test_perfil_define_modelos_cargados_a_la_vez(
    monkeypatch: pytest.MonkeyPatch, perfil: str, esperado: int
) -> None:
    monkeypatch.setenv("PERFIL", perfil)
    assert _settings().ollama_max_loaded_models == esperado


def test_ollama_max_loaded_models_se_puede_fijar_por_entorno(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PERFIL", "S")
    monkeypatch.setenv("OLLAMA_MAX_LOADED_MODELS", "2")
    assert _settings().ollama_max_loaded_models == 2


def test_perfil_l_usa_embeddings_grandes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERFIL", "L")
    assert _settings().embed_model == "qwen3-embedding:8b"


def test_perfil_es_insensible_a_mayusculas_en_el_nombre_de_la_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("perfil", "M")
    assert _settings().perfil is Perfil.M


def test_perfil_invalido_falla(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERFIL", "XL")
    with pytest.raises(ValueError):
        _settings()


def test_variable_de_entorno_gana_sobre_el_perfil(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERFIL", "M")
    monkeypatch.setenv("LLM_MODEL_MAIN", "qwen3:8b")
    monkeypatch.setenv("LLM_NUM_CTX", "8192")
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "2")
    s = _settings()
    assert s.llm_model_main == "qwen3:8b"
    assert s.llm_num_ctx == 8192
    assert s.llm_max_concurrency == 2
    # lo que no se fijó sigue saliendo del perfil M
    assert s.llm_model_fast == "qwen3.8:27b"
    assert s.ollama_context_length == 32768


def test_cambiar_de_modelo_es_solo_configuracion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MODEL_MAIN", "gpt-oss:20b")
    monkeypatch.setenv("LLM_MODEL_FAST", "gpt-oss:20b")
    s = _settings()
    assert (s.llm_model_main, s.llm_model_fast) == ("gpt-oss:20b", "gpt-oss:20b")


def test_variables_del_anexo_b_se_leen_del_entorno(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
    monkeypatch.setenv("REDIS_URL", "redis://otro:6379/1")
    monkeypatch.setenv("OIDC_ISSUER", "http://kc:8080/realms/x")
    monkeypatch.setenv("CONFIANZA_MIN", "0.75")
    monkeypatch.setenv("MAX_PASOS_REACT", "5")
    monkeypatch.setenv("LLM_CTX_ALERTA", "0.9")
    s = _settings()
    assert s.ollama_base_url == "http://host.docker.internal:11434"
    assert s.redis_url == "redis://otro:6379/1"
    assert s.oidc_issuer == "http://kc:8080/realms/x"
    assert s.confianza_min == 0.75
    assert s.max_pasos_react == 5
    assert s.llm_ctx_alerta == 0.9


def test_variables_desconocidas_se_ignoran(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERFIL", "S")
    s = _settings(TICKETS_MODO="local")
    assert s.perfil is Perfil.S


def test_tabla_de_perfiles_cubre_s_m_l() -> None:
    for tabla in (
        PerfilHardware.PROVEEDOR,
        PerfilHardware.MODELO_MAIN,
        PerfilHardware.MODELO_FAST,
        PerfilHardware.EMBED_MODEL,
        PerfilHardware.NUM_CTX,
        PerfilHardware.MAX_CONCURRENCY,
        PerfilHardware.MAX_LOADED_MODELS,
    ):
        assert set(tabla) == set(Perfil)


def test_ollama_context_length_coincide_con_num_ctx_en_cada_perfil() -> None:
    for perfil in Perfil:
        d = PerfilHardware.defaults(perfil)
        assert d["llm_num_ctx"] == d["ollama_context_length"]
        assert d["llm_max_concurrency"] == d["ollama_num_parallel"]


@pytest.mark.parametrize(
    "variable",
    [
        "POSTGRES_PASSWORD",
        "KC_ADMIN_PASSWORD",
        "OIDC_CLIENT_SECRET",
        "STREAMLIT_COOKIE_SECRET",
    ],
)
def test_secretos_no_aparecen_en_repr_ni_str(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    valor = "super-secreto-xyz-123"
    monkeypatch.setenv(variable, valor)
    s = _settings()
    assert valor not in repr(s)
    assert valor not in str(s)
    assert valor not in repr(getattr(s, variable.lower()))
    # el valor real sigue disponible para quien lo necesite explícitamente
    assert getattr(s, variable.lower()).get_secret_value() == valor


def test_defaults_no_traen_credenciales_en_el_codigo() -> None:
    s = _settings()
    assert s.postgres_password.get_secret_value() == ""
    assert s.kc_admin_password.get_secret_value() == ""
    assert s.oidc_client_secret.get_secret_value() == ""
    assert s.streamlit_cookie_secret.get_secret_value() == ""
    # las URLs no llevan contraseña: el usuario va sin ":clave" antes de "@"
    assert urlsplit(s.app_db_url).password is None
    assert urlsplit(s.agent_db_url).password is None


def test_secretos_por_defecto_no_aparecen_en_repr() -> None:
    texto = repr(_settings())
    assert "cambiar_me" not in texto
    assert "SecretStr('')" in texto


def test_obtener_settings_esta_cacheado() -> None:
    assert obtener_settings() is obtener_settings()
