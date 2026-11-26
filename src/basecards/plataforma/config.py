"""Configuración de la plataforma por perfil de hardware.

Lee las variables del Anexo B del Manual Técnico (``PERFIL``, ``LLM_*``,
``OLLAMA_*``, ``MAX_PASOS_REACT``, ``CONFIANZA_MIN``, ``LLM_CTX_ALERTA``,
``LLM_MAX_CONCURRENCY``, URLs de DB/Redis y OIDC) y resuelve los valores por
defecto a partir del perfil S/M/L (PRD §8.1, Manual §4.1).

Reglas aplicadas:

- RP-01: LLM local con Ollama o vLLM, configurable por perfil.
- RNF-09: reproducibilidad; los defaults quedan fijados en la tabla de perfiles.
- Cambio de modelo o de perfil = configuración (``.env``), no código (PRD §8.1).
- Los secretos (``*_PASSWORD``, ``*_SECRET``) usan ``SecretStr`` y nunca
  aparecen en ``repr`` ni en logs.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from typing import Any, ClassVar

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Perfil(str, Enum):
    """Perfil de hardware. Determina modelo, contexto y concurrencia."""

    S = "S"
    M = "M"
    L = "L"


class PerfilHardware:
    """Valores por defecto de cada perfil (PRD §8.1, Manual §4.1).

    Solo cubre lo que cambia con el hardware: modelo principal/rápido,
    embeddings, ventana de contexto y concurrencia del LLM. El resto de las
    variables tiene un único valor por omisión en ``Settings``.
    """

    #: proveedor de inferencia: Ollama en S/M, vLLM en L.
    PROVEEDOR: ClassVar[dict[Perfil, str]] = {
        Perfil.S: "ollama",
        Perfil.M: "ollama",
        Perfil.L: "vllm",
    }
    #: modelo principal (diagnóstico, planificación ReAct, narrativa).
    MODELO_MAIN: ClassVar[dict[Perfil, str]] = {
        Perfil.S: "qwen3.5:9b",
        Perfil.M: "qwen3.8:27b",
        Perfil.L: "gpt-oss:120b",
    }
    #: modelo rápido (clasificar intención, extraer campos, resumir). En el
    #: perfil S se usa un solo modelo para todo (Manual §4.12).
    MODELO_FAST: ClassVar[dict[Perfil, str]] = {
        Perfil.S: "qwen3.5:9b",
        Perfil.M: "qwen3.8:27b",
        Perfil.L: "gpt-oss:120b",
    }
    #: modelo de embeddings.
    EMBED_MODEL: ClassVar[dict[Perfil, str]] = {
        Perfil.S: "qwen3-embedding:0.6b",
        Perfil.M: "qwen3-embedding:0.6b",
        Perfil.L: "qwen3-embedding:8b",
    }
    #: ventana de contexto por petición (tokens), enviada como ``num_ctx``.
    NUM_CTX: ClassVar[dict[Perfil, int]] = {
        Perfil.S: 16384,
        Perfil.M: 32768,
        Perfil.L: 65536,
    }
    #: peticiones simultáneas al LLM (semáforo global = OLLAMA_NUM_PARALLEL).
    MAX_CONCURRENCY: ClassVar[dict[Perfil, int]] = {
        Perfil.S: 1,
        Perfil.M: 4,
        Perfil.L: 4,
    }
    #: modelos cargados a la vez en Ollama. En S solo cabe uno (los embeddings se
    #: cargan cuando se necesitan); en M y L conviven principal y embeddings.
    MAX_LOADED_MODELS: ClassVar[dict[Perfil, int]] = {
        Perfil.S: 1,
        Perfil.M: 2,
        Perfil.L: 2,
    }

    @classmethod
    def defaults(cls, perfil: Perfil) -> dict[str, Any]:
        """Devuelve los valores por defecto del perfil dado."""
        return {
            "llm_provider": cls.PROVEEDOR[perfil],
            "llm_model_main": cls.MODELO_MAIN[perfil],
            "llm_model_fast": cls.MODELO_FAST[perfil],
            "embed_model": cls.EMBED_MODEL[perfil],
            "llm_num_ctx": cls.NUM_CTX[perfil],
            "ollama_context_length": cls.NUM_CTX[perfil],
            "llm_max_concurrency": cls.MAX_CONCURRENCY[perfil],
            "ollama_num_parallel": cls.MAX_CONCURRENCY[perfil],
            "ollama_max_loaded_models": cls.MAX_LOADED_MODELS[perfil],
        }


class Settings(BaseSettings):
    """Configuración de la plataforma leída del entorno y de ``.env``.

    Los campos cuyo valor por defecto depende del perfil se dejan en ``None`` y
    los rellena ``_aplicar_perfil`` después de leer ``PERFIL``. Así, fijar una
    variable en el entorno siempre gana sobre el valor del perfil.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Perfil y modelos ---
    perfil: Perfil = Perfil.S
    llm_provider: str | None = None
    ollama_base_url: str = "http://ollama:11434"
    llm_model_main: str | None = None
    llm_model_fast: str | None = None
    embed_model: str | None = None
    embed_dim: int = 1024
    llm_max_concurrency: int | None = None

    # --- Ollama ---
    ollama_context_length: int | None = None
    ollama_num_parallel: int | None = None
    ollama_max_loaded_models: int | None = None
    ollama_keep_alive: str = "24h"
    ollama_flash_attention: int = 1
    ollama_kv_cache_type: str = "q8_0"

    # --- Base de datos ---
    postgres_user: str = "bc_admin"
    # Sin credenciales en el código: los secretos vienen de `.env` o del entorno.
    postgres_password: SecretStr = SecretStr("")
    app_db_url: str = "postgresql+psycopg://bc_app@postgres:5432/basecards"
    agent_db_url: str = "postgresql+psycopg://bc_agente@postgres:5432/basecards"
    redis_url: str = "redis://redis:6379/0"

    # --- Identidad (OIDC) ---
    kc_admin: str = "admin"
    kc_admin_password: SecretStr = SecretStr("")
    oidc_issuer: str = "http://localhost:8080/realms/basecards"
    oidc_client_id: str = "basecards-portal"
    oidc_client_secret: SecretStr = SecretStr("")
    streamlit_cookie_secret: SecretStr = SecretStr("")

    # --- Agentes ---
    confianza_min: float = 0.6
    max_pasos_react: int = 8
    llm_num_ctx: int | None = None
    llm_ctx_alerta: float = 0.8

    @model_validator(mode="after")
    def _aplicar_perfil(self) -> Settings:
        """Rellena los campos dependientes del perfil que siguen en ``None``.

        Un valor leído del entorno (ya no es ``None``) se respeta; solo se
        completan los huecos con la tabla del perfil.
        """
        defaults = PerfilHardware.defaults(self.perfil)
        for campo, valor in defaults.items():
            if getattr(self, campo) is None:
                setattr(self, campo, valor)
        return self


@lru_cache(maxsize=1)
def obtener_settings() -> Settings:
    """Devuelve una instancia cacheada de ``Settings``."""
    return Settings()
