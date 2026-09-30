import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.config import RAIZ, ConfigNegocio, cargar_config

PREFIJOS_CON_CREDENCIALES = ("CHATWOOT_", "ANTHROPIC_", "GOOGLE_", "SHEET_")
TELEFONO = "+54 9 11 5555-0000"
# Martes hábil, dentro del horario de Imprenta Ejemplo
HORA_DE_PRUEBA = datetime(2026, 10, 6, 10, 0, tzinfo=ZoneInfo("America/Argentina/Buenos_Aires"))


@pytest.fixture
def config() -> ConfigNegocio:
    """Imprenta Ejemplo con el reloj fijo en HORA_DE_PRUEBA; cada test lo mueve con fijar_ahora."""
    config = cargar_config(RAIZ / "config" / "negocio.ejemplo.json")
    config.fijar_ahora(HORA_DE_PRUEBA)
    return config


@pytest.fixture(autouse=True)
def sin_credenciales_reales(monkeypatch: pytest.MonkeyPatch) -> None:
    """Los tests corren sin red ni credenciales: se borran las del entorno de quien los corre."""
    for nombre in list(os.environ):
        if nombre.startswith(PREFIJOS_CON_CREDENCIALES):
            monkeypatch.delenv(nombre)


def _payload(**cambios: object) -> dict[str, object]:
    """Payload sintetico de un webhook de Chatwoot (message_created, incoming, no privado)."""
    base: dict[str, object] = {
        "event": "message_created",
        "message_type": "incoming",
        "private": False,
        "id": 101,
        "content": "hola, quiero tarjetas",
        "attachments": [],
        "conversation": {"id": 555, "status": "open"},
        "account": {"id": 1},
        "inbox": {"id": 2},
        "sender": {"name": "Cliente Prueba", "phone_number": TELEFONO},
    }
    base.update(cambios)
    return base
