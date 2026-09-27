import os

import pytest

PREFIJOS_CON_CREDENCIALES = ("CHATWOOT_", "ANTHROPIC_", "GOOGLE_")


@pytest.fixture(autouse=True)
def sin_credenciales_reales(monkeypatch: pytest.MonkeyPatch) -> None:
    """Los tests corren sin red ni credenciales: se borran las del entorno de quien los corre."""
    for nombre in list(os.environ):
        if nombre.startswith(PREFIJOS_CON_CREDENCIALES):
            monkeypatch.delenv(nombre)
