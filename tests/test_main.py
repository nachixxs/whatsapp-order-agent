import json
import logging
import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from app.chatwoot import ClienteChatwoot
from app.main import CARPETA_CAPTURAS, _quitar_query, app, get_cliente_chatwoot
from tests.conftest import _payload

SECRETO = "secreto-de-prueba"


class ClienteFalso(ClienteChatwoot):
    def __init__(self) -> None:
        super().__init__()
        self.respuestas: list[tuple[int, str]] = []

    def responder(self, id_conversacion: int, texto: str) -> None:
        self.respuestas.append((id_conversacion, texto))


def _configurar_entorno(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHATWOOT_URL", "http://chatwoot.local")
    monkeypatch.setenv("CHATWOOT_ACCOUNT_ID", "1")
    monkeypatch.setenv("CHATWOOT_INBOX_ID", "2")
    monkeypatch.setenv("CHATWOOT_BOT_TOKEN", "token-de-prueba")
    monkeypatch.setenv("CHATWOOT_WEBHOOK_SECRET", SECRETO)


@pytest.fixture
def cliente_falso() -> ClienteFalso:
    falso = ClienteFalso()
    app.dependency_overrides[get_cliente_chatwoot] = lambda: falso
    yield falso
    app.dependency_overrides.pop(get_cliente_chatwoot, None)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_webhook_secreto_vacio_nunca_coincide(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """R49: un secreto vacio (no configurado) nunca coincide, aunque el token tambien venga vacio."""
    respuesta = client.post("/webhook/chatwoot", content=json.dumps(_payload()).encode())
    assert respuesta.status_code == 200
    assert respuesta.json() == {"estado": "ignorado"}


def test_webhook_token_no_ascii_nunca_200_con_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R50: un token con caracteres no ASCII (o el secreto con tilde) nunca da 500."""
    _configurar_entorno(monkeypatch)
    respuesta = client.post(
        "/webhook/chatwoot",
        params={"token": "ñ"},
        content=json.dumps(_payload()).encode(),
    )
    assert respuesta.status_code == 200
    assert respuesta.json() == {"estado": "ignorado"}


def test_webhook_secreto_incorrecto(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """R49: un secreto que no coincide se descarta, siempre 200."""
    _configurar_entorno(monkeypatch)
    respuesta = client.post(
        "/webhook/chatwoot?token=otra-cosa", content=json.dumps(_payload()).encode()
    )
    assert respuesta.status_code == 200
    assert respuesta.json() == {"estado": "ignorado"}


def test_webhook_secreto_correcto_procesa(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, cliente_falso: ClienteFalso
) -> None:
    """R49: con el secreto correcto y account/inbox esperados, el webhook procesa."""
    _configurar_entorno(monkeypatch)
    respuesta = client.post(
        f"/webhook/chatwoot?token={SECRETO}", content=json.dumps(_payload()).encode()
    )
    assert respuesta.status_code == 200
    assert respuesta.json() == {"estado": "ok"}
    assert cliente_falso.respuestas == [(555, "Eco: hola, quiero tarjetas")]


def test_webhook_account_equivocada_se_descarta(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, cliente_falso: ClienteFalso
) -> None:
    """R49: un account_id que no coincide con el configurado se descarta."""
    _configurar_entorno(monkeypatch)
    payload = _payload(account={"id": 999})
    respuesta = client.post(
        f"/webhook/chatwoot?token={SECRETO}", content=json.dumps(payload).encode()
    )
    assert respuesta.json() == {"estado": "ignorado"}
    assert cliente_falso.respuestas == []


def test_webhook_inbox_equivocado_se_descarta(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, cliente_falso: ClienteFalso
) -> None:
    """R49: un inbox_id que no coincide con el configurado se descarta."""
    _configurar_entorno(monkeypatch)
    payload = _payload(inbox={"id": 999})
    respuesta = client.post(
        f"/webhook/chatwoot?token={SECRETO}", content=json.dumps(payload).encode()
    )
    assert respuesta.json() == {"estado": "ignorado"}
    assert cliente_falso.respuestas == []


@pytest.mark.parametrize(
    "cambios",
    [
        {"message_type": "outgoing"},
        {"private": True},
        {"event": "conversation_status_changed"},
    ],
)
def test_webhook_r48_se_descarta_sin_eco(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    cliente_falso: ClienteFalso,
    cambios: dict[str, object],
) -> None:
    """R48: outgoing, nota privada y otro evento nunca generan un eco."""
    _configurar_entorno(monkeypatch)
    payload = _payload(**cambios)
    respuesta = client.post(
        f"/webhook/chatwoot?token={SECRETO}", content=json.dumps(payload).encode()
    )
    assert respuesta.status_code == 200
    assert respuesta.json() == {"estado": "ignorado"}
    assert cliente_falso.respuestas == []


@pytest.mark.parametrize(
    "cuerpo",
    [
        pytest.param(b"{no es json", id="json_roto"),
        pytest.param(b"[" * 50_000 + b"]" * 50_000, id="anidado_profundo"),
        pytest.param("hola \ud83d".encode("utf-8", "surrogatepass"), id="surrogate"),
        pytest.param(b"[1, 2, 3]", id="no_es_objeto"),
        pytest.param(b"42", id="escalar"),
    ],
)
def test_webhook_r51_payload_roto_siempre_200_sin_eco(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    cliente_falso: ClienteFalso,
    cuerpo: bytes,
) -> None:
    """R51 y R50: un payload roto o con tipos raros nunca lanza y siempre da 200 sin eco."""
    _configurar_entorno(monkeypatch)
    respuesta = client.post(f"/webhook/chatwoot?token={SECRETO}", content=cuerpo)
    assert respuesta.status_code == 200
    assert cliente_falso.respuestas == []


def test_webhook_eco_de_adjuntos(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, cliente_falso: ClienteFalso
) -> None:
    """Un mensaje solo con adjuntos contesta 'Eco: recibi N archivo(s)'."""
    _configurar_entorno(monkeypatch)
    payload = _payload(content="", attachments=[{"id": 1}, {"id": 2}])
    client.post(f"/webhook/chatwoot?token={SECRETO}", content=json.dumps(payload).encode())
    assert cliente_falso.respuestas == [(555, "Eco: recibí 2 archivo(s)")]


def test_webhook_eco_se_corta_a_4096_caracteres(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, cliente_falso: ClienteFalso
) -> None:
    """R35: ningun mensaje de eco supera los 4.096 caracteres."""
    _configurar_entorno(monkeypatch)
    payload = _payload(content="a" * 5_000)
    client.post(f"/webhook/chatwoot?token={SECRETO}", content=json.dumps(payload).encode())
    assert len(cliente_falso.respuestas) == 1
    assert len(cliente_falso.respuestas[0][1]) == 4_096


def test_webhook_body_gigante_se_corta_sin_provocar_reintento(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, cliente_falso: ClienteFalso
) -> None:
    """Seguridad: un body de mas de 1 MB se corta con un codigo que Chatwoot no reintenta."""
    _configurar_entorno(monkeypatch)
    payload = _payload(content="a" * 2_000_000)
    respuesta = client.post(f"/webhook/chatwoot?token={SECRETO}", content=json.dumps(payload).encode())
    assert respuesta.status_code not in (429, 500)
    assert cliente_falso.respuestas == []


def test_docs_deshabilitados(client: TestClient) -> None:
    """Seguridad: no se expone documentacion interactiva de la API."""
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_salud_ok_con_config_completa(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """R55: /salud da 200 cuando la config de Chatwoot esta completa."""
    _configurar_entorno(monkeypatch)
    respuesta = client.get("/salud")
    assert respuesta.status_code == 200
    assert respuesta.json() == {"estado": "ok"}


def test_salud_degradado_sin_detalle(client: TestClient) -> None:
    """R55: /salud da 503 sin decir que falta cuando la config esta incompleta."""
    respuesta = client.get("/salud")
    assert respuesta.status_code == 503
    assert respuesta.json() == {"estado": "degradado"}


def test_salud_no_llama_al_cliente_de_chatwoot(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R55/DoD CP1: /salud no hace ninguna llamada de red, ni siquiera intenta abrir un socket.

    TestClient habla con la app por transporte ASGI (en el mismo proceso), asi que no toca
    sockets reales; si /salud alguna vez llamara a un httpx.Client de verdad (por ejemplo al
    de Chatwoot), este parche lo haria explotar antes de tocar la red.
    """
    _configurar_entorno(monkeypatch)

    def _explota_socket(*args: object, **kwargs: object) -> None:
        raise AssertionError("/salud no deberia abrir ningun socket")

    monkeypatch.setattr(socket, "create_connection", _explota_socket)

    respuesta = client.get("/salud")
    assert respuesta.status_code == 200


def test_webhook_r52_secreto_no_aparece_en_los_logs(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R52: el secreto no aparece en ningun log, ni con la auth fallida ni con la exitosa."""
    _configurar_entorno(monkeypatch)
    with caplog.at_level(logging.INFO):
        client.post(f"/webhook/chatwoot?token={SECRETO}", content=json.dumps(_payload()).encode())
        client.post("/webhook/chatwoot?token=otro-secreto-cualquiera", content=b"{}")
    # Solo los logs de la app: el "httpx" del TestClient loguea su propia URL de salida,
    # que no es un log que emita este servidor.
    texto = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("app."))
    assert SECRETO not in texto
    assert "otro-secreto-cualquiera" not in texto


def test_webhook_captura_apagada_por_defecto_no_escribe_nada(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    cliente_falso: ClienteFalso,
    tmp_path: pytest.TempPathFactory,
) -> None:
    """La captura para la 1.6 esta apagada por defecto: no crea el archivo."""
    _configurar_entorno(monkeypatch)
    monkeypatch.setattr("app.main.CARPETA_CAPTURAS", tmp_path)
    archivo = tmp_path / "payloads.jsonl"
    client.post(f"/webhook/chatwoot?token={SECRETO}", content=json.dumps(_payload()).encode())
    assert not archivo.exists()


def test_webhook_captura_encendida_agrega_una_linea(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    cliente_falso: ClienteFalso,
    tmp_path: pytest.TempPathFactory,
) -> None:
    """Con CHATWOOT_CAPTURAR_PAYLOADS encendido, el cuerpo crudo se agrega a payloads.jsonl."""
    _configurar_entorno(monkeypatch)
    monkeypatch.setenv("CHATWOOT_CAPTURAR_PAYLOADS", "1")
    monkeypatch.setattr("app.main.CARPETA_CAPTURAS", tmp_path)
    archivo = tmp_path / "payloads.jsonl"
    client.post(f"/webhook/chatwoot?token={SECRETO}", content=json.dumps(_payload()).encode())
    lineas = archivo.read_text(encoding="utf-8").splitlines()
    assert len(lineas) == 1
    assert json.loads(lineas[0])["event"] == "message_created"


def test_webhook_captura_falla_escritura_sigue_devolviendo_200(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    cliente_falso: ClienteFalso,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Punto 2: si la captura no puede escribir (OSError), el webhook igual da 200."""
    _configurar_entorno(monkeypatch)
    monkeypatch.setenv("CHATWOOT_CAPTURAR_PAYLOADS", "1")

    class _CarpetaRota:
        def mkdir(self, *args: object, **kwargs: object) -> None:
            raise OSError("disco lleno")

    monkeypatch.setattr("app.main.CARPETA_CAPTURAS", _CarpetaRota())
    with caplog.at_level(logging.WARNING):
        respuesta = client.post(
            f"/webhook/chatwoot?token={SECRETO}", content=json.dumps(_payload()).encode()
        )
    assert respuesta.status_code == 200
    assert respuesta.json() == {"estado": "ok"}
    assert any("no se pudo escribir la captura" in r.getMessage() for r in caplog.records)


def test_quitar_query_filtra_el_token_del_access_log() -> None:
    """R52: el filtro del access log de uvicorn borra la query string (ahi viaja el secreto)."""
    registro = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1", "POST", f"/webhook/chatwoot?token={SECRETO}", "1.1", 200),
        exc_info=None,
    )
    assert _quitar_query(registro) is True
    assert SECRETO not in "".join(str(arg) for arg in registro.args)
    assert registro.args[2] == "/webhook/chatwoot"
