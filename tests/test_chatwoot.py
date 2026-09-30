import json
from datetime import UTC, datetime

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
    assert mensaje.adjuntos == []


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
    assert mensaje.adjuntos == []


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


def test_adjuntos_se_parsean_sin_data_url() -> None:
    """R29 y R52: el adjunto lleva id, tipo, extension y tamano; el data_url no entra al modelo."""
    url = "https://chatwoot.local/rails/active_storage/blobs/redirect/secreto-firmado"
    adjunto = {"id": 7, "file_type": "image", "extension": None, "file_size": 2048, "data_url": url}
    mensaje = parsear_evento(json.dumps(_payload(content=None, attachments=[adjunto])).encode())
    assert mensaje is not None
    assert [a.model_dump() for a in mensaje.adjuntos] == [
        {"id": 7, "tipo": "image", "extension": None, "tamano": 2048}
    ]
    assert "secreto-firmado" not in repr(mensaje)


def test_adjunto_sin_id_valido_se_descarta() -> None:
    """R51: un adjunto sin id (o con basura) se descarta sin tumbar el parseo del resto."""
    crudos = [{"file_type": "image"}, {"id": "x"}, "basura", {"id": 3, "file_type": "file"}]
    mensaje = parsear_evento(json.dumps(_payload(attachments=crudos)).encode())
    assert mensaje is not None
    assert [a.id for a in mensaje.adjuntos] == [3]


def test_creado_en_utc_y_none_si_falta_o_es_invalido() -> None:
    """R29: created_at sale en UTC; si falta o es invalido queda None (se usa la hora de llegada)."""
    ok = parsear_evento(json.dumps(_payload(created_at="2026-09-27T21:05:27.700Z")).encode())
    assert ok is not None
    assert ok.creado == datetime(2026, 9, 27, 21, 5, 27, 700000, tzinfo=UTC)
    for valor in (None, "ayer", 12, ""):
        mensaje = parsear_evento(json.dumps(_payload(created_at=valor)).encode())
        assert mensaje is not None
        assert mensaje.creado is None
