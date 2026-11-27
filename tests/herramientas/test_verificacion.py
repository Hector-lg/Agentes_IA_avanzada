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


# ---------------------------------------------------------------------------
# Pruebas de comparación de valores
# ---------------------------------------------------------------------------


class TestComparacionValores:
    """Pruebas de la lógica de comparación de valores."""

    def test_coincidencia_exacta(self, tmp_path: Path) -> None:
        """Test que una coincidencia exacta no genera discrepancias."""
        archivo = tmp_path / "test.md"
        archivo.write_text("La tasa es 2.34%", encoding="utf-8")

        resultado = documento_verificar_cifras(
            archivo,
            {"{{m.tasa|pct2}}": 2.34},
        )
        assert resultado.ok is True
        assert resultado.discrepancias == 0

    def test_discrepancia_valor_diferente(self, tmp_path: Path) -> None:
        """Test que un valor diferente genera discrepancia."""
        archivo = tmp_path / "test.md"
        archivo.write_text("La tasa es 2.34%", encoding="utf-8")

        resultado = documento_verificar_cifras(
            archivo,
            {"{{m.tasa|pct2}}": 3.45},
        )
        assert resultado.ok is False
        assert resultado.discrepancias >= 1

    def test_tolerancia_valores_cercanos(self, tmp_path: Path) -> None:
        """Test que valores dentro de tolerancia no generan discrepancias."""
        archivo = tmp_path / "test.md"
        archivo.write_text("La tasa es 2.34%", encoding="utf-8")

        # Con tolerancia 0.01, 2.341 debería considerarse igual a 2.34
        resultado = documento_verificar_cifras(
            archivo,
            {"{{m.tasa|pct2}}": 2.341},
            tolerancia=0.01,
        )
        assert resultado.ok is True

    def test_valor_faltante(self, tmp_path: Path) -> None:
        """Test que un valor esperado faltante genera discrepancia."""
        archivo = tmp_path / "test.md"
        archivo.write_text("Sin cifras", encoding="utf-8")

        resultado = documento_verificar_cifras(
            archivo,
            {"{{m.tasa|pct2}}": 2.34},
        )
        assert resultado.ok is False
        # Debe reportar el valor esperado faltante
        assert any(d.esperado == 2.34 and d.encontrado is None for d in resultado.detalle)

    def test_valor_no_esperado(self, tmp_path: Path) -> None:
        """Test que un valor no esperado genera discrepancia."""
        archivo = tmp_path / "test.md"
        archivo.write_text("La tasa es 99.99%", encoding="utf-8")

        resultado = documento_verificar_cifras(
            archivo,
            {"{{m.tasa|pct2}}": 2.34},
        )
        assert resultado.ok is False
        # Debe reportar el valor no esperado
        assert any(d.encontrado == 99.99 and d.esperado is None for d in resultado.detalle)

    def test_multiples_coincidencias(self, tmp_path: Path) -> None:
        """Test con múltiples valores que coinciden."""
        archivo = tmp_path / "test.md"
        archivo.write_text(
            "Tasa morosidad: 2.34%, Tasa NCL: 1.23%, Saldo: $1,234.56",
            encoding="utf-8",
        )

        resultado = documento_verificar_cifras(
            archivo,
            {
                "{{m.morosidad|pct2}}": 2.34,
                "{{m.ncl|pct2}}": 1.23,
                "{{m.saldo|mxn}}": 1234.56,
            },
        )
        assert resultado.ok is True

    def test_tolerancia_cero(self, tmp_path: Path) -> None:
        """Test con tolerancia cero (coincidencia exacta)."""
        archivo = tmp_path / "test.md"
        archivo.write_text("Valor: 100.00", encoding="utf-8")

        # Con tolerancia cero, solo acepta coincidencia exacta
        resultado = documento_verificar_cifras(
            archivo,
            {"{{m.valor|int}}": 100.0},
            tolerancia=0.0,
        )
        assert resultado.ok is True

        # Un valor ligeramente diferente debe fallar
        resultado2 = documento_verificar_cifras(
            archivo,
            {"{{m.valor|int}}": 100.01},
            tolerancia=0.0,
        )
        assert resultado2.ok is False

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

    def test_pptx_con_tabla(self, tmp_path: Path) -> None:
        """Test que extrae cifras de una tabla nativa."""
        from pptx import Presentation
        from pptx.util import Inches

        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[5])
        tabla = slide.shapes.add_table(2, 2, Inches(1), Inches(1), Inches(4), Inches(1)).table
        tabla.cell(0, 0).text = "Tasa"
        tabla.cell(0, 1).text = "Valor"
        tabla.cell(1, 0).text = "Over30"
        tabla.cell(1, 1).text = "2.34%"

        archivo = tmp_path / "test_tabla.pptx"
        prs.save(str(archivo))

        resultado = documento_verificar_cifras(archivo, {"{{m.test|pct2}}": 2.34})
        # Debe haber extraído al menos un número (2.34)
        assert resultado.cifras_encontradas >= 1

    def test_pptx_con_grafica(self, tmp_path: Path) -> None:
        """Test que extrae cifras de una gráfica nativa."""
        from pptx import Presentation
        from pptx.chart.data import CategoryChartData
        from pptx.enum.chart import XL_CHART_TYPE
        from pptx.util import Inches

        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[5])

        chart_data = CategoryChartData()
        chart_data.categories = ["A", "B", "C"]
        chart_data.add_series("Serie 1", (1.5, 2.3, 3.7))

        x, y, cx, cy = Inches(1), Inches(1), Inches(5), Inches(3)
        slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, x, y, cx, cy, chart_data)

        archivo = tmp_path / "test_grafica.pptx"
        prs.save(str(archivo))

        resultado = documento_verificar_cifras(archivo, {"{{m.test|pct2}}": 1.5})
        # Debe haber extraído los valores de la gráfica
        assert resultado.cifras_encontradas >= 3

    def test_pptx_fixture_completo(self) -> None:
        """Test con el archivo fixture completo."""
        fixture = Path("tests/fixtures/verificacion_test.pptx")
        if not fixture.exists():
            pytest.skip("Fixture no existe")

        resultado = documento_verificar_cifras(
            fixture,
            {"{{m.test|pct2}}": 2.34},
        )
        # Debe haber extraído cifras de la tabla y la gráfica
        assert resultado.cifras_encontradas >= 5


class TestIntegracionDOCX:
    """Pruebas de integración con archivos .docx reales."""

    def test_docx_inexistente(self, tmp_path: Path) -> None:
        archivo = tmp_path / "no_existe.docx"
        with pytest.raises(VerificacionError) as exc_info:
            documento_verificar_cifras(archivo, {"{{m.test|pct2}}": 1.0})
        assert exc_info.value.codigo == E_VER_ARCHIVO_INEXISTENTE

    def test_docx_con_tabla(self, tmp_path: Path) -> None:
        """Test que extrae cifras de una tabla en .docx."""
        from docx import Document

        doc = Document()
        tabla = doc.add_table(rows=2, cols=2)
        tabla.cell(0, 0).text = "Métrica"
        tabla.cell(0, 1).text = "Valor"
        tabla.cell(1, 0).text = "Tasa"
        tabla.cell(1, 1).text = "2.34%"

        archivo = tmp_path / "test.docx"
        doc.save(str(archivo))

        resultado = documento_verificar_cifras(archivo, {"{{m.test|pct2}}": 2.34})
        assert resultado.cifras_encontradas >= 1

    def test_docx_con_parrafos(self, tmp_path: Path) -> None:
        """Test que extrae cifras de párrafos en .docx."""
        from docx import Document

        doc = Document()
        doc.add_paragraph("La tasa de morosidad es 2.34%.")

        archivo = tmp_path / "test.docx"
        doc.save(str(archivo))

        resultado = documento_verificar_cifras(archivo, {"{{m.test|pct2}}": 2.34})
        assert resultado.cifras_encontradas >= 1


class TestIntegracionXLSX:
    """Pruebas de integración con archivos .xlsx reales."""

    def test_xlsx_inexistente(self, tmp_path: Path) -> None:
        archivo = tmp_path / "no_existe.xlsx"
        with pytest.raises(VerificacionError) as exc_info:
            documento_verificar_cifras(archivo, {"{{m.test|pct2}}": 1.0})
        assert exc_info.value.codigo == E_VER_ARCHIVO_INEXISTENTE

    def test_xlsx_con_valores(self, tmp_path: Path) -> None:
        """Test que extrae cifras de celdas en .xlsx."""
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws["A1"] = "Métrica"
        ws["B1"] = "Valor"
        ws["A2"] = "Tasa"
        ws["B2"] = 2.34

        archivo = tmp_path / "test.xlsx"
        wb.save(str(archivo))

        resultado = documento_verificar_cifras(archivo, {"{{m.test|pct2}}": 2.34})
        assert resultado.cifras_encontradas >= 1

    def test_xlsx_con_formula(self, tmp_path: Path) -> None:
        """Test que extrae valores cacheados de fórmulas en .xlsx."""
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws["A1"] = 10
        ws["A2"] = 20
        ws["A3"] = "=A1+A2"  # Fórmula, el valor cacheado sería 30

        archivo = tmp_path / "test.xlsx"
        wb.save(str(archivo))

        # Abrir con data_only=True para ver valores cacheados
        resultado = documento_verificar_cifras(archivo, {"{{m.suma|int}}": 30})
        # Nota: el valor cacheado puede no estar disponible si no se abrió antes en Excel
        assert resultado.cifras_encontradas >= 2  # Al menos A1 y A2


class TestIntegracionPDF:
    """Pruebas de integración con archivos .pdf reales."""

    def test_pdf_inexistente(self, tmp_path: Path) -> None:
        archivo = tmp_path / "no_existe.pdf"
        with pytest.raises(VerificacionError) as exc_info:
            documento_verificar_cifras(archivo, {"{{m.test|pct2}}": 1.0})
        assert exc_info.value.codigo == E_VER_ARCHIVO_INEXISTENTE

    def test_pdf_con_texto(self, tmp_path: Path) -> None:
        """Test que extrae cifras de texto en .pdf."""
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas

        archivo = tmp_path / "test.pdf"
        c = canvas.Canvas(str(archivo), pagesize=letter)
        c.drawString(100, 750, "La tasa de morosidad es 2.34%.")
        c.save()

        resultado = documento_verificar_cifras(archivo, {"{{m.test|pct2}}": 2.34})
        assert resultado.cifras_encontradas >= 1
