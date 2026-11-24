"""Pruebas del cargador y validador de skills."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from basecards.herramientas.skills import (
    SkillError,
    skill_hash_contenido,
    skill_validar,
    skills_leer,
    skills_listar,
)

RAIZ_REPO = Path(__file__).resolve().parents[2]
SKILL = RAIZ_REPO / "skills" / "aprobadas" / "seguimiento-cosechas"


@pytest.fixture
def copia(tmp_path: Path) -> Path:
    destino = tmp_path / "seguimiento-cosechas"
    shutil.copytree(SKILL, destino)
    return destino


def _codigos(resultado) -> set[str]:
    return {hallazgo.codigo for hallazgo in resultado.errores}


def _reemplazar(ruta: Path, viejo: str, nuevo: str) -> None:
    texto = ruta.read_text(encoding="utf-8")
    assert viejo in texto, f"no se encontró {viejo!r} en {ruta.name}"
    ruta.write_text(texto.replace(viejo, nuevo, 1), encoding="utf-8")


def _editar_metadata(copia: Path, **cambios) -> None:
    ruta = copia / "metadata.json"
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    datos.update(cambios)
    ruta.write_text(json.dumps(datos, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Skill real
# ---------------------------------------------------------------------------


def test_skill_real_es_valida() -> None:
    resultado = skill_validar(SKILL)
    assert resultado.valido, resultado.resumen()
    assert resultado.errores == []
    assert {h.codigo for h in resultado.advertencias} == {"W-SKL-100", "W-SKL-101"}


def test_hash_contenido_es_estable(copia: Path) -> None:
    assert skill_hash_contenido(copia) == skill_hash_contenido(SKILL)


# ---------------------------------------------------------------------------
# Estructura y encabezado
# ---------------------------------------------------------------------------


def test_falta_archivo_obligatorio(copia: Path) -> None:
    (copia / "charts.yaml").unlink()
    assert "E-SKL-001" in _codigos(skill_validar(copia))


def test_nombre_no_coincide_con_carpeta(copia: Path) -> None:
    _reemplazar(copia / "SKILL.md", "name: seguimiento-cosechas", "name: otra-skill")
    assert "E-SKL-002" in _codigos(skill_validar(copia))


def test_herramienta_desconocida(copia: Path) -> None:
    _reemplazar(copia / "SKILL.md", "pptx_ensamblar", "pptx_inventado")
    assert "E-SKL-004" in _codigos(skill_validar(copia))


# ---------------------------------------------------------------------------
# schema.json
# ---------------------------------------------------------------------------


def test_schema_no_cumple_meta_esquema(copia: Path) -> None:
    _reemplazar(copia / "schema.json", '"rol": "dimension"', '"rol": "otro"')
    assert "E-SKL-010" in _codigos(skill_validar(copia))


def test_llave_inexistente(copia: Path) -> None:
    _reemplazar(copia / "schema.json", '"VINTAGE",\n    "STRATEGY"', '"NO_EXISTE",\n    "STRATEGY"')
    assert "E-SKL-011" in _codigos(skill_validar(copia))


# ---------------------------------------------------------------------------
# metrics.yaml
# ---------------------------------------------------------------------------


def test_metrica_usa_columna_inexistente(copia: Path) -> None:
    _reemplazar(
        copia / "metrics.yaml", "numerador: {sum: OVER30ACCTS}", "numerador: {sum: FANTASMA}"
    )
    assert "E-SKL-021" in _codigos(skill_validar(copia))


def test_expresion_fuera_de_gramatica(copia: Path) -> None:
    _reemplazar(
        copia / "metrics.yaml",
        'expresion: "sum(ACCOUNTS)"',
        'expresion: "__import__(\\"os\\").system(\\"x\\")"',
    )
    assert "E-SKL-022" in _codigos(skill_validar(copia))


def test_formato_desconocido(copia: Path) -> None:
    _reemplazar(copia / "metrics.yaml", "formato: pct2", "formato: porcentaje")
    assert "E-SKL-023" in _codigos(skill_validar(copia))


# ---------------------------------------------------------------------------
# charts.yaml
# ---------------------------------------------------------------------------


def test_grafica_usa_metrica_inexistente(copia: Path) -> None:
    _reemplazar(copia / "charts.yaml", "metrica: volumen", "metrica: volumen_fantasma")
    assert "E-SKL-031" in _codigos(skill_validar(copia))


def test_grafica_de_imagen_sin_matplotlib(copia: Path) -> None:
    _reemplazar(copia / "charts.yaml", "motor: matplotlib", "motor: pptx")
    assert "E-SKL-032" in _codigos(skill_validar(copia))


# ---------------------------------------------------------------------------
# Marcadores e integridad
# ---------------------------------------------------------------------------


def test_marcador_usa_metrica_inexistente(copia: Path) -> None:
    with (copia / "SKILL.md").open("a", encoding="utf-8") as archivo:
        archivo.write("\n{{m.fantasma.202401.MOB12|pct2}}\n")
    assert "E-SKL-040" in _codigos(skill_validar(copia))


def test_mob_fuera_de_rango(copia: Path) -> None:
    with (copia / "SKILL.md").open("a", encoding="utf-8") as archivo:
        archivo.write("\n{{m.tasa_ncl.202401.MOB99|pct2}}\n")
    assert "E-SKL-040" in _codigos(skill_validar(copia))


def test_hash_alterado(copia: Path) -> None:
    _editar_metadata(copia, hash_contenido="sha256:" + "0" * 64)
    assert "E-SKL-051" in _codigos(skill_validar(copia))


def test_version_invalida(copia: Path) -> None:
    _editar_metadata(copia, version="1.0")
    assert "E-SKL-050" in _codigos(skill_validar(copia))


# ---------------------------------------------------------------------------
# skills_listar / skills_leer
# ---------------------------------------------------------------------------


def test_listar_incluye_la_skill_aprobada() -> None:
    resumenes = skills_listar()
    por_nombre = {resumen.nombre: resumen for resumen in resumenes}
    assert "seguimiento-cosechas" in por_nombre
    resumen = por_nombre["seguimiento-cosechas"]
    assert resumen.version == "1.0.0"
    assert resumen.origen == "aprobadas"
    assert resumen.valido
    assert resumen.salidas == ["pptx", "pdf"]


def test_leer_devuelve_los_archivos() -> None:
    datos = skills_leer("seguimiento-cosechas")
    assert datos["origen"] == "aprobadas"
    assert datos["metadata"]["version"] == "1.0.0"
    assert set(datos["archivos"]) == {
        "SKILL.md",
        "schema.json",
        "metrics.yaml",
        "charts.yaml",
        "metadata.json",
    }
    assert datos["validacion"]["valido"] is True


def test_leer_version_inexistente() -> None:
    with pytest.raises(SkillError) as exc:
        skills_leer("seguimiento-cosechas", "9.9.9")
    assert exc.value.codigo == "E-SKL-053"


def test_leer_rechaza_traversal() -> None:
    with pytest.raises(SkillError) as exc:
        skills_leer("../../fuera")
    assert exc.value.codigo == "E-SKL-002"


def test_borrador_ajeno_no_es_visible(tmp_path: Path) -> None:
    base = tmp_path / "skills"
    (base / "aprobadas").mkdir(parents=True)
    shutil.copytree(SKILL, base / "borradores" / "otro" / "seguimiento-cosechas")

    with pytest.raises(SkillError) as exc:
        skills_leer("seguimiento-cosechas", raiz=base, user_id="yo")
    assert exc.value.codigo == "E-SKL-001"

    datos = skills_leer("seguimiento-cosechas", raiz=base, user_id="otro")
    assert datos["origen"] == "borradores"
