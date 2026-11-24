"""Pruebas del compilador del DSL de métricas y de ``metricas_calcular``."""

from __future__ import annotations

import pytest

from basecards.herramientas.metricas import MetricaError, compilar_expresion

COLUMNAS = {"ACCOUNTS", "REVENUE", "NCLAMTS", "EXPENSES_TOTAL", "ENRACCTS"}


def _codigo(exc: pytest.ExceptionInfo[MetricaError]) -> str:
    return exc.value.codigo


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
