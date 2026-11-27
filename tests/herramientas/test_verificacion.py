"""Pruebas de la herramienta documento_verificar_cifras.

Cubre:
- Normalización y parsing de números
- Extracción de cifras de texto plano
- Extracción de cifras de .pptx, .docx, .xlsx, .pdf
- Comparación contra valores esperados
- Manejo de errores
"""

from __future__ import annotations

from pathlib import Path

import pytest

from basecards.herramientas.verificacion import (
    E_VER_ARCHIVO_INEXISTENTE,
    E_VER_FORMATO_NO_SOPORTADO,
    E_VER_VALORES_VACIOS,
    Discrepancia,
    ResultadoVerificacion,
    VerificacionError,
    _extraer_numeros_texto,
    _normalizar_texto_numero,
    _parsear_numero,
    _valores_son_equivalentes,
    documento_verificar_cifras,
)

# ---------------------------------------------------------------------------
# Pruebas de normalización y parsing
# ---------------------------------------------------------------------------


class TestNormalizarTextoNumero:
    """Pruebas de _normalizar_texto_numero."""

    def test_numero_simple(self) -> None:
        assert _normalizar_texto_numero("123") == "123"

    def test_numero_con_decimales(self) -> None:
        assert _normalizar_texto_numero("123.45") == "123.45"

    def test_separador_miles_coma(self) -> None:
        # "1,234" en inglés es 1234 (separador de miles)
        assert _normalizar_texto_numero("1,234") == "1234"

    def test_separador_miles_punto(self) -> None:
        # "1.234" en español podría ser 1234 o 1.234
        # Nuestra función asume separador de miles si hay 3 dígitos después
        assert _normalizar_texto_numero("1.234") == "1234"

    def test_separador_decimal_coma(self) -> None:
        # "1,23" en español es 1.23
        assert _normalizar_texto_numero("1,23") == "1.23"

    def test_separador_decimal_punto(self) -> None:
        # "1.23" es 1.23
        assert _normalizar_texto_numero("1.23") == "1.23"

    def test_moneda_dolar(self) -> None:
        assert _normalizar_texto_numero("$1,234.56") == "1234.56"

    def test_moneda_euro(self) -> None:
        assert _normalizar_texto_numero("€1.234,56") == "1234.56"

    def test_porcentaje(self) -> None:
        assert _normalizar_texto_numero("12.34%") == "12.34"

    def test_numero_negativo(self) -> None:
        assert _normalizar_texto_numero("-123.45") == "-123.45"

    def test_numero_negativo_guion_largo(self) -> None:
        assert _normalizar_texto_numero("−123.45") == "-123.45"

    def test_vacio(self) -> None:
        assert _normalizar_texto_numero("") == ""

    def test_solo_simbolos(self) -> None:
        assert _normalizar_texto_numero("$%") == ""


class TestParsearNumero:
    """Pruebas de _parsear_numero."""

    def test_entero(self) -> None:
        assert _parsear_numero("123") == 123

    def test_decimal(self) -> None:
        assert _parsear_numero("123.45") == 123.45

    def test_negativo(self) -> None:
        assert _parsear_numero("-123.45") == -123.45

    def test_no_numero(self) -> None:
        assert _parsear_numero("abc") is None

    def test_vacio(self) -> None:
        assert _parsear_numero("") is None

    def test_entero_sin_decimales(self) -> None:
        # Si no tiene parte decimal, debe devolver int
        resultado = _parsear_numero("100.00")
        # Puede ser int o float según implementación
        assert resultado is not None
        assert resultado == 100


class TestExtraerNumerosTexto:
    """Pruebas de _extraer_numeros_texto."""

    def test_texto_simple(self) -> None:
        resultados = _extraer_numeros_texto("El saldo es $1,234.56 millones")
        assert len(resultados) == 1
        assert resultados[0][1] == 1234.56

    def test_varios_numeros(self) -> None:
        resultados = _extraer_numeros_texto("La tasa es 2.34% y el saldo es $1,234.56")
        assert len(resultados) == 2
        valores = [r[1] for r in resultados]
        assert 2.34 in valores
        assert 1234.56 in valores

    def test_sin_numeros(self) -> None:
        resultados = _extraer_numeros_texto("No hay números aquí")
        assert len(resultados) == 0

    def test_porcentaje(self) -> None:
        resultados = _extraer_numeros_texto("La tasa de morosidad alcanzó 2.34%")
        assert len(resultados) == 1
        assert resultados[0][1] == 2.34

    def test_numero_en_millones(self) -> None:
        resultados = _extraer_numeros_texto("El monto es $12.4 M")
        assert len(resultados) >= 1  # Al menos detecta 12.4

    def test_texto_con_multiples_formatos(self) -> None:
        texto = """
        Resumen ejecutivo:
        - Tasa Over30: 2.34%
        - Tasa NCL: 1.23%
        - Saldo: $1,234,567.89
        - Cuentas: 10,000
        """
        resultados = _extraer_numeros_texto(texto)
        assert len(resultados) >= 4


class TestValoresSonEquivalentes:
    """Pruebas de _valores_son_equivalentes."""

    def test_valores_iguales(self) -> None:
        assert _valores_son_equivalentes(123.45, 123.45, 0.01) is True

    def test_valores_dentro_tolerancia(self) -> None:
        assert _valores_son_equivalentes(123.45, 123.451, 0.01) is True

    def test_valores_fuera_tolerancia(self) -> None:
        assert _valores_son_equivalentes(123.45, 124.00, 0.01) is False

    def test_ambos_none(self) -> None:
        assert _valores_son_equivalentes(None, None, 0.01) is True

    def test_uno_none(self) -> None:
        assert _valores_son_equivalentes(123.45, None, 0.01) is False
        assert _valores_son_equivalentes(None, 123.45, 0.01) is False

    def test_tolerancia_cero(self) -> None:
        assert _valores_son_equivalentes(123.45, 123.45, 0.0) is True
        assert _valores_son_equivalentes(123.45, 123.46, 0.0) is False


# ---------------------------------------------------------------------------
# Pruebas de Discrepancia y ResultadoVerificacion
# ---------------------------------------------------------------------------


class TestDiscrepancia:
    """Pruebas de la clase Discrepancia."""

    def test_str_con_marcador(self) -> None:
        d = Discrepancia(
            ubicacion="diapositiva 3, tabla, fila 2",
            origen="tabla",
            esperado=2.34,
            encontrado=2.35,
            marcador="{{m.tasa_over30.202401.MOB12|pct2}}",
        )
        assert "diapositiva 3" in str(d)
        assert "2.34" in str(d)
        assert "2.35" in str(d)
        assert "{{m.tasa_over30" in str(d)

    def test_str_sin_marcador(self) -> None:
        d = Discrepancia(
            ubicacion="página 1",
            origen="pdf",
            esperado=100,
            encontrado=200,
        )
        assert "página 1" in str(d)
        assert "100" in str(d)
        assert "200" in str(d)

    def test_str_cifra_faltante(self) -> None:
        d = Discrepancia(
            ubicacion="tabla 1, fila 2",
            origen="tabla",
            esperado=100,
            encontrado=None,
        )
        assert "falta" in str(d).lower()


class TestResultadoVerificacion:
    """Pruebas de la clase ResultadoVerificacion."""

    def test_ok_sin_discrepancias(self) -> None:
        resultado = ResultadoVerificacion(archivo=Path("test.pptx"))
        assert resultado.ok is True
        assert resultado.discrepancias == 0

    def test_ok_con_discrepancias(self) -> None:
        resultado = ResultadoVerificacion(archivo=Path("test.pptx"))
        resultado.agregar(
            Discrepancia(
                ubicacion="test",
                origen="texto",
                esperado=1,
                encontrado=2,
            )
        )
        assert resultado.ok is False
        assert resultado.discrepancias == 1

    def test_agregar_multiples(self) -> None:
        resultado = ResultadoVerificacion(archivo=Path("test.pptx"))
        for i in range(3):
            resultado.agregar(
                Discrepancia(
                    ubicacion=f"test {i}",
                    origen="texto",
                    esperado=i,
                    encontrado=i + 1,
                )
            )
        assert resultado.discrepancias == 3
        assert len(resultado.detalle) == 3


# ---------------------------------------------------------------------------
# Pruebas de manejo de errores
# ---------------------------------------------------------------------------


class TestDocumentoVerificarCifrasErrores:
    """Pruebas de manejo de errores en documento_verificar_cifras."""

    def test_archivo_inexistente(self, tmp_path: Path) -> None:
        archivo = tmp_path / "no_existe.pptx"
        with pytest.raises(VerificacionError) as exc_info:
            documento_verificar_cifras(archivo, {"{{m.test|pct2}}": 1.0})
        assert exc_info.value.codigo == E_VER_ARCHIVO_INEXISTENTE

    def test_valores_vacios(self, tmp_path: Path) -> None:
        archivo = tmp_path / "test.txt"
        archivo.write_text("test")
        with pytest.raises(VerificacionError) as exc_info:
            documento_verificar_cifras(archivo, {})
        assert exc_info.value.codigo == E_VER_VALORES_VACIOS

    def test_formato_no_soportado(self, tmp_path: Path) -> None:
        archivo = tmp_path / "test.xyz"
        archivo.write_text("test")
        with pytest.raises(VerificacionError) as exc_info:
            documento_verificar_cifras(archivo, {"{{m.test|pct2}}": 1.0})
        assert exc_info.value.codigo == E_VER_FORMATO_NO_SOPORTADO


# ---------------------------------------------------------------------------
# Pruebas de extracción por formato (usando fixtures reales)
# ---------------------------------------------------------------------------


class TestExtraerCifrasMarkdown:
    """Pruebas de extracción de cifras de archivos Markdown."""

    def test_markdown_con_cifras(self, tmp_path: Path) -> None:
        archivo = tmp_path / "test.md"
        archivo.write_text(
            """
# Resumen ejecutivo

- Tasa Over30: 2.34%
- Tasa NCL: 1.23%
- Saldo: $1,234,567.89
""",
            encoding="utf-8",
        )
        resultado = documento_verificar_cifras(
            archivo,
            {"{{m.test|pct2}}": 2.34},
        )
        assert resultado.cifras_encontradas >= 3

    def test_markdown_sin_cifras(self, tmp_path: Path) -> None:
        archivo = tmp_path / "test.md"
        archivo.write_text("# Sin cifras\n\nSolo texto.", encoding="utf-8")
        resultado = documento_verificar_cifras(
            archivo,
            {"{{m.test|pct2}}": 1.0},
        )
        assert resultado.cifras_encontradas == 0


class TestExtraerCifrasHTML:
    """Pruebas de extracción de cifras de archivos HTML."""

    def test_html_con_cifras(self, tmp_path: Path) -> None:
        archivo = tmp_path / "test.html"
        archivo.write_text(
            """
<html>
<body>
<p>La tasa es 2.34% y el saldo es $1,234.56</p>
</body>
</html>
""",
            encoding="utf-8",
        )
        resultado = documento_verificar_cifras(
            archivo,
            {"{{m.test|pct2}}": 2.34},
        )
        assert resultado.cifras_encontradas >= 2

    def test_html_sin_cifras(self, tmp_path: Path) -> None:
        archivo = tmp_path / "test.html"
        archivo.write_text(
            "<html><body><p>Solo texto</p></body></html>",
            encoding="utf-8",
        )
        resultado = documento_verificar_cifras(
            archivo,
            {"{{m.test|pct2}}": 1.0},
        )
        assert resultado.cifras_encontradas == 0


# ---------------------------------------------------------------------------
# Pruebas de integración con archivos reales (se añadirán más en Commit 2)
# ---------------------------------------------------------------------------


class TestIntegracionPPTX:
    """Pruebas de integración con archivos .pptx reales.

    Se expandirán en Commit 2 con archivos de prueba que contengan
    tablas y gráficas nativas.
    """

    def test_pptx_inexistente(self, tmp_path: Path) -> None:
        """Test que verifica el error correcto para archivo inexistente."""
        archivo = tmp_path / "no_existe.pptx"
        with pytest.raises(VerificacionError) as exc_info:
            documento_verificar_cifras(archivo, {"{{m.test|pct2}}": 1.0})
        assert exc_info.value.codigo == E_VER_ARCHIVO_INEXISTENTE


class TestIntegracionDOCX:
    """Pruebas de integración con archivos .docx reales.

    Se expandirán en Commit 2.
    """

    def test_docx_inexistente(self, tmp_path: Path) -> None:
        archivo = tmp_path / "no_existe.docx"
        with pytest.raises(VerificacionError) as exc_info:
            documento_verificar_cifras(archivo, {"{{m.test|pct2}}": 1.0})
        assert exc_info.value.codigo == E_VER_ARCHIVO_INEXISTENTE


class TestIntegracionXLSX:
    """Pruebas de integración con archivos .xlsx reales.

    Se expandirán en Commit 2.
    """

    def test_xlsx_inexistente(self, tmp_path: Path) -> None:
        archivo = tmp_path / "no_existe.xlsx"
        with pytest.raises(VerificacionError) as exc_info:
            documento_verificar_cifras(archivo, {"{{m.test|pct2}}": 1.0})
        assert exc_info.value.codigo == E_VER_ARCHIVO_INEXISTENTE


class TestIntegracionPDF:
    """Pruebas de integración con archivos .pdf reales.

    Se expandirán en Commit 2.
    """

    def test_pdf_inexistente(self, tmp_path: Path) -> None:
        archivo = tmp_path / "no_existe.pdf"
        with pytest.raises(VerificacionError) as exc_info:
            documento_verificar_cifras(archivo, {"{{m.test|pct2}}": 1.0})
        assert exc_info.value.codigo == E_VER_ARCHIVO_INEXISTENTE
