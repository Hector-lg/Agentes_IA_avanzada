"""Esquemas de la API del MPDC (Manual §5.8). Una solicitud que no los cumple recibe un 422."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

MOB_MAX_API = 60


def _aaaamm(v: int | None) -> int | None:
    if v is not None and (not 200001 <= v <= 209912 or not 1 <= v % 100 <= 12):
        raise ValueError(f"debe ser AAAAMM con mes 01–12 (recibido: {v})")
    return v


def _unicos(v: list[int]) -> list[int]:
    return sorted(set(v))


# Mes en formato AAAAMM (VINTAGE, filtros de cosecha): 202613 o 2026 responden 422
AAAAMM = Annotated[int, AfterValidator(_aaaamm)]
MobValido = Annotated[int, Field(ge=0, le=MOB_MAX_API)]


class Cohorte(BaseModel):
    """Llave de 7 dimensiones de la cohorte, con los valores del diccionario de Banamex."""

    VINTAGE: AAAAMM = Field(examples=[202511])
    STRATEGY: str = Field(examples=["DARIA 1.6"])
    STRATEGY_SEGMENT: str = Field(examples=["LE Thin"])
    PRODUCT: str = Field(examples=["Clasica"])
    CHANNEL: str = Field(examples=["Internet"])
    AGE_TAG: str = Field(examples=["02| Young 25-30"])
    SCORE_TAG: str = Field(examples=["c.160-164"])

    @field_validator("STRATEGY", "STRATEGY_SEGMENT", "PRODUCT", "CHANNEL", "AGE_TAG", "SCORE_TAG")
    @classmethod
    def _n01(cls, v: str) -> str:
        return v.strip()  # N-01: los mismos valores que el Parquet normalizado


class Observado(BaseModel):
    """Métricas de un MOB; cada par numerador/denominador completo da una desviación."""

    MOB: MobValido
    NCLAMTS: float | None = None
    ENRAMTS: float | None = Field(default=None, ge=0)
    OVER30ACCTS: float | None = Field(default=None, ge=0)
    ENRACCTS: float | None = Field(default=None, ge=0)
    MARGEN: float | None = None  # acumulado al MOB: REVENUE − NCL − gastos acumulados
    ACCOUNTS: float | None = Field(default=None, ge=0)


class Originacion(BaseModel):
    """Atributos del MOB 0. Si se omiten, se toman del corte o se imputan con cohortes parecidas."""

    ACCOUNTS: float = Field(gt=0)
    CREDITLINESAMTS: float = Field(gt=0)
    ENRAMTS: float | None = Field(default=None, ge=0)


ListaMob = Annotated[
    list[MobValido], Field(min_length=1, max_length=MOB_MAX_API + 1), AfterValidator(_unicos)
]


class SolicitudScore(BaseModel):
    cohorte: Cohorte
    mobs: ListaMob | None = Field(
        default=None,
        description="MOB a pronosticar; por defecto de 0 al horizonte (12) o al MOB observado",
        examples=[[0, 1, 2, 3, 6, 9, 12]],
    )
    observado: Observado | None = None
    originacion: Originacion | None = None
    incluir_drivers: bool = True


class ModeloInfo(BaseModel):
    nombre: str
    version: int
    alias: str | None
    corte: int
    entrenado_hasta: int


class OriginacionUsada(BaseModel):
    fuente: Literal["solicitud", "corte", "imputada"]
    ACCOUNTS_0: float
    CREDITLINESAMTS_0: float
    ENRAMTS_0: float | None
    MOB_ORIGEN: int


class Banda(BaseModel):
    q05: float
    q50: float
    q95: float


class PuntoCurva(Banda):
    MOB: int
    observado: float | None = None


class BandaHorizonte(Banda):
    MOB: int
    umbrales: dict[str, float]


class Desviacion(Banda):
    indicador: str
    MOB: int
    observado: float
    desviacion: float = Field(description="PRD §6.6: ≥ 0; pasa de 1 si la fila sale de la banda")
    fuera_de_intervalo: bool


class Driver(BaseModel):
    feature: str
    contribucion: float


class PronosticoOver30(Banda):
    MOB_ULTIMO: int
    PERF_MONTH: int
    tasa_over30_ultimo: float
    MOB_OBJETIVO: int
    PERF_MONTH_OBJETIVO: int


class RespuestaScore(BaseModel):
    modelo: ModeloInfo
    cohorte: Cohorte
    cohorte_en_corte: bool
    originacion: OriginacionUsada
    curva: list[PuntoCurva] = Field(description="Tasa NCL esperada por MOB")
    curva_over30: list[PuntoCurva]
    margen_esperado: list[PuntoCurva] = Field(description="Margen por cuenta esperado por MOB")
    nivel_riesgo: Literal["bajo", "medio", "alto", "critico"]
    ncl_esperada_horizonte: BandaHorizonte
    fuente_observado: Literal["solicitud", "corte"] | None = None
    desviacion: Desviacion | None = Field(default=None, description="La de tasa_ncl")
    desviaciones: list[Desviacion] = Field(default_factory=list)
    drivers: list[Driver] = Field(default_factory=list, description="Los de tasa_ncl")
    drivers_por_indicador: dict[str, list[Driver]] = Field(default_factory=dict)
    pronostico_over30_3mob: PronosticoOver30 | None = None
    advertencias: list[str] = Field(default_factory=list)


class FilaRanking(BaseModel):
    """Promedios por cuentas originadas al MOB horizonte; trae también q05/q95 y observados."""

    model_config = ConfigDict(extra="allow")

    ranking: int
    STRATEGY: str
    STRATEGY_SEGMENT: str | None = None
    cohortes: int
    cohortes_con_observado: int
    cuentas: int
    margen_por_cuenta_q50: float
    tasa_ncl_q50: float
    tasa_over30_q50: float


class RespuestaRanking(BaseModel):
    modelo: ModeloInfo
    horizonte_mob: int
    total: int
    ranking: list[FilaRanking]


class CohorteResumen(Cohorte):
    FAMILIA: str
    ACCOUNTS_0: int
    MOB_ULTIMO: int


class RespuestaCohortes(BaseModel):
    total: int
    cohortes: list[CohorteResumen]
