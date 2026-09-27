import json

import httpx
import pytest

from app.chatwoot import ClienteChatwoot, parsear_evento

TELEFONO = "+54 9 11 5555-0000"


def _payload(**cambios: object) -> dict[str, object]:
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


def test_parsear_evento_mensaje_incoming_completo() -> None:
    """R48: un message_created incoming, no privado, se convierte en MensajeEntrante."""
    mensaje = parsear_evento(json.dumps(_payload()).encode("utf-8"))
    assert mensaje is not None
    assert mensaje.contenido == "hola, quiero tarjetas"
    assert mensaje.id_conversacion == 555
    assert mensaje.account_id == 1
    assert mensaje.inbox_id == 2
    assert mensaje.cantidad_adjuntos == 0


def test_parsear_evento_outgoing_se_descarta() -> None:
    """R48: un mensaje outgoing (del bot o de una persona) se descarta."""
    payload = _payload(message_type="outgoing")
    assert parsear_evento(json.dumps(payload).encode("utf-8")) is None


def test_parsear_evento_nota_privada_se_descarta() -> None:
    """R48: una nota privada se descarta aunque el evento sea message_created."""
    payload = _payload(private=True)
    assert parsear_evento(json.dumps(payload).encode("utf-8")) is None


def test_parsear_evento_otro_evento_se_descarta() -> None:
    """R48: un evento que no es message_created se descarta."""
    payload = _payload(event="conversation_status_changed")
    assert parsear_evento(json.dumps(payload).encode("utf-8")) is None


def test_parsear_evento_cuenta_adjuntos() -> None:
    """El modelo cuenta los adjuntos sin interpretarlos (base para el eco)."""
    payload = _payload(content="", attachments=[{"id": 1}, {"id": 2}])
    mensaje = parsear_evento(json.dumps(payload).encode("utf-8"))
    assert mensaje is not None
    assert mensaje.cantidad_adjuntos == 2


def test_parsear_evento_json_roto() -> None:
    """R51: un JSON invalido nunca lanza, devuelve None."""
    assert parsear_evento(b"{no es json") is None


def test_parsear_evento_anidado_muy_profundo() -> None:
    """R51: un JSON anidado a proposito (RecursionError) nunca lanza."""
    cuerpo = b"[" * 100_000 + b"]" * 100_000
    assert parsear_evento(cuerpo) is None


def test_parsear_evento_no_es_un_objeto() -> None:
    """R51: un JSON valido pero sin forma de objeto (una lista, un numero) se ignora."""
    assert parsear_evento(b"[1, 2, 3]") is None
    assert parsear_evento(b"42") is None


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
