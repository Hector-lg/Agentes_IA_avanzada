"""Pruebas de ``documento_convertir`` y ``documento_miniaturas``.

Todas las pruebas usan un stub de ``SandboxRunner``: no dependen de LibreOffice
ni de ``pdftoppm`` reales.
"""

from __future__ import annotations

import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from basecards.herramientas.conversion import (
    E_CON_ARCHIVO_CORRUPTO,
    E_CON_ARCHIVO_INEXISTENTE,
    E_CON_DIRECTORIO_INVALIDO,
    E_CON_PDF_VACIO,
    E_CON_PDFTOPPM_FALLO,
    E_CON_PDFTOPPM_NO_DISPONIBLE,
    E_CON_SALIDA_INVALIDA,
    E_CON_SANDBOX,
    E_CON_SIN_MINIATURAS,
    E_CON_SOFFICE_FALLO,
    E_CON_SOFFICE_NO_DISPONIBLE,
    E_CON_TIMEOUT,
    ConversionError,
    ResultadoComando,
    documento_convertir,
    documento_miniaturas,
)


@dataclass
class _Llamada:
    comando: str
    argumentos: tuple[str, ...]
    timeout_s: float


class StubSandbox:
    """Simula el sandbox y registra los comandos que recibe."""

    def __init__(self, respuestas: list[object]) -> None:
        self.respuestas = respuestas
        self.llamadas: list[_Llamada] = []

    def ejecutar(
        self,
        comando: str,
        argumentos: Sequence[str],
        *,
        timeout_s: float = 120.0,
    ) -> ResultadoComando:
        self.llamadas.append(_Llamada(comando, tuple(argumentos), timeout_s))
        respuesta = self.respuestas.pop(0)
        if isinstance(respuesta, BaseException):
            raise respuesta
        if callable(respuesta):
            return respuesta(comando, tuple(argumentos))
        assert isinstance(respuesta, ResultadoComando)
        return respuesta


def _crear_pptx(ruta: Path) -> Path:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(ruta, "w") as paquete:
        paquete.writestr("[Content_Types].xml", "<Types/>")
        paquete.writestr("ppt/presentation.xml", "<p:presentation/>")
    return ruta


def _crear_pdf(
    comando: str,
    argumentos: tuple[str, ...],
    contenido: bytes = b"%PDF-1.4\n",
) -> ResultadoComando:
    assert comando == "soffice"
    outdir = Path(argumentos[argumentos.index("--outdir") + 1])
    origen = Path(argumentos[-1])
    (outdir / f"{origen.stem}.pdf").write_bytes(contenido)
    return ResultadoComando(codigo=0)


def _pdf_vacio(comando: str, argumentos: tuple[str, ...]) -> ResultadoComando:
    return _crear_pdf(comando, argumentos, contenido=b"")


def _conversion_y_miniaturas(
    paginas: int,
) -> Callable[[str, tuple[str, ...]], ResultadoComando]:
    def responder(comando: str, argumentos: tuple[str, ...]) -> ResultadoComando:
        if comando == "soffice":
            return _crear_pdf(comando, argumentos)
        if comando == "pdftoppm":
            prefijo = Path(argumentos[-1])
            for pagina in range(1, paginas + 1):
                Path(f"{prefijo}-{pagina}.png").write_bytes(b"\x89PNG\r\n")
            return ResultadoComando(codigo=0)
        raise AssertionError(f"comando inesperado: {comando}")

    return responder


# ---------------------------------------------------------------------------
# documento_convertir
# ---------------------------------------------------------------------------


def test_convierte_pptx_a_pdf(tmp_path: Path) -> None:
    origen = _crear_pptx(tmp_path / "presentacion.pptx")
    destino = tmp_path / "salida" / "presentacion.pdf"
    stub = StubSandbox([_crear_pdf])

    documento_convertir(origen, destino, runner=stub)

    assert destino.is_file()
    assert destino.read_bytes().startswith(b"%PDF")
    assert len(stub.llamadas) == 1
    llamada = stub.llamadas[0]
    assert llamada.comando == "soffice"
    assert llamada.argumentos[:4] == ("--headless", "--convert-to", "pdf", "--outdir")
    assert Path(llamada.argumentos[4]) == destino.parent.resolve()
    assert Path(llamada.argumentos[5]) == origen.resolve()
    assert llamada.timeout_s == 120.0


def test_renombra_el_pdf_si_la_salida_tiene_otro_nombre(tmp_path: Path) -> None:
    origen = _crear_pptx(tmp_path / "presentacion.pptx")
    destino = tmp_path / "informe.pdf"
    stub = StubSandbox([_crear_pdf])

    documento_convertir(origen, destino, runner=stub)

    assert destino.is_file()
    assert not (tmp_path / "presentacion.pdf").exists()


def test_pptx_inexistente_falla_antes_del_sandbox(tmp_path: Path) -> None:
    stub = StubSandbox([])

    with pytest.raises(ConversionError) as excinfo:
        documento_convertir(tmp_path / "no-existe.pptx", tmp_path / "salida.pdf", runner=stub)

    assert excinfo.value.codigo == E_CON_ARCHIVO_INEXISTENTE
    assert stub.llamadas == []


def test_pptx_corrupto_falla_antes_del_sandbox(tmp_path: Path) -> None:
    origen = tmp_path / "corrupto.pptx"
    origen.write_text("no soy un zip", encoding="utf-8")
    stub = StubSandbox([])

    with pytest.raises(ConversionError) as excinfo:
        documento_convertir(origen, tmp_path / "salida.pdf", runner=stub)

    assert excinfo.value.codigo == E_CON_ARCHIVO_CORRUPTO
    assert stub.llamadas == []


def test_salida_debe_ser_pdf(tmp_path: Path) -> None:
    origen = _crear_pptx(tmp_path / "presentacion.pptx")
    stub = StubSandbox([])

    with pytest.raises(ConversionError) as excinfo:
        documento_convertir(origen, tmp_path / "salida.docx", runner=stub)

    assert excinfo.value.codigo == E_CON_SALIDA_INVALIDA
    assert stub.llamadas == []


def test_soffice_falla(tmp_path: Path) -> None:
    origen = _crear_pptx(tmp_path / "presentacion.pptx")
    stub = StubSandbox([ResultadoComando(codigo=1, stderr="no se pudo convertir")])

    with pytest.raises(ConversionError) as excinfo:
        documento_convertir(origen, tmp_path / "salida.pdf", runner=stub)

    assert excinfo.value.codigo == E_CON_SOFFICE_FALLO
    assert "no se pudo convertir" in str(excinfo.value)


@pytest.mark.parametrize(
    "respuesta",
    [ResultadoComando(codigo=127), FileNotFoundError("soffice")],
)
def test_soffice_no_disponible(tmp_path: Path, respuesta: object) -> None:
    origen = _crear_pptx(tmp_path / "presentacion.pptx")
    stub = StubSandbox([respuesta])

    with pytest.raises(ConversionError) as excinfo:
        documento_convertir(origen, tmp_path / "salida.pdf", runner=stub)

    assert excinfo.value.codigo == E_CON_SOFFICE_NO_DISPONIBLE


def test_timeout(tmp_path: Path) -> None:
    origen = _crear_pptx(tmp_path / "presentacion.pptx")
    stub = StubSandbox([TimeoutError("agotado")])

    with pytest.raises(ConversionError) as excinfo:
        documento_convertir(origen, tmp_path / "salida.pdf", runner=stub, timeout_s=5.0)

    assert excinfo.value.codigo == E_CON_TIMEOUT
    assert stub.llamadas[0].timeout_s == 5.0


def test_pdf_vacio(tmp_path: Path) -> None:
    origen = _crear_pptx(tmp_path / "presentacion.pptx")
    stub = StubSandbox([_pdf_vacio])

    with pytest.raises(ConversionError) as excinfo:
        documento_convertir(origen, tmp_path / "salida.pdf", runner=stub)

    assert excinfo.value.codigo == E_CON_PDF_VACIO


def test_pdf_no_generado(tmp_path: Path) -> None:
    origen = _crear_pptx(tmp_path / "presentacion.pptx")
    stub = StubSandbox([ResultadoComando(codigo=0)])

    with pytest.raises(ConversionError) as excinfo:
        documento_convertir(origen, tmp_path / "salida.pdf", runner=stub)

    assert excinfo.value.codigo == E_CON_PDF_VACIO


def test_runner_no_configurado(tmp_path: Path) -> None:
    origen = _crear_pptx(tmp_path / "presentacion.pptx")

    with pytest.raises(ConversionError) as excinfo:
        documento_convertir(origen, tmp_path / "salida.pdf")

    assert excinfo.value.codigo == E_CON_SANDBOX


def test_error_del_runner(tmp_path: Path) -> None:
    origen = _crear_pptx(tmp_path / "presentacion.pptx")
    stub = StubSandbox([RuntimeError("sandbox caído")])

    with pytest.raises(ConversionError) as excinfo:
        documento_convertir(origen, tmp_path / "salida.pdf", runner=stub)

    assert excinfo.value.codigo == E_CON_SANDBOX


# ---------------------------------------------------------------------------
# documento_miniaturas
# ---------------------------------------------------------------------------


def test_genera_miniaturas_por_diapositiva(tmp_path: Path) -> None:
    origen = _crear_pptx(tmp_path / "presentacion.pptx")
    salida = tmp_path / "miniaturas"
    responder = _conversion_y_miniaturas(3)
    stub = StubSandbox([responder, responder])

    rutas = documento_miniaturas(origen, salida, runner=stub)

    assert [ruta.name for ruta in rutas] == [
        "diapositiva-01.png",
        "diapositiva-02.png",
        "diapositiva-03.png",
    ]
    assert all(ruta.is_file() for ruta in rutas)
    assert [llamada.comando for llamada in stub.llamadas] == ["soffice", "pdftoppm"]
    pdftoppm = stub.llamadas[1]
    assert pdftoppm.argumentos[:3] == ("-png", "-r", "80")
    assert Path(pdftoppm.argumentos[3]).suffix == ".pdf"
    assert Path(pdftoppm.argumentos[4]) == salida.resolve() / "diapositiva"
    assert not list(salida.glob("*.pdf"))
    assert not list(salida.glob(".basecards-miniaturas-*"))


def test_nombres_estables_con_mas_de_diez_paginas(tmp_path: Path) -> None:
    origen = _crear_pptx(tmp_path / "presentacion.pptx")
    salida = tmp_path / "miniaturas"
    responder = _conversion_y_miniaturas(12)
    stub = StubSandbox([responder, responder])

    rutas = documento_miniaturas(origen, salida, runner=stub)

    esperadas = [f"diapositiva-{pagina:02d}.png" for pagina in range(1, 13)]
    assert [ruta.name for ruta in rutas] == esperadas


def test_limpia_miniaturas_previas(tmp_path: Path) -> None:
    origen = _crear_pptx(tmp_path / "presentacion.pptx")
    salida = tmp_path / "miniaturas"
    salida.mkdir()
    (salida / "diapositiva-99.png").write_bytes(b"viejo")
    responder = _conversion_y_miniaturas(2)
    stub = StubSandbox([responder, responder])

    rutas = documento_miniaturas(origen, salida, runner=stub)

    assert not (salida / "diapositiva-99.png").exists()
    assert len(rutas) == 2


def test_pdftoppm_falla(tmp_path: Path) -> None:
    origen = _crear_pptx(tmp_path / "presentacion.pptx")
    salida = tmp_path / "miniaturas"

    def responder(comando: str, argumentos: tuple[str, ...]) -> ResultadoComando:
        if comando == "soffice":
            return _crear_pdf(comando, argumentos)
        return ResultadoComando(codigo=1, stderr="pdf dañado")

    stub = StubSandbox([responder, responder])

    with pytest.raises(ConversionError) as excinfo:
        documento_miniaturas(origen, salida, runner=stub)

    assert excinfo.value.codigo == E_CON_PDFTOPPM_FALLO
    assert not list(salida.glob("diapositiva-*.png"))


def test_pdftoppm_sin_miniaturas(tmp_path: Path) -> None:
    origen = _crear_pptx(tmp_path / "presentacion.pptx")
    salida = tmp_path / "miniaturas"

    def responder(comando: str, argumentos: tuple[str, ...]) -> ResultadoComando:
        if comando == "soffice":
            return _crear_pdf(comando, argumentos)
        return ResultadoComando(codigo=0)

    stub = StubSandbox([responder, responder])

    with pytest.raises(ConversionError) as excinfo:
        documento_miniaturas(origen, salida, runner=stub)

    assert excinfo.value.codigo == E_CON_SIN_MINIATURAS


def test_pdftoppm_no_disponible(tmp_path: Path) -> None:
    origen = _crear_pptx(tmp_path / "presentacion.pptx")
    salida = tmp_path / "miniaturas"

    def responder(comando: str, argumentos: tuple[str, ...]) -> ResultadoComando:
        if comando == "soffice":
            return _crear_pdf(comando, argumentos)
        raise FileNotFoundError("pdftoppm")

    stub = StubSandbox([responder, responder])

    with pytest.raises(ConversionError) as excinfo:
        documento_miniaturas(origen, salida, runner=stub)

    assert excinfo.value.codigo == E_CON_PDFTOPPM_NO_DISPONIBLE


def test_salida_miniaturas_es_archivo(tmp_path: Path) -> None:
    origen = _crear_pptx(tmp_path / "presentacion.pptx")
    salida = tmp_path / "miniaturas"
    salida.write_text("no soy un directorio", encoding="utf-8")
    stub = StubSandbox([])

    with pytest.raises(ConversionError) as excinfo:
        documento_miniaturas(origen, salida, runner=stub)

    assert excinfo.value.codigo == E_CON_DIRECTORIO_INVALIDO
    assert stub.llamadas == []


def test_miniaturas_propaga_error_de_conversion(tmp_path: Path) -> None:
    origen = _crear_pptx(tmp_path / "presentacion.pptx")
    stub = StubSandbox([ResultadoComando(codigo=1, stderr="fallo")])

    with pytest.raises(ConversionError) as excinfo:
        documento_miniaturas(origen, tmp_path / "miniaturas", runner=stub)

    assert excinfo.value.codigo == E_CON_SOFFICE_FALLO


def test_miniaturas_sin_runner(tmp_path: Path) -> None:
    origen = _crear_pptx(tmp_path / "presentacion.pptx")

    with pytest.raises(ConversionError) as excinfo:
        documento_miniaturas(origen, tmp_path / "miniaturas")

    assert excinfo.value.codigo == E_CON_SANDBOX
