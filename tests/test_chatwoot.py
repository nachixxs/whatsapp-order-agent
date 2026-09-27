import json

import httpx
import pytest

from app.chatwoot import ClienteChatwoot, parsear_evento
from tests.conftest import _payload


def test_parsear_evento_mensaje_incoming_completo() -> None:
    """R48: un message_created incoming, no privado, se convierte en MensajeEntrante."""
    mensaje = parsear_evento(json.dumps(_payload()).encode("utf-8"))
    assert mensaje is not None
    assert mensaje.contenido == "hola, quiero tarjetas"
    assert mensaje.id_conversacion == 555
    assert mensaje.account_id == 1
    assert mensaje.inbox_id == 2
    assert mensaje.cantidad_adjuntos == 0


def test_parsear_evento_surrogate_en_el_contenido() -> None:
    """R51: un texto con un surrogate suelto se sanea en vez de lanzar."""
    payload = _payload(content="hola\ud83d")
    mensaje = parsear_evento(json.dumps(payload).encode("utf-8", "surrogatepass"))
    assert mensaje is not None
    mensaje.contenido.encode("utf-8")  # no debe lanzar UnicodeEncodeError


def test_parsear_evento_tipos_inesperados_en_campos() -> None:
    """R51: un campo con un tipo inesperado (conversation como string) no tumba el parseo."""
    payload = _payload(conversation="no es un dict", attachments="tampoco")
    mensaje = parsear_evento(json.dumps(payload).encode("utf-8"))
    assert mensaje is not None
    assert mensaje.id_conversacion is None
    assert mensaje.cantidad_adjuntos == 0


def test_responder_sin_credenciales_no_lanza(caplog: pytest.LogCaptureFixture) -> None:
    """R53: sin credenciales configuradas, no lanza hacia el endpoint y no loguea el token."""
    cliente = ClienteChatwoot()
    with caplog.at_level("ERROR"):
        cliente.responder(555, "Eco: hola")
    assert "faltan credenciales" in caplog.text
    assert "hola" not in caplog.text


def test_responder_con_credenciales_usa_el_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """El cliente manda el token por header, nunca por otro lado."""
    monkeypatch.setenv("CHATWOOT_URL", "http://chatwoot.local")
    monkeypatch.setenv("CHATWOOT_ACCOUNT_ID", "1")
    monkeypatch.setenv("CHATWOOT_BOT_TOKEN", "secreto-de-prueba")
    llamadas: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        llamadas.append(request)
        return httpx.Response(200, json={"id": 1})

    cliente = ClienteChatwoot(cliente_http=httpx.Client(transport=httpx.MockTransport(_handler)))
    cliente.responder(555, "Eco: hola")

    assert len(llamadas) == 1
    assert llamadas[0].headers["api_access_token"] == "secreto-de-prueba"
    assert llamadas[0].url.path == "/api/v1/accounts/1/conversations/555/messages"


def test_responder_fallo_de_red_no_lanza_ni_loguea_el_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """R53: un fallo de red se loguea sin el token y no se propaga."""
    monkeypatch.setenv("CHATWOOT_URL", "http://chatwoot.local")
    monkeypatch.setenv("CHATWOOT_ACCOUNT_ID", "1")
    monkeypatch.setenv("CHATWOOT_BOT_TOKEN", "secreto-de-prueba")

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no hay red", request=request)

    cliente = ClienteChatwoot(cliente_http=httpx.Client(transport=httpx.MockTransport(_handler)))
    with caplog.at_level("ERROR"):
        cliente.responder(555, "Eco: hola")  # no debe lanzar
    assert "secreto-de-prueba" not in caplog.text
