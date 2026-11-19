# Plataforma de IA para Data Science — Cubo Base Cards (Banamex)

Plataforma integral de IA local para el análisis, diagnóstico de calidad, pronóstico de riesgo crediticio y generación automática de presentaciones ejecutivas sobre el cubo agregado **Base Cards** de Banamex.

- **Diseño sin fuga de datos:** Opera 100% en infraestructura local mediante modelos de lenguaje de pesos abiertos (Ollama / vLLM). Ningún dato sale del perímetro.
- **Documentación oficial:**
  - [PRD_agentes.md](docs/PRD_agentes.md): Requisitos de producto, reglas de negocio y arquitectura funcional.
  - [Manual_Tecnico_Plataforma_BaseCards.md](docs/Manual_Tecnico_Plataforma_BaseCards.md): Manual técnico de arquitectura, despliegue y referencia operativa.

---

## Dos modos de ejecución: ¿Despliegue o Desarrollo?

| Modo | ¿Para quién es? | ¿Cómo se ejecuta? |
|---|---|---|
| **1. Despliegue Completo (Producción / Staging / Evaluación)** | Quien desea replicar la plataforma completa con todos sus servicios (API, UI Streamlit, Postgres, Keycloak, Celery workers y Ollama). | **100% Docker Compose.** No se instala nada en el host. |
| **2. Entorno de Desarrollo Local** | Desarrolladores del equipo o nuevos colaboradores que van a programar módulos, agentes en LangGraph, skills o correr pruebas unitarias (`pytest`). | **Entorno virtual `.venv` local.** Permite iterar y depurar en segundos sin esperar builds de Docker. |

---

## Modo 1: Despliegue Rápido de la Plataforma (Docker)

Para replicar y levantar la plataforma completa en una máquina con Docker y Docker Compose:

```bash
# 1. Clonar el repositorio
git clone <url-del-repositorio>
cd Agentes_IA_avanzada

# 2. Configurar variables de entorno
cp .env.example .env

# 3. Descargar modelos de lenguaje y levantar los contenedores
make modelos
make up

# 4. Inicializar base de datos, RAG y modelos de ML
make init

# 5. Ejecutar prueba de humo
make smoke
```
*La interfaz estará disponible en `http://localhost:8501` y la API en `http://localhost:8000`.*

---

## Modo 2: Guía de Desarrollo Local (Equipo y Colaboradores)

Esta sección es para quienes van a desarrollar código en `src/`, construir subagentes o agregar skills.

### Requisitos previos
- **Python 3.12 o 3.13** instalado en tu sistema operativo.
- **Git**.
- *(Opcional pero recomendado)* `uv` para gestión ultra rápida de paquetes.

---

### Paso a paso: Inicialización del entorno `.venv`

El entorno virtual se crea siempre en la **raíz del proyecto** (nunca dentro de `src/`).

#### En Windows (PowerShell):
```powershell
# 1. Posicionarse en la raíz del proyecto
cd Agentes_IA_avanzada

# 2. Crear el entorno virtual
python -m venv .venv

# 3. Activar el entorno
.venv\Scripts\Activate.ps1

# 4. Instalar dependencias base y registrar el paquete en modo editable
pip install --upgrade pip
pip install -r requirements.txt
pip install -e .
```

#### En Linux o macOS (Bash / Zsh):
```bash
# 1. Posicionarse en la raíz del proyecto
cd Agentes_IA_avanzada

# 2. Crear el entorno virtual
python3 -m venv .venv

# 3. Activar el entorno
source .venv/bin/activate

# 4. Instalar dependencias base y registrar el paquete en modo editable
pip install --upgrade pip
pip install -r requirements.txt
pip install -e .
```

#### Alternativa ultra rápida con `uv`:
Si tienes instalado [`uv`](https://docs.astral.sh/uv/):
```bash
uv venv --python 3.12
# Activar según tu OS (.venv\Scripts\Activate.ps1 o source .venv/bin/activate)
uv pip install -r requirements.txt
uv pip install -e .
```

> **¿Qué hace `pip install -e .`?**  
> Gracias a la configuración en `pyproject.toml`, instala la carpeta `src/basecards` en modo *editable*. Esto permite que desde cualquier archivo o test puedas hacer:
> ```python
> from basecards.presentaciones.grafo import loop_presentaciones
> from basecards.presentaciones.marcadores import formatear_marcador
> ```
> Cualquier cambio que hagas en tu código se refleja al instante sin reinstalar el paquete.

---

## Estructura del Código y Módulos

```
Agentes_IA_avanzada/
├── docs/                        # Documentación técnica y funcional
│   ├── PRD_agentes.md
│   └── Manual_Tecnico_Plataforma_BaseCards.md
│
├── pyproject.toml               # Configuración del paquete basecards y herramientas
├── requirements.txt             # Dependencias base con versiones fijadas
│
├── src/basecards/               # Paquete fuente principal
│   ├── ingesta/                 # [Integrante 1] Ingesta y validación columnar
│   ├── modelo/                  # [Integrante 1] MPDC (LightGBM cuantílico, curvas esperadas)
│   ├── soporte/                 # [Integrante 2] Validación determinista y tickets de incidentes
│   ├── copiloto/                # [Integrante 4] Grafo de análisis y triage de preguntas
│   ├── presentaciones/          # [Integrante 3] Generador de presentaciones y documentos (E6)
│   │   ├── grafo.py             # StateGraph principal de LangGraph
│   │   ├── estado.py            # TypedDict de estado del agente
│   │   ├── marcadores.py        # Sustitución y verificación de cifras {{m.*}}
│   │   ├── metricas_dsl.py      # Compilador seguro DSL -> SQL DuckDB (sin eval)
│   │   ├── nodos/               # Nodos individuales del grafo
│   │   └── herramientas/        # Herramientas tipadas (pptx, docx, validación)
│   └── plataforma/              # [Integrante 4] Identidad, checkpoints, memoria
│
├── skills/                      # Catálogo versionado de Agent Skills
│   ├── aprobadas/               # Skills oficiales en producción
│   │   ├── seguimiento-cosechas/
│   │   ├── rentabilidad/
│   │   ├── champion-vs-challenger/
│   │   └── formato-pptx/
│   └── borradores/              # Skills propuestas por el agente en revisión
│
└── tests/                       # Pruebas unitarias y de integración
    ├── e6_presentaciones/       # Pruebas de grafos, marcadores y verificación
    └── ...
```

---

## División de Responsabilidades del Equipo

| Integrante | Capacidad / Área | Responsabilidad Principal |
|---|---|---|
| **Integrante 1** | Núcleo de Analítica (E1, E2) | Features, entrenamiento MPDC (9 modelos LightGBM), scoring, API `/score-cohorte`. |
| **Integrante 2** | Agente de Soporte (E1, E3, E4) | Reglas R-, P-, N-, C-, detección de desvíos K-01, diagnóstico ReAct y tickets. |
| **Integrante 3** | Creador de Presentaciones (E6) | Skills de negocio y formato, grafo LangGraph E6, ensamblado Office y verificación de cifras. |
| **Integrante 4** | Copiloto y Plataforma (E0, E5, E7)| Infraestructura Docker, orquestador, LLM local, Keycloak, memoria y auditoría. |

---

## Lineamientos para Nuevas Aportaciones

1. **El LLM nunca calcula cifras ni decide permisos:**
   - Toda cifra proviene de consultas a DuckDB/Pandas o del modelo de ML.
   - En la generación de texto se usan marcadores como `{{m.tasa_ncl.202401.MOB12|pct2}}`.
   - Si el LLM escribe números literales, el validador de cifras los rechaza.

2. **Nuevas Skills:**
   - Deben crearse dentro de `skills/aprobadas/<nombre>/` con la estructura oficial: `SKILL.md`, `schema.json`, `metrics.yaml`, `charts.yaml`, `template.pptx` y `metadata.json`.
   - Las métricas deben definirse usando el DSL cerrado de `metrics.yaml` (nunca código Python arbitrario ni `eval`).

3. **Pruebas de Calidad:**
   Antes de hacer commit, verifica que el código cumpla con los estándares:
   ```bash
   # Formato y estilo con Ruff
   ruff check .
   ruff format .

   # Ejecución de tests unitarios
   pytest tests/
   ```

---

## Contacto y Soporte

Proyecto desarrollado para la materia *Inteligencia Artificial Avanzada para la Ciencia de Datos (TC3009C.621)* con el socio formador **Banamex**.
Para dudas arquitectónicas o de negocio, consultar [docs/PRD_agentes.md](docs/PRD_agentes.md).
