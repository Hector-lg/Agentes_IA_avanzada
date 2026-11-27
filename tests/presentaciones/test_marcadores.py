from __future__ import annotations

import pytest

from basecards.presentaciones.marcadores import (
    MarcadorError,
    contiene_cifras_literales,
    parsear,
    render,
    render_marcadores,
)


def test_parsear_extrae_marcadores_validos() -> None:
    encontrados = parsear("NCL {{m.tasa_ncl.202401.MOB12|pct2}}.")

    assert len(encontrados) == 1
    assert encontrados[0].metrica == "tasa_ncl"
    assert encontrados[0].cohorte == "202401"
    assert encontrados[0].mob == 12
    assert encontrados[0].formato == "pct2"


def test_parsear_rechaza_formato_invalido() -> None:
    with pytest.raises(MarcadorError):
        parsear("{{m.tasa_ncl.202401.MOB12|porcentaje}}")


@pytest.mark.parametrize(
    ("valor", "formato", "esperado"),
    [
        (0.1234, "pct1", "12.3%"),
        (0.1234, "pct2", "12.34%"),
        (1234, "mxn", "$1,234"),
        (12_345_678, "mxn_millones", "$12.3 M"),
        (1234, "int", "1,234"),
    ],
)
def test_render_formatea_valores(valor: float, formato: str, esperado: str) -> None:
    assert render(valor, formato) == esperado


def test_render_marcadores_sustituye_sin_calcular() -> None:
    texto = "La tasa es {{m.tasa_ncl.202401.MOB12|pct2}}."
    metricas = {"{{m.tasa_ncl.202401.MOB12|pct2}}": 0.0234}

    assert render_marcadores(texto, metricas) == "La tasa es 2.34%."


def test_contiene_cifras_literales_ignora_marcadores_y_contexto() -> None:
    texto = (
        "MOB 12: {{m.tasa_ncl.202401.MOB12|pct2}} en 2025-Q1; la categoría 30+ sigue bajo control."
    )

    assert contiene_cifras_literales(texto) is False


def test_contiene_cifras_literales_rechaza_cifra_de_negocio() -> None:
    assert contiene_cifras_literales("La tasa observada es 2.34%.") is True
    assert contiene_cifras_literales("El saldo asciende a $1,200.") is True
