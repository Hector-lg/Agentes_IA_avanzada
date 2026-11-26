# E1 Ingesta y E2 MPDC — Núcleo de analítica (Integrante 1)

Guía de lo implementado para la ingesta del corte (E1) y el Modelo de Pronóstico de Desempeño por
Cohorte (MPDC, E2). Complementa a [PRD_agentes.md](PRD_agentes.md) §5–§6 y al
[Manual técnico](Manual_Tecnico_Plataforma_BaseCards.md) §2.6 y §5. Cada issue agrega su sección.

## 1. Cómo se corre

```powershell
# Entorno (una vez): Python 3.13 + dependencias
python -m venv .venv; .venv\Scripts\Activate.ps1
pip install -r requirements.txt; pip install -e .

# E1 — CSV del corte → Parquet tipado (+ validación del Agente de Soporte)
python -m basecards.ingesta --csv "ruta\base_cards_sintetica.csv" --corte 202609 --validar
```

En Linux es igual (`source .venv/bin/activate`). `data/` no se versiona: el CSV y el Parquet
viven solo en cada máquina.

| Paso | Tiempo medido (laptop, 16 hilos) |
|---|---|
| Ingesta (sin validación) | 7–11 s para 1,089,833 filas (193.5 MB CSV → 37.3 MB Parquet zstd) |
| Validación E1 del Agente de Soporte (`--validar`) | ~66 s (con el fix de C-01 del issue #5; antes, la regla C-01 lanza `TypeError` y el manifiesto lo registra) |

## 2. E1 — Ingesta (`src/basecards/ingesta/`, issue #4)

`ingerir_csv(ruta, corte)` hace, en orden:

1. **Lee el CSV** con el cargador del Agente de Soporte (`basecards.soporte.carga`), que acepta
   encabezados con nombre de variable o con códigos `dummiN` (S-06). Así el Parquet y la validación
   ven exactamente los mismos datos.
2. **Exige las 22 columnas (R-01).** Si falta alguna lanza `IngestaError`: sin ellas no se puede
   armar un Parquet tipado. Las columnas extra se descartan y se listan en el manifiesto.
3. **N-01:** recorta los espacios finales de los catálogos (194,376 filas en el corte 202609, por
   ejemplo `"Brands-NonPayroll-OffUs&LE "`).
4. **Tipos explícitos:** catálogos `category`, conteos `int`, montos `float64`. Los valores que no se
   pueden convertir a número se cuentan en el manifiesto (`valores_no_numericos`).
5. **Escribe** `data/cortes/{corte}/base_cards.parquet` con compresión zstd.
6. **Escribe** `data/cortes/{corte}/manifest.json` con:
   - SHA-256 del CSV y del Parquet: el hash del corte que usa la ficha del modelo (Manual §5.9);
   - filas, cohortes, rangos de VINTAGE y MOB, último PERF_MONTH y tipos por columna;
   - **detección de series acumuladas (S-01):** para cada métrica de riesgo, la fracción de
     diferencias por MOB que son negativas. Si las series se ven acumuladas pero
     `config/indicadores.yaml` dice `acumulado: false` (o al revés), el manifiesto lo avisa en `avisos`.
7. **Con `--validar`** corre `validar_dataframe` del Agente de Soporte y guarda el dictamen
   (`apto` / `no apto`) en el manifiesto y los hallazgos en `hallazgos_e1.json`. Si una regla del
   Agente de Soporte lanza un error, la ingesta no se cae: deja el error en el manifiesto.

Funciones para el resto del equipo:

| Función | Para qué |
|---|---|
| `leer_corte(corte)` | DataFrame tipado del Parquet del corte |
| `leer_manifiesto(corte)` | metadatos, hashes, avisos y dictamen |
| `corte_anterior_derivado(df, corte)` | S-07: el mismo corte sin las filas con PERF_MONTH = corte (vectorizado) |
| `perf_month(vintage, mob)` | PERF_MONTH = VINTAGE + MOB meses (vectorizado) |

Para que `metricas_calcular` (presentaciones, issue #31) lea este Parquet basta con
`BASECARDS_DATOS=data/cortes/202609/base_cards.parquet`.

### Lo que encontró la ingesta en el corte 202609

| Dato | Valor |
|---|---|
| Filas / cohortes | 1,089,833 / 64,245 |
| VINTAGE / MOB | 202401–202609 / 0–32 |
| Nulos, llaves duplicadas, violaciones de R-14/R-15/R-16 | 0 |
| Filas normalizadas por N-01 | 194,376 |
| Series que nunca decrecen con el MOB | GCL, NCL, recuperaciones, Over30 (cuentas y saldo) y REVENUE |

Las series de riesgo se comportan como **acumuladas**, así que el manifiesto avisa que no coinciden
con `acumulado: false`. El ajuste del parámetro se hace en el issue #5.