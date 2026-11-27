from __future__ import annotations

from dataclasses import dataclass

import pytest

from basecards.presentaciones.nodos.redactar import RedaccionError, redactar_seccion


@dataclass
class Respuesta:
    content: str


class LLMFalso:
    def __init__(self, respuestas: list[str]) -> None:
        self.respuestas = iter(respuestas)
        self.llamadas: list[dict] = []

    def invoke(self, messages, **kwargs):
        self.llamadas.append({"messages": messages, **kwargs})
        return Respuesta(next(self.respuestas))


def test_redactar_reintenta_cifras_y_envia_num_ctx() -> None:
    llm = LLMFalso(
        [
            "La tasa observada es 2.34%.",
            "La tasa observada es {{m.tasa_ncl.202401.MOB12|pct2}}.",
        ]
    )

    texto = redactar_seccion(
        "Explicar NCL.",
        {"{{m.tasa_ncl.202401.MOB12|pct2}}": 0.0234},
        llm=llm,
        num_ctx=32768,
    )

    assert "{{m.tasa_ncl.202401.MOB12|pct2}}" in texto
    assert len(llm.llamadas) == 2
    assert all(llamada["options"] == {"num_ctx": 32768} for llamada in llm.llamadas)


def test_redactar_emite_e_red_002_al_segundo_fallo() -> None:
    llm = LLMFalso(["La tasa es 2.34%.", "El saldo es $1,000."])

    with pytest.raises(RedaccionError, match="E-RED-002"):
        redactar_seccion("Explicar el hallazgo.", {}, llm=llm, num_ctx=16384)

    assert len(llm.llamadas) == 2
