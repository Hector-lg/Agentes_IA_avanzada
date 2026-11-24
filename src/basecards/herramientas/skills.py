"""Carga y validación de skills del catálogo ``skills/``.

Una skill cumple el estándar Agent Skills (``SKILL.md`` con encabezado YAML) y el
contrato del PRD (``schema.json``, ``metrics.yaml``, ``charts.yaml``,
``metadata.json`` y, opcionalmente, ``template.pptx``).

El módulo trabaja contra el sistema de archivos; la raíz se configura con
``BASECARDS_SKILLS_DIR``. No usa red, ``eval`` ni dependencias adicionales: el
encabezado YAML se interpreta con ``yaml.safe_load``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Literal

import jsonschema
import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

# ---------------------------------------------------------------------------
# Constantes del contrato
# ---------------------------------------------------------------------------

ARCHIVOS_OBLIGATORIOS = ("SKILL.md", "schema.json", "metrics.yaml", "charts.yaml", "metadata.json")
ARCHIVOS_INTEGRIDAD = ("SKILL.md", "schema.json", "metrics.yaml", "charts.yaml")
ARCHIVOS_TEXTO = (*ARCHIVOS_INTEGRIDAD, "metadata.json")
ARCHIVOS_BINARIOS = (".pptx", ".docx", ".xlsx", ".pdf", ".png", ".jpg", ".jpeg", ".svg")

NOMBRE_SKILL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
NOMBRE_USUARIO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

MARCADOR_RE = re.compile(
    r"\{\{\s*m\.(?P<metrica>[^.|}]+)\.(?P<cohorte>[^.|}]+)\."
    r"(?P<mob>[^.|}]+)\|(?P<formato>[^}]+)\}\}"
)
MARCADOR_PREFIJO = "{{m."
COHORTE_RE = re.compile(r"^(?:\d{6}|todas)$")
MOB_RE = re.compile(r"^MOB(?:0|[1-9]\d*)$")
MOB_MAX_POR_DEFECTO = 32

TOKEN_EXPRESION = re.compile(
    r"(?P<numero>\d+(?:\.\d+)?)"
    r"|(?P<identificador>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<operador>[-+*/(),])"
    r"|(?P<espacio>\s+)"
)
FUNCIONES_DSL = {"sum", "count"}
FORMATOS_VALOR = {"pct1", "pct2", "mxn", "mxn_millones", "int"}
CUANTILES = {"q05", "q50", "q95"}

SALIDAS_VALIDAS = {
    "pptx",
    "pdf",
    "docx",
    "xlsx",
    "odt",
    "ods",
    "odp",
    "md",
    "html",
    "csv",
    "parquet",
    "json",
    "png",
    "svg",
}

HERRAMIENTAS_VALIDAS = {
    "cubo_describir_corte",
    "cubo_consultar",
    "cubo_metricas_cohorte",
    "cubo_curva_vintage",
    "cubo_diff_cortes",
    "modelo_curva_esperada",
    "modelo_comparar_estrategias",
    "incidentes_buscar_similares",
    "docs_buscar",
    "reglas_explicar",
    "skills_listar",
    "skills_leer",
    "ticket_crear",
    "ticket_comentar",
    "ticket_actualizar_campos",
    "notificar",
    "corte_dictaminar",
    "paquete_validar",
    "metricas_calcular",
    "graficas_generar",
    "pptx_ensamblar",
    "docx_ensamblar",
    "xlsx_ensamblar",
    "datos_exportar",
    "texto_renderizar",
    "documento_verificar_cifras",
    "documento_convertir",
    "documento_miniaturas",
    "skill_proponer_borrador",
    "run_python",
    "draft_risk_memo",
    "recommend_action",
}

TIPOS_GRAFICA = {"barras", "columnas", "lineas", "abanico", "mapa_calor", "tabla"}
GRAFICAS_NATIVAS = {"barras", "columnas", "lineas", "tabla"}
GRAFICAS_IMAGEN = {"abanico", "mapa_calor"}

MAX_BYTES_TEXTO = 1_000_000

# Meta-esquema de ``schema.json``. No se resuelven ``$ref`` remotos: la
# validación es local y sin red.
META_ESQUEMA_PAQUETE: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["version", "diccionario", "llave", "columnas"],
    "additionalProperties": False,
    "properties": {
        "$schema": {"type": "string"},
        "version": {"type": "integer", "minimum": 1},
        "diccionario": {"type": "string", "minLength": 1},
        "granularidad": {"type": "string"},
        "llave": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1},
        },
        "columnas": {
            "type": "object",
            "minProperties": 1,
            "additionalProperties": {"$ref": "#/$defs/columna"},
        },
        "catalogos": {
            "type": "object",
            "additionalProperties": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string"},
            },
        },
        "reglas": {"type": "array", "items": {"$ref": "#/$defs/regla"}},
    },
    "$defs": {
        "columna": {
            "type": "object",
            "required": ["tipo", "rol"],
            "additionalProperties": False,
            "properties": {
                "tipo": {"enum": ["entero", "real", "catalogo"]},
                "rol": {"enum": ["dimension", "metrica"]},
                "min": {"type": "number"},
                "max": {"type": "number"},
                "catalogo": {"type": "string", "minLength": 1},
                "descripcion": {"type": "string"},
            },
        },
        "regla": {
            "type": "object",
            "required": ["id", "expresion"],
            "additionalProperties": False,
            "properties": {
                "id": {"type": "string", "pattern": "^[A-Z]-\\d{2}$"},
                "severidad": {"enum": ["critica", "alta", "media", "baja"]},
                "expresion": {"type": "string", "minLength": 1},
                "condicion": {"type": "string"},
                "tolerancia": {"type": "number"},
                "descripcion": {"type": "string"},
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Errores y modelos
# ---------------------------------------------------------------------------


class SkillError(Exception):
    """Error de carga o validación con código estable."""

    def __init__(self, codigo: str, mensaje: str, archivo: str = "", ruta: str = "") -> None:
        super().__init__(f"{codigo}: {mensaje}")
        self.codigo = codigo
        self.mensaje = mensaje
        self.archivo = archivo
        self.ruta = ruta


class Hallazgo(BaseModel):
    codigo: str
    mensaje: str
    archivo: str = ""
    ruta: str = ""
    severidad: Literal["error", "advertencia"] = "error"


class ResultadoValidacion(BaseModel):
    skill: str
    version: str | None = None
    valido: bool
    errores: list[Hallazgo] = Field(default_factory=list)
    advertencias: list[Hallazgo] = Field(default_factory=list)
    archivos_presentes: list[str] = Field(default_factory=list)

    def resumen(self) -> str:
        estado = "OK" if self.valido else "CON ERRORES"
        lineas = [f"{self.skill} {self.version or ''}".strip() + f": {estado}"]
        lineas.append(f"  errores: {len(self.errores)} | advertencias: {len(self.advertencias)}")
        for hallazgo in [*self.errores, *self.advertencias]:
            ubicacion = f" ({hallazgo.ruta})" if hallazgo.ruta else ""
            lineas.append(
                f"  [{hallazgo.codigo}] {hallazgo.archivo}: {hallazgo.mensaje}{ubicacion}"
            )
        return "\n".join(lineas)


class ResumenSkill(BaseModel):
    nombre: str
    version: str | None = None
    descripcion: str = ""
    aprobador: str | None = None
    fecha_aprobacion: date | None = None
    salidas: list[str] = Field(default_factory=list)
    ruta: str
    origen: Literal["aprobadas", "borradores"]
    valido: bool = True


class FrenteSkill(BaseModel):
    """Encabezado YAML de ``SKILL.md`` (estándar Agent Skills)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    description: str
    allowed_tools: list[str] = Field(default_factory=list, alias="allowed-tools")
    salidas: list[str] = Field(default_factory=list)

    @field_validator("allowed_tools", mode="before")
    @classmethod
    def _separar_herramientas(cls, valor: Any) -> Any:
        # El estándar Agent Skills admite una cadena separada por espacios.
        if isinstance(valor, str):
            return valor.split()
        return valor

    @field_validator("salidas", mode="before")
    @classmethod
    def _separar_salidas(cls, valor: Any) -> Any:
        if isinstance(valor, str):
            return [parte for parte in valor.replace(",", " ").split() if parte]
        return valor


class MetadataSkill(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    autor: str
    aprobador: str
    fecha_aprobacion: date
    diccionario: str
    hash_template: str | None = None
    hash_contenido: str
    salidas: list[str] = Field(default_factory=list)
    cambios: str = ""

    @field_validator("version")
    @classmethod
    def _semver(cls, valor: str) -> str:
        if not VERSION_RE.fullmatch(valor):
            raise ValueError("usa SemVer MAJOR.MINOR.PATCH, por ejemplo 1.0.0")
        return valor

    @field_validator("hash_contenido")
    @classmethod
    def _hash_contenido(cls, valor: str) -> str:
        if not HASH_RE.fullmatch(valor):
            raise ValueError("usa sha256:<64 hex>")
        return valor

    @field_validator("hash_template")
    @classmethod
    def _hash_template(cls, valor: str | None) -> str | None:
        if valor is not None and not HASH_RE.fullmatch(valor):
            raise ValueError("usa sha256:<64 hex> o null")
        return valor


class Agregado(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sum: str | None = None
    count: str | None = None

    @model_validator(mode="after")
    def _exactamente_uno(self) -> Agregado:
        if (self.sum is None) == (self.count is None):
            raise ValueError("usa exactamente una de las claves sum o count")
        return self


class Metrica(BaseModel):
    model_config = ConfigDict(extra="forbid")

    descripcion: str = ""
    numerador: Agregado | None = None
    denominador: Agregado | None = None
    expresion: str | None = None
    formato: str

    @model_validator(mode="after")
    def _forma(self) -> Metrica:
        if self.expresion is not None:
            if self.numerador is not None or self.denominador is not None:
                raise ValueError("usa expresion o numerador/denominador, nunca ambos")
            return self
        if self.numerador is None or self.denominador is None:
            raise ValueError("faltan numerador y denominador, o una expresion")
        return self


class FiltroExposicion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    columna: str
    min: float


class IndicadorEsperado(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metrica: str
    nombre: str


class Esperadas(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fuente: str
    alias_modelo: str = "champion"
    indicadores: list[IndicadorEsperado] = Field(min_length=1)
    cuantiles: list[str] = Field(min_length=1)

    @field_validator("cuantiles")
    @classmethod
    def _cuantiles(cls, valores: list[str]) -> list[str]:
        desconocidos = set(valores) - CUANTILES
        if desconocidos:
            raise ValueError(f"cuantiles no soportados: {sorted(desconocidos)}")
        return valores


class MetricasDoc(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    skill: str | None = None
    filtro_exposicion: FiltroExposicion | None = None
    metricas: dict[str, Metrica] = Field(min_length=1)
    esperadas: Esperadas | None = None
    agrupaciones: list[str] = Field(default_factory=list)


class SerieGrafica(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metrica: str
    agrupacion: list[str] = Field(default_factory=list)
    etiqueta: str | None = None
    banda: str | None = None


class Grafica(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tipo: Literal["barras", "columnas", "lineas", "abanico", "mapa_calor", "tabla"]
    nativa: bool
    motor: str | None = None
    titulo: str | None = None
    eje_x: str | None = None
    series: list[SerieGrafica] = Field(default_factory=list)
    origen: str | None = None
    columnas: list[str] = Field(default_factory=list)
    max_filas: int | None = None
    filtro: str | None = None

    @field_validator("max_filas")
    @classmethod
    def _max_filas(cls, valor: int | None) -> int | None:
        if valor is not None and not 1 <= valor <= 100:
            raise ValueError("max_filas debe estar entre 1 y 100")
        return valor


class GraficasDoc(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    estilos: dict[str, Any] = Field(default_factory=dict)
    graficas: dict[str, Grafica] = Field(min_length=1)


class _Acumulador:
    def __init__(self) -> None:
        self.errores: list[Hallazgo] = []
        self.advertencias: list[Hallazgo] = []

    def error(self, codigo: str, mensaje: str, archivo: str = "", ruta: str = "") -> None:
        self.errores.append(Hallazgo(codigo=codigo, mensaje=mensaje, archivo=archivo, ruta=ruta))

    def advertencia(self, codigo: str, mensaje: str, archivo: str = "", ruta: str = "") -> None:
        self.advertencias.append(
            Hallazgo(
                codigo=codigo,
                mensaje=mensaje,
                archivo=archivo,
                ruta=ruta,
                severidad="advertencia",
            )
        )

    def agregar(self, exc: SkillError) -> None:
        self.error(exc.codigo, exc.mensaje, exc.archivo, exc.ruta)


# ---------------------------------------------------------------------------
# Lectura de archivos
# ---------------------------------------------------------------------------


def raiz_skills(raiz: Path | None = None) -> Path:
    """Raíz del catálogo de skills (``BASECARDS_SKILLS_DIR`` o ``<repo>/skills``)."""
    if raiz is not None:
        return Path(raiz).expanduser().resolve()
    entorno = os.environ.get("BASECARDS_SKILLS_DIR")
    if entorno:
        return Path(entorno).expanduser().resolve()
    return Path(__file__).resolve().parents[3] / "skills"


def _validar_nombre(nombre: str, *, archivo: str = "") -> None:
    if not NOMBRE_SKILL_RE.fullmatch(nombre):
        raise SkillError(
            "E-SKL-002",
            f"nombre inválido '{nombre}': usa minúsculas, números y guiones (2-64)",
            archivo,
        )


def _validar_usuario(user_id: str) -> None:
    if not NOMBRE_USUARIO_RE.fullmatch(user_id):
        raise SkillError("E-SKL-062", f"user_id inválido '{user_id}'")


def _dentro_de(ruta: Path, base: Path) -> bool:
    try:
        ruta.resolve().relative_to(base.resolve())
    except ValueError:
        return False
    return True


def _leer_texto(ruta: Path) -> str:
    if not ruta.is_file():
        raise SkillError("E-SKL-001", f"falta el archivo {ruta.name}", ruta.name)
    if ruta.stat().st_size > MAX_BYTES_TEXTO:
        raise SkillError("E-SKL-061", f"{ruta.name} supera {MAX_BYTES_TEXTO} bytes", ruta.name)
    try:
        return ruta.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SkillError("E-SKL-061", f"{ruta.name} no es UTF-8 válido", ruta.name) from exc


def _formatear_validacion(exc: ValidationError) -> str:
    partes = []
    for error in exc.errors()[:5]:
        ubicacion = ".".join(str(p) for p in error["loc"])
        partes.append(f"{ubicacion}: {error['msg']}")
    return "; ".join(partes)


def _partir_skill_md(texto: str) -> tuple[dict[str, Any], str]:
    lineas = texto.lstrip("\ufeff").splitlines()
    if not lineas or lineas[0].strip() != "---":
        raise SkillError("E-SKL-003", "SKILL.md debe iniciar con --- en la primera línea")
    cierre = next((i for i in range(1, len(lineas)) if lineas[i].strip() == "---"), None)
    if cierre is None:
        raise SkillError("E-SKL-003", "falta el cierre --- del encabezado YAML")
    try:
        datos = yaml.safe_load("\n".join(lineas[1:cierre])) or {}
    except yaml.YAMLError as exc:
        raise SkillError("E-SKL-003", f"encabezado YAML inválido: {exc}") from exc
    if not isinstance(datos, dict):
        raise SkillError("E-SKL-003", "el encabezado YAML debe ser un mapa")
    return datos, "\n".join(lineas[cierre + 1 :])


def _cargar_metadata(ruta: Path) -> MetadataSkill:
    try:
        datos = json.loads(_leer_texto(ruta / "metadata.json"))
    except json.JSONDecodeError as exc:
        raise SkillError("E-SKL-050", f"JSON inválido: {exc}", "metadata.json") from exc
    try:
        return MetadataSkill.model_validate(datos)
    except ValidationError as exc:
        raise SkillError("E-SKL-050", _formatear_validacion(exc), "metadata.json") from exc


def _cargar_yaml(ruta: Path, modelo: type[BaseModel], codigo: str) -> Any:
    try:
        datos = yaml.safe_load(_leer_texto(ruta)) or {}
    except yaml.YAMLError as exc:
        raise SkillError(codigo, f"YAML inválido: {exc}", ruta.name) from exc
    if not isinstance(datos, dict):
        raise SkillError(codigo, "el archivo debe ser un mapa YAML", ruta.name)
    try:
        return modelo.model_validate(datos)
    except ValidationError as exc:
        raise SkillError(codigo, _formatear_validacion(exc), ruta.name) from exc


def skill_hash_contenido(ruta: Path) -> str:
    """sha256 estable sobre los archivos de texto de la skill."""
    digest = hashlib.sha256()
    for nombre in ARCHIVOS_INTEGRIDAD:
        archivo = Path(ruta) / nombre
        if not archivo.is_file():
            continue
        digest.update(nombre.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(archivo.read_bytes())
        digest.update(b"\x00")
    return f"sha256:{digest.hexdigest()}"


def _hash_archivo(ruta: Path) -> str:
    return f"sha256:{hashlib.sha256(ruta.read_bytes()).hexdigest()}"


# ---------------------------------------------------------------------------
# Validación semántica
# ---------------------------------------------------------------------------


def _validar_frente(frente: FrenteSkill, nombre: str, acc: _Acumulador) -> None:
    if frente.name != nombre:
        acc.error(
            "E-SKL-002",
            f"el encabezado declara '{frente.name}' y la carpeta se llama '{nombre}'",
            "SKILL.md",
            "name",
        )
    if not frente.description.strip():
        acc.error("E-SKL-003", "falta description en el encabezado", "SKILL.md", "description")
    if len(frente.description) > 1024:
        acc.error("E-SKL-003", "description supera 1024 caracteres", "SKILL.md", "description")
    if not frente.allowed_tools:
        acc.error("E-SKL-004", "falta allowed-tools", "SKILL.md", "allowed-tools")
    for herramienta in frente.allowed_tools:
        if herramienta not in HERRAMIENTAS_VALIDAS:
            acc.error(
                "E-SKL-004",
                f"herramienta desconocida '{herramienta}'",
                "SKILL.md",
                "allowed-tools",
            )
    if not frente.salidas:
        acc.error("E-SKL-005", "falta salidas en el encabezado", "SKILL.md", "salidas")
    for salida in frente.salidas:
        if salida not in SALIDAS_VALIDAS:
            acc.error("E-SKL-005", f"salida no permitida '{salida}'", "SKILL.md", "salidas")


def _identificadores(texto: str) -> list[str]:
    return re.findall(r"[A-Za-z_][A-Za-z0-9_]*", texto)


def _validar_esquema_paquete(datos: Any, acc: _Acumulador) -> dict[str, Any] | None:
    if not isinstance(datos, dict):
        acc.error("E-SKL-010", "schema.json debe ser un objeto JSON", "schema.json")
        return None
    try:
        jsonschema.Draft202012Validator(META_ESQUEMA_PAQUETE).validate(datos)
    except jsonschema.ValidationError as exc:
        ruta = "/".join(str(p) for p in exc.absolute_path)
        acc.error("E-SKL-010", exc.message, "schema.json", ruta)
        return None

    columnas: dict[str, Any] = datos["columnas"]
    catalogos: dict[str, Any] = datos.get("catalogos", {})

    for nombre, columna in columnas.items():
        if columna.get("catalogo") and columna["catalogo"] not in catalogos:
            acc.error(
                "E-SKL-012",
                f"catálogo '{columna['catalogo']}' no definido en catalogos",
                "schema.json",
                f"columnas.{nombre}.catalogo",
            )
        if "min" in columna and "max" in columna and columna["min"] > columna["max"]:
            acc.error(
                "E-SKL-011",
                f"min mayor que max en la columna {nombre}",
                "schema.json",
                f"columnas.{nombre}",
            )

    for clave in datos["llave"]:
        if clave not in columnas:
            acc.error(
                "E-SKL-011",
                f"la llave '{clave}' no existe en columnas",
                "schema.json",
                "llave",
            )
        elif columnas[clave]["rol"] != "dimension":
            acc.error(
                "E-SKL-011",
                f"la llave '{clave}' no es una dimensión",
                "schema.json",
                "llave",
            )

    for indice, regla in enumerate(datos.get("reglas", [])):
        texto = f"{regla.get('expresion', '')} {regla.get('condicion', '')}"
        for identificador in _identificadores(texto):
            if identificador not in columnas and identificador not in {"true", "false"}:
                acc.error(
                    "E-SKL-013",
                    f"la regla {regla['id']} usa la columna inexistente '{identificador}'",
                    "schema.json",
                    f"reglas[{indice}]",
                )
    return datos


def _validar_expresion(
    expresion: str,
    columnas: dict[str, Any],
    acc: _Acumulador,
    ruta: str,
) -> None:
    tokens: list[tuple[str, str]] = []
    posicion = 0
    while posicion < len(expresion):
        coincidencia = TOKEN_EXPRESION.match(expresion, posicion)
        if coincidencia is None:
            acc.error(
                "E-SKL-022",
                f"carácter no permitido en la expresión: {expresion[posicion]!r}",
                "metrics.yaml",
                ruta,
            )
            return
        posicion = coincidencia.end()
        if coincidencia.lastgroup != "espacio":
            tokens.append((coincidencia.lastgroup or "", coincidencia.group()))
    if not tokens:
        acc.error("E-SKL-022", "expresión vacía", "metrics.yaml", ruta)
        return

    nivel = 0
    for posicion, (tipo, valor) in enumerate(tokens):
        if tipo != "identificador":
            if valor == "(":
                nivel += 1
            elif valor == ")":
                nivel -= 1
                if nivel < 0:
                    acc.error("E-SKL-022", "paréntesis desbalanceados", "metrics.yaml", ruta)
                    return
            continue
        siguiente = tokens[posicion + 1][1] if posicion + 1 < len(tokens) else ""
        if valor in FUNCIONES_DSL:
            if siguiente != "(":
                acc.error(
                    "E-SKL-022",
                    f"la función {valor} requiere paréntesis",
                    "metrics.yaml",
                    ruta,
                )
        elif siguiente == "(":
            acc.error(
                "E-SKL-022",
                f"función no permitida '{valor}'; solo sum y count",
                "metrics.yaml",
                ruta,
            )
        elif valor not in columnas:
            acc.error(
                "E-SKL-021",
                f"columna inexistente '{valor}'",
                "metrics.yaml",
                ruta,
            )
    if nivel != 0:
        acc.error("E-SKL-022", "paréntesis desbalanceados", "metrics.yaml", ruta)


def _validar_metricas(doc: MetricasDoc, columnas: dict[str, Any], acc: _Acumulador) -> None:
    dimensiones = {n for n, c in columnas.items() if c.get("rol") == "dimension"}
    for nombre, metrica in doc.metricas.items():
        ruta = f"metricas.{nombre}"
        if metrica.formato not in FORMATOS_VALOR:
            acc.error(
                "E-SKL-023",
                f"formato desconocido '{metrica.formato}'",
                "metrics.yaml",
                f"{ruta}.formato",
            )
        if metrica.expresion is not None:
            _validar_expresion(metrica.expresion, columnas, acc, f"{ruta}.expresion")
        for campo in ("numerador", "denominador"):
            agregado = getattr(metrica, campo)
            if agregado is None:
                continue
            columna = agregado.sum or agregado.count or ""
            if columna not in columnas:
                acc.error(
                    "E-SKL-021",
                    f"columna inexistente '{columna}'",
                    "metrics.yaml",
                    f"{ruta}.{campo}",
                )

    filtro = doc.filtro_exposicion
    if filtro is not None and filtro.columna not in columnas:
        acc.error(
            "E-SKL-021",
            f"columna inexistente '{filtro.columna}'",
            "metrics.yaml",
            "filtro_exposicion.columna",
        )

    for agrupacion in doc.agrupaciones:
        if agrupacion not in dimensiones:
            acc.error(
                "E-SKL-021",
                f"agrupación '{agrupacion}' no es una dimensión del schema",
                "metrics.yaml",
                "agrupaciones",
            )

    if doc.esperadas is not None:
        for indicador in doc.esperadas.indicadores:
            if indicador.metrica not in doc.metricas:
                acc.error(
                    "E-SKL-021",
                    f"el indicador esperado usa la métrica inexistente '{indicador.metrica}'",
                    "metrics.yaml",
                    "esperadas.indicadores",
                )


def _nombres_metricas(doc: MetricasDoc) -> set[str]:
    nombres = set(doc.metricas)
    if doc.esperadas is not None:
        for indicador in doc.esperadas.indicadores:
            for cuantil in doc.esperadas.cuantiles:
                nombres.add(f"{cuantil}_{indicador.nombre}")
    return nombres


def _validar_graficas(
    doc: GraficasDoc,
    doc_metricas: MetricasDoc | None,
    columnas: dict[str, Any],
    cuerpo: str,
    acc: _Acumulador,
) -> None:
    dimensiones = {n for n, c in columnas.items() if c.get("rol") == "dimension"}
    metricas = _nombres_metricas(doc_metricas) if doc_metricas else set()

    for grafica_id, grafica in doc.graficas.items():
        ruta = f"graficas.{grafica_id}"
        if grafica.tipo in GRAFICAS_NATIVAS and not grafica.nativa:
            acc.error(
                "E-SKL-032",
                f"la gráfica '{grafica.tipo}' debe ser nativa de PowerPoint",
                "charts.yaml",
                ruta,
            )
        if grafica.tipo in GRAFICAS_IMAGEN:
            if grafica.nativa:
                acc.error(
                    "E-SKL-032",
                    f"la gráfica '{grafica.tipo}' se genera como imagen matplotlib",
                    "charts.yaml",
                    ruta,
                )
            if grafica.motor != "matplotlib":
                acc.error(
                    "E-SKL-032",
                    "las gráficas de imagen requieren motor: matplotlib",
                    "charts.yaml",
                    ruta,
                )
        if grafica.eje_x is not None and grafica.eje_x not in dimensiones:
            acc.error(
                "E-SKL-032",
                f"eje_x '{grafica.eje_x}' no es una dimensión del schema",
                "charts.yaml",
                f"{ruta}.eje_x",
            )
        if grafica.tipo != "tabla" and not grafica.series:
            acc.error("E-SKL-030", "la gráfica no declara series", "charts.yaml", ruta)
        if grafica.tipo == "tabla" and not grafica.columnas:
            acc.error("E-SKL-030", "la tabla no declara columnas", "charts.yaml", ruta)
        for serie in grafica.series:
            if doc_metricas is not None and serie.metrica not in metricas:
                acc.error(
                    "E-SKL-031",
                    f"métrica inexistente '{serie.metrica}'",
                    "charts.yaml",
                    f"{ruta}.series",
                )
            for agrupacion in serie.agrupacion:
                if agrupacion not in dimensiones:
                    acc.error(
                        "E-SKL-021",
                        f"agrupación '{agrupacion}' no es una dimensión del schema",
                        "charts.yaml",
                        f"{ruta}.series",
                    )
        if grafica_id not in cuerpo:
            acc.advertencia(
                "W-SKL-103",
                f"la gráfica '{grafica_id}' no se menciona en SKILL.md",
                "charts.yaml",
                ruta,
            )


def _es_plantilla(valor: str) -> bool:
    limpio = valor.strip()
    return limpio.startswith("<") or limpio == "*"


def _validar_marcadores(cuerpo: str, nombres: set[str], mob_max: int, acc: _Acumulador) -> None:
    encontrados = list(MARCADOR_RE.finditer(cuerpo))
    if cuerpo.count(MARCADOR_PREFIJO) != len(encontrados):
        acc.error(
            "E-SKL-040",
            "hay marcadores m.* con formato inválido; usa {{m.metrica.cohorte.MOBn|formato}}",
            "SKILL.md",
        )
    for marcador in encontrados:
        metrica = marcador.group("metrica").strip()
        cohorte = marcador.group("cohorte").strip()
        mob = marcador.group("mob").strip()
        formato = marcador.group("formato").strip()
        if any(_es_plantilla(v) for v in (metrica, cohorte, mob, formato)):
            continue
        if metrica not in nombres:
            acc.error(
                "E-SKL-040",
                f"el marcador usa la métrica inexistente '{metrica}'",
                "SKILL.md",
            )
        if not COHORTE_RE.fullmatch(cohorte):
            acc.error(
                "E-SKL-040",
                f"cohorte inválida '{cohorte}'; usa AAAAMM o 'todas'",
                "SKILL.md",
            )
        if not MOB_RE.fullmatch(mob):
            acc.error(
                "E-SKL-040",
                f"MOB inválido '{mob}'; usa MOB0 a MOB{mob_max}",
                "SKILL.md",
            )
        elif int(mob.removeprefix("MOB")) > mob_max:
            acc.error(
                "E-SKL-040",
                f"MOB fuera de rango '{mob}'; usa MOB0 a MOB{mob_max}",
                "SKILL.md",
            )
        if formato not in FORMATOS_VALOR:
            acc.error(
                "E-SKL-040",
                f"formato desconocido '{formato}'",
                "SKILL.md",
            )


def _validar_integridad(
    ruta: Path,
    metadata: MetadataSkill,
    frente: FrenteSkill | None,
    esquema: dict[str, Any] | None,
    cuerpo: str,
    nombres_metricas: set[str],
    acc: _Acumulador,
) -> None:
    if esquema is not None and metadata.diccionario != esquema.get("diccionario"):
        acc.error(
            "E-SKL-052",
            "metadata.diccionario no coincide con schema.json",
            "metadata.json",
            "diccionario",
        )
    if frente is not None and set(metadata.salidas) != set(frente.salidas):
        acc.advertencia(
            "W-SKL-104",
            "metadata.salidas no coincide con el encabezado de SKILL.md",
            "metadata.json",
            "salidas",
        )

    calculado = skill_hash_contenido(ruta)
    if calculado != metadata.hash_contenido:
        acc.error(
            "E-SKL-051",
            f"hash_contenido no coincide; recalcula con skill_hash_contenido() ({calculado})",
            "metadata.json",
            "hash_contenido",
        )

    plantilla = ruta / "template.pptx"
    if plantilla.is_file():
        if metadata.hash_template is None:
            acc.advertencia(
                "W-SKL-105",
                "template.pptx presente pero hash_template es null",
                "metadata.json",
                "hash_template",
            )
        elif _hash_archivo(plantilla) != metadata.hash_template:
            acc.error(
                "E-SKL-051",
                "hash_template no coincide con template.pptx",
                "metadata.json",
                "hash_template",
            )
    elif metadata.hash_template is not None:
        acc.error(
            "E-SKL-051",
            "hash_template declarado pero falta template.pptx",
            "metadata.json",
            "hash_template",
        )

    mob_max = MOB_MAX_POR_DEFECTO
    if esquema is not None:
        maximo = esquema.get("columnas", {}).get("MOB", {}).get("max")
        if isinstance(maximo, (int, float)):
            mob_max = int(maximo)
    _validar_marcadores(cuerpo, nombres_metricas, mob_max, acc)


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def skill_validar(ruta: Path, *, raiz: Path | None = None) -> ResultadoValidacion:
    """Valida una skill completa y devuelve errores y advertencias."""
    ruta = Path(ruta).expanduser().resolve()
    acc = _Acumulador()
    nombre = ruta.name

    if raiz is not None and not _dentro_de(ruta, Path(raiz)):
        return ResultadoValidacion(
            skill=nombre,
            valido=False,
            errores=[
                Hallazgo(
                    codigo="E-SKL-060",
                    mensaje=f"la ruta {ruta} queda fuera de {raiz}",
                )
            ],
        )

    if not ruta.is_dir():
        return ResultadoValidacion(
            skill=nombre,
            valido=False,
            errores=[Hallazgo(codigo="E-SKL-060", mensaje=f"la skill no existe: {ruta}")],
        )

    if not NOMBRE_SKILL_RE.fullmatch(nombre):
        acc.error(
            "E-SKL-002",
            f"nombre de carpeta inválido '{nombre}'",
        )

    presentes = sorted(p.name for p in ruta.iterdir() if p.is_file())
    for requerido in ARCHIVOS_OBLIGATORIOS:
        if not (ruta / requerido).is_file():
            acc.error("E-SKL-001", f"falta el archivo obligatorio {requerido}", requerido)
    if not (ruta / "template.pptx").is_file():
        acc.advertencia(
            "W-SKL-100",
            "sin template.pptx: se usa config/branding/template_banamex.pptx",
            "template.pptx",
        )
    for carpeta in ("ejemplos", "tests"):
        if not (ruta / carpeta).is_dir():
            acc.advertencia("W-SKL-101", f"falta la carpeta {carpeta}/", carpeta)

    metadata: MetadataSkill | None = None
    version: str | None = None
    if (ruta / "metadata.json").is_file():
        try:
            metadata = _cargar_metadata(ruta)
            version = metadata.version
            if metadata.name != nombre:
                acc.error(
                    "E-SKL-002",
                    f"metadata.name '{metadata.name}' no coincide con la carpeta '{nombre}'",
                    "metadata.json",
                    "name",
                )
        except SkillError as exc:
            acc.agregar(exc)

    frente: FrenteSkill | None = None
    cuerpo = ""
    if (ruta / "SKILL.md").is_file():
        try:
            datos, cuerpo = _partir_skill_md(_leer_texto(ruta / "SKILL.md"))
            frente = FrenteSkill.model_validate(datos)
            _validar_frente(frente, nombre, acc)
        except SkillError as exc:
            acc.agregar(exc)
        except ValidationError as exc:
            acc.error("E-SKL-003", _formatear_validacion(exc), "SKILL.md")

    esquema_datos: dict[str, Any] | None = None
    if (ruta / "schema.json").is_file():
        try:
            crudo = json.loads(_leer_texto(ruta / "schema.json"))
        except json.JSONDecodeError as exc:
            acc.error("E-SKL-010", f"JSON inválido: {exc}", "schema.json")
        else:
            esquema_datos = _validar_esquema_paquete(crudo, acc)

    columnas = (esquema_datos or {}).get("columnas", {})
    metricas_doc: MetricasDoc | None = None
    if (ruta / "metrics.yaml").is_file():
        try:
            metricas_doc = _cargar_yaml(ruta / "metrics.yaml", MetricasDoc, "E-SKL-020")
        except SkillError as exc:
            acc.agregar(exc)
        else:
            if columnas:
                _validar_metricas(metricas_doc, columnas, acc)
            else:
                acc.advertencia(
                    "W-SKL-102",
                    "no se pudo cruzar metrics.yaml: schema.json inválido",
                    "metrics.yaml",
                )

    graficas_doc: GraficasDoc | None = None
    if (ruta / "charts.yaml").is_file():
        try:
            graficas_doc = _cargar_yaml(ruta / "charts.yaml", GraficasDoc, "E-SKL-030")
        except SkillError as exc:
            acc.agregar(exc)
        else:
            if columnas:
                _validar_graficas(graficas_doc, metricas_doc, columnas, cuerpo, acc)
            else:
                acc.advertencia(
                    "W-SKL-102",
                    "no se pudo cruzar charts.yaml: schema.json inválido",
                    "charts.yaml",
                )

    if metadata is not None:
        nombres = _nombres_metricas(metricas_doc) if metricas_doc else set()
        _validar_integridad(ruta, metadata, frente, esquema_datos, cuerpo, nombres, acc)

    return ResultadoValidacion(
        skill=nombre,
        version=version,
        valido=not acc.errores,
        errores=acc.errores,
        advertencias=acc.advertencias,
        archivos_presentes=presentes,
    )


def _ubicar_skill(
    base: Path, skill: str, *, user_id: str | None = None
) -> tuple[Path, Literal["aprobadas", "borradores"]]:
    _validar_nombre(skill)
    candidatos: list[tuple[Path, Literal["aprobadas", "borradores"]]] = []
    aprobada = base / "aprobadas" / skill
    if aprobada.is_dir():
        candidatos.append((aprobada, "aprobadas"))
    if user_id:
        _validar_usuario(user_id)
        borrador = base / "borradores" / user_id / skill
        if borrador.is_dir():
            candidatos.append((borrador, "borradores"))
    if not candidatos:
        raise SkillError(
            "E-SKL-001",
            f"skill '{skill}' no encontrada en aprobadas ni en borradores del usuario",
        )
    ruta, origen = candidatos[0]
    ruta = ruta.resolve()
    if not _dentro_de(ruta, base):
        raise SkillError("E-SKL-060", f"la ruta {ruta} queda fuera de {base}")
    return ruta, origen


def skills_listar(
    raiz: Path | None = None,
    *,
    incluir_borradores: bool = False,
    user_id: str | None = None,
) -> list[ResumenSkill]:
    """Skills aprobadas (y borradores del propio usuario) con su versión."""
    base = raiz_skills(raiz)
    carpetas: list[tuple[Path, Literal["aprobadas", "borradores"]]] = [
        (base / "aprobadas", "aprobadas")
    ]
    if incluir_borradores:
        if not user_id:
            raise SkillError("E-SKL-062", "incluir_borradores requiere user_id")
        _validar_usuario(user_id)
        carpetas.append((base / "borradores" / user_id, "borradores"))

    resumenes: list[ResumenSkill] = []
    for carpeta, origen in carpetas:
        if not carpeta.is_dir():
            continue
        for hija in sorted(carpeta.iterdir(), key=lambda p: p.name):
            if not hija.is_dir() or hija.name.startswith((".", "_")):
                continue
            if not _dentro_de(hija, base):
                continue
            version: str | None = None
            descripcion = ""
            aprobador: str | None = None
            fecha: date | None = None
            salidas: list[str] = []
            valido = True
            try:
                metadata = _cargar_metadata(hija)
                datos, _ = _partir_skill_md(_leer_texto(hija / "SKILL.md"))
                frente = FrenteSkill.model_validate(datos)
                if frente.name != hija.name:
                    raise SkillError("E-SKL-002", "nombre distinto al de la carpeta")
                version = metadata.version
                descripcion = frente.description
                aprobador = metadata.aprobador
                fecha = metadata.fecha_aprobacion
                salidas = frente.salidas
            except (SkillError, ValidationError):
                valido = False
            resumenes.append(
                ResumenSkill(
                    nombre=hija.name,
                    version=version,
                    descripcion=descripcion,
                    aprobador=aprobador,
                    fecha_aprobacion=fecha,
                    salidas=salidas,
                    ruta=str(hija),
                    origen=origen,
                    valido=valido,
                )
            )
    return resumenes


def skills_leer(
    skill: str,
    version: str | None = None,
    *,
    raiz: Path | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Devuelve los archivos de texto de una skill y el inventario de binarios.

    Solo lee skills aprobadas o borradores del ``user_id`` autenticado. El
    ``template.pptx`` no se devuelve en claro: se reporta nombre, tamaño y hash.
    """
    base = raiz_skills(raiz)
    ruta, origen = _ubicar_skill(base, skill, user_id=user_id)
    metadata = _cargar_metadata(ruta)
    if version is not None:
        if not VERSION_RE.fullmatch(version):
            raise SkillError("E-SKL-053", f"versión inválida '{version}'", "metadata.json")
        if version != metadata.version:
            raise SkillError(
                "E-SKL-053",
                f"versión {version} no disponible; en disco está {metadata.version}",
                "metadata.json",
            )

    datos, cuerpo = _partir_skill_md(_leer_texto(ruta / "SKILL.md"))
    frente = FrenteSkill.model_validate(datos)

    archivos: dict[str, str] = {}
    for nombre in ARCHIVOS_TEXTO:
        archivo = ruta / nombre
        if archivo.is_file():
            archivos[nombre] = _leer_texto(archivo)

    binarios = [
        {
            "nombre": archivo.name,
            "bytes": archivo.stat().st_size,
            "sha256": _hash_archivo(archivo),
        }
        for archivo in sorted(ruta.iterdir())
        if archivo.is_file() and archivo.suffix.lower() in ARCHIVOS_BINARIOS
    ]

    validacion = skill_validar(ruta, raiz=base)
    return {
        "metadata": metadata.model_dump(mode="json"),
        "frontmatter": frente.model_dump(by_alias=True),
        "cuerpo": cuerpo,
        "archivos": archivos,
        "binarios": binarios,
        "origen": origen,
        "validacion": {
            "valido": validacion.valido,
            "errores": [h.model_dump() for h in validacion.errores],
            "advertencias": [h.model_dump() for h in validacion.advertencias],
        },
    }


# ---------------------------------------------------------------------------
# CLI (compatible con skillctl validate del sandbox)
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m basecards.herramientas.skills",
        description="Lista y valida skills del catálogo Base Cards.",
    )
    subcomandos = parser.add_subparsers(dest="comando", required=True)

    validar = subcomandos.add_parser("validate", help="Valida una skill")
    validar.add_argument("ruta", type=Path)

    listar = subcomandos.add_parser("list", help="Lista skills aprobadas")
    listar.add_argument("--raiz", type=Path, default=None)
    listar.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)

    if args.comando == "validate":
        resultado = skill_validar(args.ruta)
        print(resultado.resumen())
        return 0 if resultado.valido else 1

    resumenes = skills_listar(args.raiz)
    if args.json:
        print(
            json.dumps(
                [r.model_dump(mode="json") for r in resumenes],
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        for resumen in resumenes:
            estado = "" if resumen.valido else " [inválida]"
            print(f"{resumen.nombre} {resumen.version or '?'}{estado}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
