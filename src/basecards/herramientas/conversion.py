"""Conversión de presentaciones y generación de miniaturas en el sandbox.

Las dos herramientas de este módulo son deterministas y no usan el LLM:
``documento_convertir`` convierte un ``.pptx`` ya aprobado a ``.pdf`` con
LibreOffice (``soffice --headless``) y ``documento_miniaturas`` genera una
imagen PNG por diapositiva con ``pdftoppm``.

Los comandos no se ejecutan en el proceso del agente. Se envían a un
``SandboxRunner``, que en producción es el cliente de ``sandbox-runner``: un
contenedor efímero con ``--network none``, sin privilegios y con el workspace
montado en ``/workspace``. Si no se inyecta un runner, la herramienta falla con
``E-CON-005`` en lugar de ejecutar LibreOffice en el host (ADR-10).
"""

from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

TIMEOUT_CONVERSION_S = 120.0
RESOLUCION_MINIATURAS_DPI = 80
PATRON_MINIATURA = re.compile(r"^diapositiva-(\d+)\.png$")


class ConversionError(Exception):
    """Error de conversión con un código estable ``E-CON-*``."""

    def __init__(self, codigo: str, mensaje: str, ruta: str = "") -> None:
        super().__init__(f"{codigo}: {mensaje}")
        self.codigo = codigo
        self.mensaje = mensaje
        self.ruta = ruta


# Códigos de error estables
E_CON_ARCHIVO_INEXISTENTE = "E-CON-001"
E_CON_ARCHIVO_CORRUPTO = "E-CON-002"
E_CON_SOFFICE_FALLO = "E-CON-003"
E_CON_PDF_VACIO = "E-CON-004"
E_CON_SANDBOX = "E-CON-005"
E_CON_TIMEOUT = "E-CON-006"
E_CON_SOFFICE_NO_DISPONIBLE = "E-CON-007"
E_CON_SALIDA_INVALIDA = "E-CON-008"
E_CON_DIRECTORIO_INVALIDO = "E-CON-010"
E_CON_PDFTOPPM_FALLO = "E-CON-011"
E_CON_PDFTOPPM_NO_DISPONIBLE = "E-CON-012"
E_CON_SIN_MINIATURAS = "E-CON-013"
E_CON_RENOMBRADO_FALLO = "E-CON-014"


@dataclass(frozen=True)
class ResultadoComando:
    """Resultado de un comando ejecutado en el sandbox.

    Attributes:
        codigo: Código de salida del proceso.
        stdout: Salida estándar, ya truncada por el sandbox-runner.
        stderr: Salida de error, ya truncada por el sandbox-runner.
    """

    codigo: int
    stdout: str = ""
    stderr: str = ""


class SandboxRunner(Protocol):
    """Ejecuta comandos permitidos dentro del sandbox sin red.

    El adaptador real es el cliente de ``sandbox-runner``. Debe:

    - lanzar el contenedor con ``--network none``;
    - montar solo el workspace del usuario en ``/workspace``;
    - traducir las rutas del host a ``/workspace``;
    - aceptar solo ``soffice`` y ``pdftoppm`` (lista blanca del Manual §4.3.3);
    - devolver el código de salida y la salida truncada;
    - lanzar ``TimeoutError`` si se agota ``timeout_s``.
    """

    def ejecutar(
        self,
        comando: str,
        argumentos: Sequence[str],
        *,
        timeout_s: float = TIMEOUT_CONVERSION_S,
    ) -> ResultadoComando:
        """Ejecuta ``comando`` con ``argumentos`` y devuelve su resultado."""
        ...


def _resolver_runner(runner: SandboxRunner | None) -> SandboxRunner:
    if runner is None:
        raise ConversionError(
            E_CON_SANDBOX,
            "no se inyectó un SandboxRunner; la conversión solo puede ejecutarse "
            "en el sandbox sin red (ADR-10)",
        )
    return runner


def _resumen_salida(texto: str, limite: int = 500) -> str:
    limpio = " ".join(texto.split())
    return limpio[:limite] if limpio else "sin salida"


def _ejecutar(
    runner: SandboxRunner,
    comando: str,
    argumentos: Sequence[str],
    *,
    timeout_s: float,
    codigo_fallo: str,
    codigo_no_disponible: str,
) -> ResultadoComando:
    try:
        resultado = runner.ejecutar(comando, argumentos, timeout_s=timeout_s)
    except TimeoutError as exc:
        raise ConversionError(
            E_CON_TIMEOUT,
            f"{comando} excedió el timeout de {timeout_s:g} s",
        ) from exc
    except FileNotFoundError as exc:
        raise ConversionError(
            codigo_no_disponible,
            f"{comando} no está disponible en el sandbox: {exc}",
        ) from exc
    except ConversionError:
        raise
    except Exception as exc:
        raise ConversionError(
            E_CON_SANDBOX,
            f"el sandbox no pudo ejecutar {comando}: {type(exc).__name__}: {exc}",
        ) from exc

    if resultado.codigo == 127:
        raise ConversionError(
            codigo_no_disponible,
            f"{comando} no está disponible en el sandbox (código 127)",
        )
    if resultado.codigo != 0:
        detalle = _resumen_salida(resultado.stderr or resultado.stdout)
        raise ConversionError(
            codigo_fallo,
            f"{comando} terminó con código {resultado.codigo}: {detalle}",
        )
    return resultado


def _validar_pptx(ruta_pptx: Path | str) -> Path:
    ruta = Path(ruta_pptx).expanduser()
    if not ruta.exists():
        raise ConversionError(
            E_CON_ARCHIVO_INEXISTENTE,
            f"no existe el archivo {ruta}",
            str(ruta),
        )
    if not ruta.is_file():
        raise ConversionError(
            E_CON_ARCHIVO_INEXISTENTE,
            f"{ruta} no es un archivo",
            str(ruta),
        )
    if ruta.suffix.lower() != ".pptx":
        raise ConversionError(
            E_CON_ARCHIVO_CORRUPTO,
            f"se esperaba un .pptx: {ruta}",
            str(ruta),
        )
    try:
        with ruta.open("rb") as archivo:
            cabecera = archivo.read(4)
    except OSError as exc:
        raise ConversionError(
            E_CON_ARCHIVO_CORRUPTO,
            f"no se pudo leer {ruta}: {exc}",
            str(ruta),
        ) from exc
    if cabecera != b"PK\x03\x04":
        raise ConversionError(
            E_CON_ARCHIVO_CORRUPTO,
            f"{ruta} no es un .pptx válido (falta la firma ZIP)",
            str(ruta),
        )
    try:
        with zipfile.ZipFile(ruta) as paquete:
            nombres = set(paquete.namelist())
    except (OSError, zipfile.BadZipFile) as exc:
        raise ConversionError(
            E_CON_ARCHIVO_CORRUPTO,
            f"{ruta} está corrupto: {exc}",
            str(ruta),
        ) from exc
    requeridos = {"[Content_Types].xml", "ppt/presentation.xml"}
    if not requeridos <= nombres:
        faltantes = sorted(requeridos - nombres)
        raise ConversionError(
            E_CON_ARCHIVO_CORRUPTO,
            f"{ruta} no parece una presentación .pptx (faltan {faltantes})",
            str(ruta),
        )
    return ruta.resolve()


def documento_convertir(
    ruta_pptx: Path | str,
    salida_pdf: Path | str,
    *,
    runner: SandboxRunner | None = None,
    timeout_s: float = TIMEOUT_CONVERSION_S,
) -> None:
    """Convierte un ``.pptx`` aprobado a ``.pdf`` con LibreOffice en el sandbox.

    Parameters
    ----------
    ruta_pptx:
        Presentación de origen. Debe existir, ser legible y ser un ``.pptx``
        válido.
    salida_pdf:
        Ruta del PDF a generar. Su directorio se crea si no existe.
    runner:
        Cliente del sandbox sin red. Si se omite, se lanza ``E-CON-005``.
    timeout_s:
        Tiempo máximo de la conversión. El Manual Técnico fija 120 s.

    Raises
    ------
    ConversionError
        Con un código ``E-CON-*`` si el archivo no es válido, si el sandbox
        falla, si hay timeout o si el PDF resultante no existe o está vacío.
    """
    origen = _validar_pptx(ruta_pptx)
    destino = Path(salida_pdf).expanduser()
    if destino.suffix.lower() != ".pdf":
        raise ConversionError(
            E_CON_SALIDA_INVALIDA,
            f"la salida debe terminar en .pdf: {destino}",
            str(destino),
        )
    destino = destino.resolve()
    ejecutor = _resolver_runner(runner)

    try:
        destino.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConversionError(
            E_CON_SALIDA_INVALIDA,
            f"no se pudo crear el directorio de salida {destino.parent}: {exc}",
            str(destino),
        ) from exc

    argumentos = [
        "--headless",
        "--convert-to",
        "pdf",
        "--outdir",
        str(destino.parent),
        str(origen),
    ]
    _ejecutar(
        ejecutor,
        "soffice",
        argumentos,
        timeout_s=timeout_s,
        codigo_fallo=E_CON_SOFFICE_FALLO,
        codigo_no_disponible=E_CON_SOFFICE_NO_DISPONIBLE,
    )

    generado = destino.parent / f"{origen.stem}.pdf"
    if not generado.is_file() or generado.stat().st_size == 0:
        raise ConversionError(
            E_CON_PDF_VACIO,
            f"soffice no generó un PDF válido en {generado}",
            str(generado),
        )
    if generado != destino:
        try:
            generado.replace(destino)
        except OSError as exc:
            raise ConversionError(
                E_CON_SALIDA_INVALIDA,
                f"no se pudo mover el PDF a {destino}: {exc}",
                str(destino),
            ) from exc
    if not destino.is_file() or destino.stat().st_size == 0:
        raise ConversionError(
            E_CON_PDF_VACIO,
            f"el PDF final {destino} está vacío o no existe",
            str(destino),
        )


def _limpiar_miniaturas(directorio: Path, *, silencioso: bool = False) -> None:
    for ruta in directorio.glob("diapositiva-*.png"):
        try:
            ruta.unlink()
        except OSError as exc:
            if silencioso:
                continue
            raise ConversionError(
                E_CON_DIRECTORIO_INVALIDO,
                f"no se pudo limpiar la miniatura previa {ruta}: {exc}",
                str(ruta),
            ) from exc


def _renombrar_miniaturas(directorio: Path) -> list[Path]:
    encontradas: list[tuple[int, Path]] = []
    for ruta in directorio.glob("diapositiva-*.png"):
        coincidencia = PATRON_MINIATURA.match(ruta.name)
        if coincidencia is not None:
            encontradas.append((int(coincidencia.group(1)), ruta))
    if not encontradas:
        raise ConversionError(
            E_CON_SIN_MINIATURAS,
            f"pdftoppm no generó miniaturas en {directorio}",
            str(directorio),
        )

    encontradas.sort(key=lambda par: par[0])
    ancho = max(2, len(str(len(encontradas))))
    resultado: list[Path] = []
    for indice, (_, ruta) in enumerate(encontradas, start=1):
        final = directorio / f"diapositiva-{indice:0{ancho}d}.png"
        if ruta != final:
            try:
                ruta.replace(final)
            except OSError as exc:
                raise ConversionError(
                    E_CON_RENOMBRADO_FALLO,
                    f"no se pudo renombrar {ruta} a {final}: {exc}",
                    str(ruta),
                ) from exc
        resultado.append(final)
    return resultado


def documento_miniaturas(
    ruta_pptx: Path | str,
    salida_dir: Path | str,
    *,
    runner: SandboxRunner | None = None,
    timeout_s: float = TIMEOUT_CONVERSION_S,
) -> list[Path]:
    """Genera una miniatura PNG por diapositiva y devuelve sus rutas.

    Primero convierte el ``.pptx`` a PDF con :func:`documento_convertir` en un
    directorio temporal y después ejecuta ``pdftoppm`` en el sandbox. El PDF
    intermedio se elimina al terminar; el directorio de salida queda solo con
    los PNG.

    Parameters
    ----------
    ruta_pptx:
        Presentación de origen. Debe existir, ser legible y ser un ``.pptx``
        válido.
    salida_dir:
        Directorio donde se escriben ``diapositiva-01.png``,
        ``diapositiva-02.png``, etc. Se crea si no existe.
    runner:
        Cliente del sandbox sin red. Si se omite, se lanza ``E-CON-005``.
    timeout_s:
        Tiempo máximo de cada comando (conversión y miniaturas).

    Returns
    -------
    list[Path]
        Rutas ordenadas de las miniaturas generadas.

    Raises
    ------
    ConversionError
        Con un código ``E-CON-*`` si la conversión o la generación fallan.
    """
    origen = _validar_pptx(ruta_pptx)
    destino_dir = Path(salida_dir).expanduser()
    if destino_dir.exists() and not destino_dir.is_dir():
        raise ConversionError(
            E_CON_DIRECTORIO_INVALIDO,
            f"{destino_dir} existe y no es un directorio",
            str(destino_dir),
        )
    ejecutor = _resolver_runner(runner)

    try:
        destino_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConversionError(
            E_CON_DIRECTORIO_INVALIDO,
            f"no se pudo crear el directorio {destino_dir}: {exc}",
            str(destino_dir),
        ) from exc
    destino_dir = destino_dir.resolve()

    try:
        temporal = Path(tempfile.mkdtemp(prefix=".basecards-miniaturas-", dir=destino_dir))
    except OSError as exc:
        raise ConversionError(
            E_CON_DIRECTORIO_INVALIDO,
            f"no se pudo crear el directorio temporal en {destino_dir}: {exc}",
            str(destino_dir),
        ) from exc
    try:
        # El sandbox corre con otro UID; el temporal debe ser escribible por él.
        temporal.chmod(0o777)
    except OSError as exc:
        shutil.rmtree(temporal, ignore_errors=True)
        raise ConversionError(
            E_CON_DIRECTORIO_INVALIDO,
            f"no se pudieron ajustar los permisos de {temporal}: {exc}",
            str(temporal),
        ) from exc

    pdf = temporal / f"{origen.stem}.pdf"
    prefijo = destino_dir / "diapositiva"
    try:
        documento_convertir(origen, pdf, runner=ejecutor, timeout_s=timeout_s)
        _limpiar_miniaturas(destino_dir)
        argumentos = [
            "-png",
            "-r",
            str(RESOLUCION_MINIATURAS_DPI),
            str(pdf),
            str(prefijo),
        ]
        try:
            _ejecutar(
                ejecutor,
                "pdftoppm",
                argumentos,
                timeout_s=timeout_s,
                codigo_fallo=E_CON_PDFTOPPM_FALLO,
                codigo_no_disponible=E_CON_PDFTOPPM_NO_DISPONIBLE,
            )
        except ConversionError:
            _limpiar_miniaturas(destino_dir, silencioso=True)
            raise
        try:
            return _renombrar_miniaturas(destino_dir)
        except ConversionError:
            _limpiar_miniaturas(destino_dir, silencioso=True)
            raise
    finally:
        shutil.rmtree(temporal, ignore_errors=True)
