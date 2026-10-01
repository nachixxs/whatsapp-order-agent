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


def _cliente(monkeypatch: pytest.MonkeyPatch, estado: int = 200, error: bool = False) -> tuple[ClienteChatwoot, list[httpx.Request]]:
    monkeypatch.setenv("CHATWOOT_URL", "http://chatwoot.local")
    monkeypatch.setenv("CHATWOOT_ACCOUNT_ID", "1")
    monkeypatch.setenv("CHATWOOT_BOT_TOKEN", "secreto-de-prueba")
    llamadas: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        llamadas.append(request)
        if error:
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(estado, json={})

    return ClienteChatwoot(cliente_http=httpx.Client(transport=httpx.MockTransport(_handler))), llamadas


def test_nota_interna_es_un_mensaje_privado(monkeypatch: pytest.MonkeyPatch) -> None:
    """R47: el aviso al asesor es un mensaje con private=true y message_type outgoing."""
    cliente, llamadas = _cliente(monkeypatch)
    assert cliente.nota_interna(555, "Nota de prueba") is True
    assert llamadas[0].url.path == "/api/v1/accounts/1/conversations/555/messages"
    assert json.loads(llamadas[0].content) == {"content": "Nota de prueba", "message_type": "outgoing", "private": True}


def test_pasar_a_persona_pone_la_conversacion_en_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """R47: derivar es toggle_status con status open."""
    cliente, llamadas = _cliente(monkeypatch)
    assert cliente.pasar_a_persona(555) is True
    assert llamadas[0].url.path == "/api/v1/accounts/1/conversations/555/toggle_status"
    assert json.loads(llamadas[0].content) == {"status": "open"}
    assert llamadas[0].headers["api_access_token"] == "secreto-de-prueba"


@pytest.mark.parametrize("estado, error", [(400, False), (404, False), (500, False), (200, True)])
@pytest.mark.parametrize("accion", ["responder", "nota_interna", "pasar_a_persona"])
def test_fallo_de_chatwoot_da_false_sin_lanzar_ni_loguear_secretos(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, accion: str, estado: int, error: bool
) -> None:
    """R52, R53: 4xx, 5xx o timeout devuelven False; ni el token ni el texto llegan al log."""
    cliente, _ = _cliente(monkeypatch, estado, error)
    llamar = getattr(cliente, accion)
    with caplog.at_level("INFO"):
        resultado = llamar(555) if accion == "pasar_a_persona" else llamar(555, "texto-del-cliente")
    assert resultado is False
    assert "secreto-de-prueba" not in caplog.text
    assert "texto-del-cliente" not in caplog.text


@pytest.mark.parametrize("faltante", ["CHATWOOT_URL", "CHATWOOT_ACCOUNT_ID", "CHATWOOT_BOT_TOKEN"])
def test_falta_una_credencial_da_false_sin_llamar_a_la_red(monkeypatch: pytest.MonkeyPatch, faltante: str) -> None:
    """R53: sin una de las tres credenciales no se hace ningun pedido."""
    cliente, llamadas = _cliente(monkeypatch)
    monkeypatch.delenv(faltante)
    assert cliente.nota_interna(555, "x") is False
    assert cliente.pasar_a_persona(555) is False
    assert cliente.responder(555, "x") is False
    assert llamadas == []


def _con_sender(**sender: object) -> bytes:
    return json.dumps(_payload(sender=sender)).encode()


def test_contacto_trae_id_y_atributos() -> None:
    """R45: el id y los custom_attributes del sender llegan al Contacto."""
    mensaje = parsear_evento(_con_sender(id=77, name="Ana", custom_attributes={"nombre_preguntado": True}))
    assert mensaje is not None
    assert mensaje.contacto.id == 77
    assert mensaje.contacto.atributos == {"nombre_preguntado": True}


@pytest.mark.parametrize("crudo", [{}, {"id": "x", "custom_attributes": None}, {"id": [1], "custom_attributes": "texto"}, {"custom_attributes": [1]}])
def test_contacto_sin_id_o_atributos_validos_da_none_sin_lanzar(crudo: dict[str, object]) -> None:
    """R45, R51: tipos raros dan None (sin dato), nunca una excepcion."""
    mensaje = parsear_evento(_con_sender(**crudo))
    assert mensaje is not None
    assert mensaje.contacto.id is None
    assert mensaje.contacto.atributos is None


def test_actualizar_contacto_hace_put_con_el_token_de_agente_y_5_segundos(monkeypatch: pytest.MonkeyPatch) -> None:
    """R45: PUT del contacto con CHATWOOT_AGENTE_TOKEN (no el del bot) y tope de 5 s."""
    cliente, llamadas = _cliente(monkeypatch)
    monkeypatch.setenv("CHATWOOT_AGENTE_TOKEN", "token-agente-prueba")
    assert cliente.actualizar_contacto(77, {"nombre_cliente": "Ana"}) is True
    assert llamadas[0].method == "PUT"
    assert llamadas[0].url.path == "/api/v1/accounts/1/contacts/77"
    assert json.loads(llamadas[0].content) == {"custom_attributes": {"nombre_cliente": "Ana"}}
    assert llamadas[0].headers["api_access_token"] == "token-agente-prueba"
    assert llamadas[0].extensions["timeout"]["read"] == 5.0


@pytest.mark.parametrize("estado, error", [(400, False), (404, False), (500, False), (200, True)])
def test_actualizar_contacto_fallido_da_false_sin_secretos_en_el_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, estado: int, error: bool
) -> None:
    """R45, R52: 4xx, 5xx o timeout dan False, sin reintento; ni token ni datos al log."""
    cliente, llamadas = _cliente(monkeypatch, estado, error)
    monkeypatch.setenv("CHATWOOT_AGENTE_TOKEN", "token-agente-prueba")
    with caplog.at_level("INFO"):
        assert cliente.actualizar_contacto(77, {"nombre_cliente": "dato-del-cliente"}) is False
    assert len(llamadas) == 1
    assert "token-agente-prueba" not in caplog.text
    assert "dato-del-cliente" not in caplog.text


def test_actualizar_contacto_sin_token_de_agente_da_false_aunque_este_el_del_bot(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """R45: el token del bot no sirve para contactos; sin el de agente no hay red."""
    cliente, llamadas = _cliente(monkeypatch)
    monkeypatch.delenv("CHATWOOT_AGENTE_TOKEN", raising=False)
    with caplog.at_level("INFO"):
        assert cliente.actualizar_contacto(77, {"nombre_cliente": "dato-del-cliente"}) is False
    assert llamadas == []
    assert "dato-del-cliente" not in caplog.text
