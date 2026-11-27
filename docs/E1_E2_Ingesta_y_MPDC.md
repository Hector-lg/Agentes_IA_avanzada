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
| Validación E1 del Agente de Soporte (`--validar`) | ~90 s, dictamen `apto`; ~35 s son de P-07, que corre desde que `acumulado: true` (issue #5) |

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

Las series de riesgo se comportan como **acumuladas**. Por eso `config/indicadores.yaml` fija
`acumulado: true` (issue #5), lo que activa la regla P-07 del Agente de Soporte; en este corte P-07
no encuentra ninguna serie que baje. Si un corte futuro deja de comportarse así, el manifiesto lo
avisa en `avisos`.

### Ajustes del issue #5

- **`config/mapeo_columnas.yaml` (S-06):** los códigos `dummiN` ahora son los del diccionario de
  Banamex, que no son consecutivos (`dummi12` = STRATEGY, `dummi2` = AGE_TAG, `dummi44` = REVENUE…).
- **C-01 (`soporte/reglas/c_reglas.py`):** convierte a `float64` antes de `clip(lower=1e-9)`. Con
  columnas `Int64` que traen ceros (por ejemplo OVER30ACCTS), pandas lanzaba `TypeError` y
  `--validar` no daba dictamen.

## 3. E2 — Indicadores objetivo y features (`src/basecards/modelo/`, issue #6)

`construir_tabla(df)` recibe el corte tipado (`leer_corte(corte)`) y devuelve una fila por
cohorte × MOB con los indicadores objetivo, las features y `PERF_MONTH`.

### 3.1 Indicadores objetivo (`indicadores.py`)

| Indicador | Fórmula | Peso (exposición) |
|---|---|---|
| `tasa_ncl` | NCLAMTS / ENRAMTS | ENRAMTS |
| `tasa_over30` | OVER30ACCTS / ENRACCTS | ENRACCTS |
| `margen_por_cuenta` | MARGEN / ACCOUNTS | ACCOUNTS |

Si el denominador es 0, la tasa queda en NaN (no en `inf`). MARGEN depende de S-01: con
`acumulado: true`, REVENUE y NCLAMTS ya vienen acumulados al MOB pero los gastos son del mes, así
que se restan los gastos acumulados de la cohorte: `REVENUE − NCLAMTS − Σ EXPENSES_TOTAL(m ≤ MOB)`.
Las fórmulas y los pesos están en `config/modelo.yaml` (`modelo.objetivos`).

### 3.2 Features (`features.py`)

| Feature | De dónde sale |
|---|---|
| `MOB` | la fila |
| `SCORE_ORD` | posición del SCORE_TAG dentro de su familia (`<130` = 1 … `190+` = 8; `a.<=154` = 1 … `i.205+` = 9) |
| `VINTAGE_MES` | mes del año de VINTAGE |
| `LOG_ACCOUNTS`, `LINEA_PROM` | ACCOUNTS y CREDITLINESAMTS del MOB 0 de la cohorte (originación) |
| `UTIL_INICIAL` | ENRAMTS / CREDITLINESAMTS del MOB 0; NaN en las filas de MOB 0 |
| `STRATEGY, FAMILIA, STRATEGY_SEGMENT, PRODUCT, CHANNEL, AGE_TAG` | categóricas; `matriz()` fija sus categorías a las del entrenamiento |

- **Sin fuga de información:** ninguna feature usa métricas del MOB que se predice ni de MOB
  posteriores. `test_sin_fuga_de_informacion` altera las 14 métricas de todos los MOB ≥ 1 y verifica
  que ninguna feature cambie.
- **VINTAGE no entra como categoría**, para que el modelo pueda pronosticar cosechas que no vio.
  SCORE_TAG entra como `SCORE_ORD` + `FAMILIA` (`modelo.score_orden` y `modelo.familias`).
- **Para M4:** `agregar_rezagos_over30` agrega la tasa Over30 en t, t−1 y t−2, su pendiente y el
  objetivo en t+3. Solo usa MOB contiguos: si a una cohorte le falta un MOB, esas filas quedan en NaN.
- **Para proyectar:** `filas_a_mob(tabla, mob)` pone cada cohorte en un MOB dado (por ejemplo, el 12),
  aunque todavía no haya llegado ahí.

### 3.3 Lo que se ve en el corte 202609

| Dato | Valor |
|---|---|
| `construir_tabla` sobre 1,089,833 filas | 2.7 s (rezagos de M4: 0.5 s) |
| `SCORE_ORD` sin valor | 0 filas (Recalibration: 1,050,121 filas; DARIA: 39,712) |
| `UTIL_INICIAL` en NaN | 64,245 filas, una por cohorte: justo las de MOB 0 |
| ACCOUNTS dentro de una cohorte | constante en todos los MOB, así que funciona como atributo de originación |
| `tasa_ncl` (mediana / p99) | 0.048 / 0.413 (puede pasar de 1 porque la NCL es acumulada y ENRAMTS es del mes) |
| `tasa_over30` (mediana / p99) | 0.125 / 0.500 |
| `margen_por_cuenta` | negativo en todas las filas: a MOB 24 la NCL acumulada por cuenta (~1,135) supera al REVENUE acumulado (~452) |