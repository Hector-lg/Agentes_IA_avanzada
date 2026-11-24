# Datos de entrada

`metricas_calcular` resuelve la fuente de datos en este orden:

1. El parámetro `datos` de la llamada.
2. La variable de entorno `BASECARDS_DATOS`.
3. La ruta por defecto `data/base_cards.parquet`.

Formatos admitidos: `.parquet`, `.csv`, `.duckdb` y `.db`. Una base DuckDB debe
tener una sola tabla; si tiene varias, se indica con el parámetro `tabla`.

Las columnas mínimas son las que declara `schema.json` de cada skill. Para
`seguimiento-cosechas`: `VINTAGE`, `MOB`, las dimensiones de la llave y las
métricas `ENRACCTS`, `OVER30ACCTS`, `NCLAMTS`, `ENRAMTS`, `ACCOUNTS`, `REVENUE`
y `EXPENSES_TOTAL`.

El archivo de datos no se versiona en el repositorio.
