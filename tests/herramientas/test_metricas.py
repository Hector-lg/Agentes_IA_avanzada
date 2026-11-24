"""Pruebas del compilador del DSL de métricas y de ``metricas_calcular``."""

from __future__ import annotations

import shutil
from pathlib import Path

import duckdb
import pytest

from basecards.herramientas.metricas import (
    MetricaError,
    compilar_expresion,
    metricas_calcular,
)

COLUMNAS = {"ACCOUNTS", "REVENUE", "NCLAMTS", "EXPENSES_TOTAL", "ENRACCTS"}
RAIZ_REPO = Path(__file__).resolve().parents[2]
SKILL = RAIZ_REPO / "skills" / "aprobadas" / "seguimiento-cosechas"


def _codigo(exc: pytest.ExceptionInfo[MetricaError]) -> str:
    return exc.value.codigo


def _copia_skill(tmp_path: Path) -> Path:
    destino = tmp_path / "seguimiento-cosechas"
    shutil.copytree(SKILL, destino)
    return destino


def _reemplazar(ruta: Path, viejo: str, nuevo: str) -> None:
    texto = ruta.read_text(encoding="utf-8")
    assert viejo in texto, f"no se encontró {viejo!r}"
    ruta.write_text(texto.replace(viejo, nuevo, 1), encoding="utf-8")


# ---------------------------------------------------------------------------
# Compilador del DSL
# ---------------------------------------------------------------------------


def test_compila_suma_simple() -> None:
    assert compilar_expresion("sum(ACCOUNTS)", COLUMNAS) == 'SUM("ACCOUNTS")'


def test_compila_count() -> None:
    assert compilar_expresion("count(ACCOUNTS)", COLUMNAS) == 'COUNT("ACCOUNTS")'


def test_compila_aritmetica_respetando_precedencia() -> None:
    sql = compilar_expresion("1 + 2 * 3", COLUMNAS)
    assert sql == "(1.0) + ((2.0) * (3.0))"


def test_compila_resta_encadenada_por_la_izquierda() -> None:
    sql = compilar_expresion("sum(REVENUE) - sum(NCLAMTS) - sum(EXPENSES_TOTAL)", COLUMNAS)
    assert sql == ('((SUM("REVENUE")) - (SUM("NCLAMTS"))) - (SUM("EXPENSES_TOTAL"))')


def test_division_protege_el_denominador_con_nullif() -> None:
    sql = compilar_expresion("sum(NCLAMTS) / sum(ENRACCTS)", COLUMNAS)
    assert sql == '(SUM("NCLAMTS")) / NULLIF((SUM("ENRACCTS")), 0)'


def test_parentesis_cambian_el_orden() -> None:
    sql = compilar_expresion("(sum(NCLAMTS) + sum(REVENUE)) / 2", COLUMNAS)
    assert sql == '((SUM("NCLAMTS")) + (SUM("REVENUE"))) / NULLIF((2.0), 0)'


def test_rechaza_caracteres_no_permitidos() -> None:
    with pytest.raises(MetricaError) as exc:
        compilar_expresion("sum(ACCOUNTS); DROP TABLE base_cards", COLUMNAS)
    assert _codigo(exc) == "E-MET-010"


def test_rechaza_columna_inexistente() -> None:
    with pytest.raises(MetricaError) as exc:
        compilar_expresion("sum(FANTASMA)", COLUMNAS)
    assert _codigo(exc) == "E-MET-012"


def test_rechaza_funcion_no_permitida() -> None:
    with pytest.raises(MetricaError) as exc:
        compilar_expresion("avg(ACCOUNTS)", COLUMNAS)
    assert _codigo(exc) == "E-MET-013"


def test_rechaza_expresion_vacia() -> None:
    with pytest.raises(MetricaError) as exc:
        compilar_expresion("   ", COLUMNAS)
    assert _codigo(exc) == "E-MET-011"


def test_rechaza_parentesis_desbalanceados() -> None:
    with pytest.raises(MetricaError) as exc:
        compilar_expresion("sum(ACCOUNTS", COLUMNAS)
    assert _codigo(exc) == "E-MET-011"


def test_rechaza_tokens_extra() -> None:
    with pytest.raises(MetricaError) as exc:
        compilar_expresion("sum(ACCOUNTS) DROP", COLUMNAS)
    assert _codigo(exc) == "E-MET-011"


# ---------------------------------------------------------------------------
# Cálculo contra DuckDB
# ---------------------------------------------------------------------------


@pytest.fixture
def datos(tmp_path: Path) -> Path:
    ruta = tmp_path / "base_cards.parquet"
    conexion = duckdb.connect()
    conexion.execute(
        """
        CREATE TABLE base_cards AS SELECT * FROM (VALUES
            (202401, 0, 1000, 10, 100.0, 10000.0, 1000, 500.0, 100.0),
            (202401, 1,  900, 27, 200.0,  9000.0, 1000, 450.0, 100.0),
            (202401, 2,  100, 50, 500.0,  1000.0, 1000,  50.0,  10.0),
            (202401, 2,  300, 30, 300.0,  3000.0, 1000, 150.0,  30.0),
            (202401, 3,  400,  4,   0.0,     0.0,  400, 100.0,  20.0),
            (202402, 0,  500,  5,  50.0,  5000.0,  500, 250.0,  50.0),
            (202402, 1,  400,  8,  80.0,  4000.0,  500, 200.0,  50.0)
        ) AS t(
            VINTAGE, MOB, ENRACCTS, OVER30ACCTS, NCLAMTS,
            ENRAMTS, ACCOUNTS, REVENUE, EXPENSES_TOTAL
        )
        """
    )
    conexion.execute(f"COPY base_cards TO '{ruta.as_posix()}' (FORMAT PARQUET)")
    conexion.close()
    return ruta


def test_tasa_es_razon_de_sumas(datos: Path) -> None:
    resultado = metricas_calcular(SKILL, "202401", 0, 0, datos=datos)
    assert resultado["{{m.tasa_over30.202401.MOB0|pct2}}"] == pytest.approx(0.01)


def test_expresion_aritmetica(datos: Path) -> None:
    resultado = metricas_calcular(SKILL, "202401", 0, 0, datos=datos)
    assert resultado["{{m.margen.202401.MOB0|mxn_millones}}"] == pytest.approx(300.0)


def test_volumen_respeta_el_formato_entero(datos: Path) -> None:
    resultado = metricas_calcular(SKILL, "202401", 0, 0, datos=datos)
    valor = resultado["{{m.volumen.202401.MOB0|int}}"]
    assert valor == 1000
    assert isinstance(valor, int)


def test_filtro_de_exposicion_excluye_filas_pequenas(datos: Path) -> None:
    # MOB2 tiene una fila con ENRACCTS=100 (se descarta) y otra con 300.
    resultado = metricas_calcular(SKILL, "202401", 2, 2, datos=datos)
    assert resultado["{{m.tasa_over30.202401.MOB2|pct2}}"] == pytest.approx(0.1)


def test_rango_mob_limita_las_claves(datos: Path) -> None:
    resultado = metricas_calcular(SKILL, "202401", 0, 1, datos=datos)
    mobs = {clave.split(".")[3].split("|")[0] for clave in resultado}
    assert mobs == {"MOB0", "MOB1"}


def test_cohorte_todas_agrega_todos_los_vintages(datos: Path) -> None:
    resultado = metricas_calcular(SKILL, "todas", 0, 0, datos=datos)
    assert resultado["{{m.tasa_over30.todas.MOB0|pct2}}"] == pytest.approx(0.01)
    assert resultado["{{m.volumen.todas.MOB0|int}}"] == 1500


def test_denominador_cero_devuelve_cero(datos: Path) -> None:
    resultado = metricas_calcular(SKILL, "202401", 3, 3, datos=datos)
    assert resultado["{{m.tasa_ncl.202401.MOB3|pct2}}"] == 0.0


def test_lee_una_base_duckdb(datos: Path, tmp_path: Path) -> None:
    base = tmp_path / "base_cards.duckdb"
    conexion = duckdb.connect(str(base))
    conexion.execute(f"CREATE TABLE base_cards AS SELECT * FROM read_parquet('{datos.as_posix()}')")
    conexion.close()

    resultado = metricas_calcular(SKILL, "202401", 0, 0, datos=base)
    assert resultado["{{m.tasa_over30.202401.MOB0|pct2}}"] == pytest.approx(0.01)


def test_base_duckdb_con_varias_tablas_exige_tabla(tmp_path: Path, datos: Path) -> None:
    base = tmp_path / "varias.duckdb"
    conexion = duckdb.connect(str(base))
    conexion.execute(f"CREATE TABLE base_cards AS SELECT * FROM read_parquet('{datos.as_posix()}')")
    conexion.execute("CREATE TABLE otra AS SELECT 1 AS X")
    conexion.close()

    with pytest.raises(MetricaError) as exc:
        metricas_calcular(SKILL, "202401", 0, 0, datos=base)
    assert _codigo(exc) == "E-MET-020"

    resultado = metricas_calcular(SKILL, "202401", 0, 0, datos=base, tabla="base_cards")
    assert resultado["{{m.tasa_over30.202401.MOB0|pct2}}"] == pytest.approx(0.01)


# ---------------------------------------------------------------------------
# Errores de entrada y de datos
# ---------------------------------------------------------------------------


def test_rechaza_caracteres_no_permitidos_en_metrics(tmp_path: Path, datos: Path) -> None:
    skill = _copia_skill(tmp_path)
    _reemplazar(
        skill / "metrics.yaml",
        'expresion: "sum(ACCOUNTS)"',
        'expresion: "sum(ACCOUNTS); DROP TABLE base_cards"',
    )
    with pytest.raises(MetricaError) as exc:
        metricas_calcular(skill, "202401", 0, 0, datos=datos)
    assert _codigo(exc) == "E-MET-010"


def test_rechaza_columna_inexistente_en_metrics(tmp_path: Path, datos: Path) -> None:
    skill = _copia_skill(tmp_path)
    _reemplazar(
        skill / "metrics.yaml",
        "numerador: {sum: OVER30ACCTS}",
        "numerador: {sum: FANTASMA}",
    )
    with pytest.raises(MetricaError) as exc:
        metricas_calcular(skill, "202401", 0, 0, datos=datos)
    assert _codigo(exc) == "E-MET-012"


def test_rechaza_funcion_no_permitida_en_metrics(tmp_path: Path, datos: Path) -> None:
    skill = _copia_skill(tmp_path)
    _reemplazar(
        skill / "metrics.yaml",
        'expresion: "sum(ACCOUNTS)"',
        'expresion: "avg(ACCOUNTS)"',
    )
    with pytest.raises(MetricaError) as exc:
        metricas_calcular(skill, "202401", 0, 0, datos=datos)
    assert _codigo(exc) == "E-MET-013"


def test_rechaza_formato_desconocido(tmp_path: Path, datos: Path) -> None:
    skill = _copia_skill(tmp_path)
    _reemplazar(skill / "metrics.yaml", "formato: pct2", "formato: porcentaje")
    with pytest.raises(MetricaError) as exc:
        metricas_calcular(skill, "202401", 0, 0, datos=datos)
    assert _codigo(exc) == "E-MET-014"


def test_rechaza_cohorte_invalida(datos: Path) -> None:
    with pytest.raises(MetricaError) as exc:
        metricas_calcular(SKILL, "2024-01", 0, 0, datos=datos)
    assert _codigo(exc) == "E-MET-003"


def test_rechaza_rango_mob_invertido(datos: Path) -> None:
    with pytest.raises(MetricaError) as exc:
        metricas_calcular(SKILL, "202401", 5, 1, datos=datos)
    assert _codigo(exc) == "E-MET-004"


def test_rechaza_mob_arriba_del_schema(datos: Path) -> None:
    with pytest.raises(MetricaError) as exc:
        metricas_calcular(SKILL, "202401", 0, 33, datos=datos)
    assert _codigo(exc) == "E-MET-004"


def test_rechaza_fuente_inexistente(tmp_path: Path) -> None:
    with pytest.raises(MetricaError) as exc:
        metricas_calcular(SKILL, "202401", 0, 0, datos=tmp_path / "nada.parquet")
    assert _codigo(exc) == "E-MET-020"


def test_rechaza_skill_sin_metrics_yaml(tmp_path: Path) -> None:
    with pytest.raises(MetricaError) as exc:
        metricas_calcular(tmp_path, "202401", 0, 0)
    assert _codigo(exc) == "E-MET-001"


def test_error_de_duckdb_si_faltan_columnas(tmp_path: Path) -> None:
    ruta = tmp_path / "incompleto.parquet"
    conexion = duckdb.connect()
    conexion.execute("CREATE TABLE t AS SELECT 202401 AS VINTAGE, 0 AS MOB")
    conexion.execute(f"COPY t TO '{ruta.as_posix()}' (FORMAT PARQUET)")
    conexion.close()

    with pytest.raises(MetricaError) as exc:
        metricas_calcular(SKILL, "202401", 0, 0, datos=ruta)
    assert _codigo(exc) == "E-MET-021"
