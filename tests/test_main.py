import hashlib
import hmac
import json
import logging
import socket
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.chatwoot import ClienteChatwoot
from app.config import ConfigNegocio
from app.main import app, get_cliente_chatwoot, get_config, get_memoria, workers_pedidos
from app.memoria import Memoria
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


class TurnoFalso:
    """Reemplaza a procesar_lote: anota los llamados y devuelve `texto` (sin API ni SQLite)."""

    def __init__(self) -> None:
        self.llamados: list[tuple[int, list[object], object, object]] = []
        self.texto: str | None = "respuesta del turno"

    def __call__(self, conversacion: int, mensajes: object, config: object, memoria: object) -> str | None:
        self.llamados.append((conversacion, list(mensajes), config, memoria))  # type: ignore[call-overload]
        return self.texto


@pytest.fixture
def turno_falso(monkeypatch: pytest.MonkeyPatch) -> TurnoFalso:
    falso = TurnoFalso()
    monkeypatch.setattr("app.main.procesar_lote", falso)
    return falso


@pytest.fixture
def memoria(tmp_path: Path) -> Memoria:
    memoria = Memoria(tmp_path / "memoria.db")
    app.dependency_overrides[get_memoria] = lambda: memoria
    yield memoria
    app.dependency_overrides.pop(get_memoria, None)
    memoria.cerrar()


@pytest.fixture
def client(config: ConfigNegocio, memoria: Memoria, turno_falso: TurnoFalso) -> TestClient:
    app.dependency_overrides[get_config] = lambda: config
    yield TestClient(app)  # sin `with`: no corre el lifespan (no toca disco ni el entorno)
    app.dependency_overrides.pop(get_config, None)


def _firmar(cuerpo: bytes, timestamp: str, secreto: str = SECRETO) -> str:
    firmado = timestamp.encode() + b"." + cuerpo
    return "sha256=" + hmac.new(secreto.encode(), firmado, hashlib.sha256).hexdigest()


def _post(
    client: TestClient,
    config: ConfigNegocio,
    cuerpo: bytes,
    desfase: int = 0,
    secreto: str = SECRETO,
) -> httpx.Response:
    """POST firmado como Chatwoot; `desfase` mueve el timestamp respecto de config.ahora()."""
    timestamp = str(int(config.ahora().timestamp()) + desfase)
    cabeceras = {"X-Chatwoot-Timestamp": timestamp, "X-Chatwoot-Signature": _firmar(cuerpo, timestamp, secreto)}
    return client.post("/webhook/chatwoot", content=cuerpo, headers=cabeceras)


def test_webhook_secreto_correcto_procesa(
    client: TestClient,
    config: ConfigNegocio,
    monkeypatch: pytest.MonkeyPatch,
    cliente_falso: ClienteFalso,
    turno_falso: TurnoFalso,
) -> None:
    """R49: con el secreto correcto y account/inbox esperados, el webhook procesa."""
    _configurar_entorno(monkeypatch)
    respuesta = _post(client, config, json.dumps(_payload()).encode())
    assert respuesta.status_code == 200
    assert respuesta.json() == {"estado": "ok"}
    assert len(turno_falso.llamados) == 1


def test_webhook_account_equivocada_se_descarta(
    client: TestClient, config: ConfigNegocio, monkeypatch: pytest.MonkeyPatch, cliente_falso: ClienteFalso
) -> None:
    """R49: un account_id que no coincide con el configurado se descarta."""
    _configurar_entorno(monkeypatch)
    payload = _payload(account={"id": 999})
    respuesta = _post(client, config, json.dumps(payload).encode()
    )
    assert respuesta.json() == {"estado": "ignorado"}
    assert cliente_falso.respuestas == []


def test_webhook_inbox_equivocado_se_descarta(
    client: TestClient, config: ConfigNegocio, monkeypatch: pytest.MonkeyPatch, cliente_falso: ClienteFalso
) -> None:
    """R49: un inbox_id que no coincide con el configurado se descarta."""
    _configurar_entorno(monkeypatch)
    payload = _payload(inbox={"id": 999})
    respuesta = _post(client, config, json.dumps(payload).encode()
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
    config: ConfigNegocio,
    monkeypatch: pytest.MonkeyPatch,
    cliente_falso: ClienteFalso,
    cambios: dict[str, object],
) -> None:
    """R48: outgoing, nota privada y otro evento nunca generan una respuesta."""
    _configurar_entorno(monkeypatch)
    payload = _payload(**cambios)
    respuesta = _post(client, config, json.dumps(payload).encode()
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
    config: ConfigNegocio,
    monkeypatch: pytest.MonkeyPatch,
    cliente_falso: ClienteFalso,
    cuerpo: bytes,
) -> None:
    """R51 y R50: un payload roto o con tipos raros nunca lanza y siempre da 200 sin eco."""
    _configurar_entorno(monkeypatch)
    respuesta = _post(client, config, cuerpo)
    assert respuesta.status_code == 200
    assert cliente_falso.respuestas == []


def test_webhook_mensaje_llega_al_turno_y_su_texto_se_responde(
    client: TestClient,
    config: ConfigNegocio,
    memoria: Memoria,
    monkeypatch: pytest.MonkeyPatch,
    cliente_falso: ClienteFalso,
    turno_falso: TurnoFalso,
) -> None:
    """R47 a R49: un mensaje valido llega a procesar_lote con conversacion, lote de uno, config y memoria."""
    _configurar_entorno(monkeypatch)
    respuesta = _post(client, config, json.dumps(_payload()).encode())
    assert respuesta.json() == {"estado": "ok"}
    [(conversacion, mensajes, config_usada, memoria_usada)] = turno_falso.llamados
    assert conversacion == 555
    assert [(m.id_mensaje, m.contenido) for m in mensajes] == [(101, "hola, quiero tarjetas")]  # type: ignore[attr-defined]
    assert config_usada is config
    assert memoria_usada is memoria
    assert cliente_falso.respuestas == [(555, "respuesta del turno")]


def test_webhook_turno_sin_texto_no_manda_nada(
    client: TestClient,
    config: ConfigNegocio,
    monkeypatch: pytest.MonkeyPatch,
    cliente_falso: ClienteFalso,
    turno_falso: TurnoFalso,
) -> None:
    """Si procesar_lote devuelve None (duplicado, compuerta), no se le escribe al cliente."""
    _configurar_entorno(monkeypatch)
    turno_falso.texto = None
    assert _post(client, config, json.dumps(_payload()).encode()).status_code == 200
    assert len(turno_falso.llamados) == 1
    assert cliente_falso.respuestas == []


def test_webhook_firma_mala_no_llega_al_turno(
    client: TestClient,
    config: ConfigNegocio,
    monkeypatch: pytest.MonkeyPatch,
    cliente_falso: ClienteFalso,
    turno_falso: TurnoFalso,
) -> None:
    """R49: con la firma mala, procesar_lote no se llama (no se escribe memoria ni se gasta API)."""
    _configurar_entorno(monkeypatch)
    respuesta = _post(client, config, json.dumps(_payload()).encode(), secreto="otro-secreto")
    assert respuesta.json() == {"estado": "ignorado"}
    assert turno_falso.llamados == []
    assert cliente_falso.respuestas == []


def test_webhook_adjuntos_sin_texto_no_van_al_turno(
    client: TestClient,
    config: ConfigNegocio,
    monkeypatch: pytest.MonkeyPatch,
    cliente_falso: ClienteFalso,
    turno_falso: TurnoFalso,
) -> None:
    """Hasta la tarea 3.3, un mensaje de solo adjuntos no entra al turno ni recibe respuesta."""
    _configurar_entorno(monkeypatch)
    payload = _payload(content="", attachments=[{"id": 1}, {"id": 2}])
    assert _post(client, config, json.dumps(payload).encode()).status_code == 200
    assert turno_falso.llamados == []
    assert cliente_falso.respuestas == []


def test_webhook_falla_al_responder_se_loguea_sin_valores_y_da_200(
    client: TestClient,
    config: ConfigNegocio,
    monkeypatch: pytest.MonkeyPatch,
    turno_falso: TurnoFalso,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R52 y R50: un error de responder no tumba el webhook y el log lleva el tipo, no el mensaje."""

    class ClienteRoto(ClienteChatwoot):
        def responder(self, id_conversacion: int, texto: str) -> None:
            raise httpx.ConnectError("http://chatwoot.local/conversations/555 token-secreto")

    app.dependency_overrides[get_cliente_chatwoot] = lambda: ClienteRoto()
    _configurar_entorno(monkeypatch)
    try:
        with caplog.at_level(logging.INFO):
            respuesta = _post(client, config, json.dumps(_payload()).encode())
    finally:
        app.dependency_overrides.pop(get_cliente_chatwoot, None)
    assert respuesta.status_code == 200
    texto = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("app."))
    assert "ConnectError" in texto
    assert "token-secreto" not in texto
    assert "555" not in texto


@pytest.mark.parametrize(
    ("argv", "entorno", "esperado"),
    [
        (["uvicorn", "app.main:app"], {}, 1),
        (["uvicorn", "app.main:app", "--workers", "3"], {}, 3),
        (["uvicorn", "app.main:app", "--workers=2"], {}, 2),
        (["gunicorn", "-w", "4", "app.main:app"], {}, 4),
        (["uvicorn", "app.main:app"], {"WEB_CONCURRENCY": "2"}, 2),
        (["uvicorn", "app.main:app", "--workers", "1"], {"WEB_CONCURRENCY": "5"}, 1),  # la CLI le gana
        (["uvicorn", "app.main:app"], {"WEB_CONCURRENCY": ""}, 1),
    ],
)
def test_workers_pedidos(argv: list[str], entorno: dict[str, str], esperado: int) -> None:
    """R28: se lee la cantidad de workers de la linea de comandos y de WEB_CONCURRENCY."""
    assert workers_pedidos(argv, entorno) == esperado


def test_r28_con_mas_de_un_worker_el_servidor_no_arranca(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config: ConfigNegocio
) -> None:
    """R28: el lifespan aborta con mas de un worker y no abre la memoria; con uno, arranca y cierra."""
    monkeypatch.setattr("sys.argv", ["uvicorn", "app.main:app"])
    # negocio.json real esta en .gitignore: el CI solo tiene el ejemplo
    monkeypatch.setattr("app.main.RUTA_POR_DEFECTO", Path(__file__).resolve().parent.parent / "config" / "negocio.ejemplo.json")
    monkeypatch.setenv("MEMORIA_RUTA", str(tmp_path / "m.db"))
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with pytest.raises(RuntimeError, match="R28"), TestClient(app):
        pass
    assert not (tmp_path / "m.db").exists()
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    with TestClient(app):
        assert (tmp_path / "m.db").exists()  # R54: la memoria se abre al arrancar


def test_webhook_body_gigante_se_corta_sin_provocar_reintento(
    client: TestClient, config: ConfigNegocio, monkeypatch: pytest.MonkeyPatch, cliente_falso: ClienteFalso
) -> None:
    """Seguridad: un body de mas de 1 MB se corta con un codigo que Chatwoot no reintenta."""
    _configurar_entorno(monkeypatch)
    payload = _payload(content="a" * 2_000_000)
    respuesta = _post(client, config, json.dumps(payload).encode())
    assert respuesta.status_code not in (429, 500)
    assert cliente_falso.respuestas == []


def test_docs_deshabilitados(client: TestClient) -> None:
    """Seguridad: no se expone documentacion interactiva de la API."""
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_salud_ok_con_config_completa(client: TestClient, config: ConfigNegocio, monkeypatch: pytest.MonkeyPatch) -> None:
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
    client: TestClient, config: ConfigNegocio, monkeypatch: pytest.MonkeyPatch
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
    config: ConfigNegocio,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R52: el secreto no aparece en ningun log, ni con la auth fallida ni con la exitosa."""
    _configurar_entorno(monkeypatch)
    with caplog.at_level(logging.INFO):
        _post(client, config, json.dumps(_payload()).encode())
        _post(client, config, b"{}", secreto="otro-secreto-cualquiera")
    # Solo los logs de la app: el "httpx" del TestClient loguea su propia URL de salida,
    # que no es un log que emita este servidor.
    texto = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("app."))
    assert SECRETO not in texto
    assert "otro-secreto-cualquiera" not in texto
    assert "sha256=" not in texto


def test_webhook_captura_apagada_por_defecto_no_escribe_nada(
    client: TestClient,
    config: ConfigNegocio,
    monkeypatch: pytest.MonkeyPatch,
    cliente_falso: ClienteFalso,
    tmp_path: pytest.TempPathFactory,
) -> None:
    """La captura para la 1.6 esta apagada por defecto: no crea el archivo."""
    _configurar_entorno(monkeypatch)
    monkeypatch.setattr("app.main.CARPETA_CAPTURAS", tmp_path)
    archivo = tmp_path / "payloads.jsonl"
    _post(client, config, json.dumps(_payload()).encode())
    assert not archivo.exists()


def test_webhook_captura_encendida_agrega_una_linea(
    client: TestClient,
    config: ConfigNegocio,
    monkeypatch: pytest.MonkeyPatch,
    cliente_falso: ClienteFalso,
    tmp_path: pytest.TempPathFactory,
) -> None:
    """Con CHATWOOT_CAPTURAR_PAYLOADS encendido, el cuerpo crudo se agrega a payloads.jsonl."""
    _configurar_entorno(monkeypatch)
    monkeypatch.setenv("CHATWOOT_CAPTURAR_PAYLOADS", "1")
    monkeypatch.setattr("app.main.CARPETA_CAPTURAS", tmp_path)
    archivo = tmp_path / "payloads.jsonl"
    _post(client, config, json.dumps(_payload()).encode())
    lineas = archivo.read_text(encoding="utf-8").splitlines()
    assert len(lineas) == 1
    assert json.loads(lineas[0])["event"] == "message_created"


def test_webhook_captura_falla_escritura_sigue_devolviendo_200(
    client: TestClient,
    config: ConfigNegocio,
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
        respuesta = _post(client, config, json.dumps(_payload()).encode()
        )
    assert respuesta.status_code == 200
    assert respuesta.json() == {"estado": "ok"}
    assert any("no se pudo escribir la captura" in r.getMessage() for r in caplog.records)


def _cabeceras(cuerpo: bytes, config: ConfigNegocio, desfase: int = 0) -> dict[str, str]:
    timestamp = str(int(config.ahora().timestamp()) + desfase)
    return {"X-Chatwoot-Timestamp": timestamp, "X-Chatwoot-Signature": _firmar(cuerpo, timestamp)}


def _sin_eco(respuesta: httpx.Response, cliente_falso: ClienteFalso) -> None:
    assert respuesta.status_code == 200
    assert respuesta.json() == {"estado": "ignorado"}
    assert cliente_falso.respuestas == []


def test_webhook_firma_buena_procesa(
    client: TestClient, config: ConfigNegocio, monkeypatch: pytest.MonkeyPatch, cliente_falso: ClienteFalso
) -> None:
    """R49: firma buena y timestamp dentro de los 5 minutos (borde incluido) procesa."""
    _configurar_entorno(monkeypatch)
    respuesta = _post(client, config, json.dumps(_payload()).encode(), desfase=-300)
    assert respuesta.json() == {"estado": "ok"}
    assert cliente_falso.respuestas == [(555, "respuesta del turno")]


def test_webhook_sin_firma_no_procesa(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, cliente_falso: ClienteFalso
) -> None:
    """R49: un POST sin X-Chatwoot-Signature ni Timestamp no se procesa."""
    _configurar_entorno(monkeypatch)
    _sin_eco(client.post("/webhook/chatwoot", content=json.dumps(_payload()).encode()), cliente_falso)


def test_webhook_firma_mala_no_procesa(
    client: TestClient, config: ConfigNegocio, monkeypatch: pytest.MonkeyPatch, cliente_falso: ClienteFalso
) -> None:
    """R49: firma hecha con otro secreto, o de otro body, no se procesa."""
    _configurar_entorno(monkeypatch)
    cuerpo = json.dumps(_payload()).encode()
    _sin_eco(_post(client, config, cuerpo, secreto="otro-secreto"), cliente_falso)
    cabeceras = _cabeceras(b"{}", config)  # firma valida, pero de otro body
    _sin_eco(client.post("/webhook/chatwoot", content=cuerpo, headers=cabeceras), cliente_falso)


def test_webhook_firma_sin_prefijo_no_procesa(
    client: TestClient, config: ConfigNegocio, monkeypatch: pytest.MonkeyPatch, cliente_falso: ClienteFalso
) -> None:
    """R49: Chatwoot v4.18.0 firma como 'sha256=<hex>'; el hex pelado no se acepta."""
    _configurar_entorno(monkeypatch)
    cuerpo = json.dumps(_payload()).encode()
    cabeceras = _cabeceras(cuerpo, config)
    cabeceras["X-Chatwoot-Signature"] = cabeceras["X-Chatwoot-Signature"].removeprefix("sha256=")
    _sin_eco(client.post("/webhook/chatwoot", content=cuerpo, headers=cabeceras), cliente_falso)


@pytest.mark.parametrize("desfase", [-301, 301, 3_600, -86_400])
def test_webhook_timestamp_fuera_de_ventana_no_procesa(
    client: TestClient,
    config: ConfigNegocio,
    monkeypatch: pytest.MonkeyPatch,
    cliente_falso: ClienteFalso,
    desfase: int,
) -> None:
    """R49: un timestamp a mas de 5 minutos de config.ahora(), viejo o del futuro, no se procesa."""
    _configurar_entorno(monkeypatch)
    _sin_eco(_post(client, config, json.dumps(_payload()).encode(), desfase=desfase), cliente_falso)


@pytest.mark.parametrize("timestamp", ["", "abc", "12.5", "-5", "١٢٣", "9" * 5_000])
def test_webhook_timestamp_no_numerico_no_procesa(
    client: TestClient,
    config: ConfigNegocio,
    monkeypatch: pytest.MonkeyPatch,
    cliente_falso: ClienteFalso,
    timestamp: str,
) -> None:
    """R49 y R50: un timestamp ausente o no numerico, firmado bien, no se procesa y no da 500."""
    _configurar_entorno(monkeypatch)
    cuerpo = json.dumps(_payload()).encode()
    cabeceras = {
        "X-Chatwoot-Timestamp": timestamp.encode("utf-8"),
        "X-Chatwoot-Signature": _firmar(cuerpo, timestamp).encode(),
    }
    _sin_eco(client.post("/webhook/chatwoot", content=cuerpo, headers=cabeceras), cliente_falso)


def test_webhook_firma_no_ascii_nunca_da_500(
    client: TestClient, config: ConfigNegocio, monkeypatch: pytest.MonkeyPatch, cliente_falso: ClienteFalso
) -> None:
    """R50: un header de firma con caracteres no ASCII se descarta con 200, nunca 500."""
    _configurar_entorno(monkeypatch)
    cuerpo = json.dumps(_payload()).encode()
    cabeceras = _cabeceras(cuerpo, config)
    cabeceras["X-Chatwoot-Signature"] = "sha256=ñ".encode("utf-8")  # type: ignore[assignment]
    _sin_eco(client.post("/webhook/chatwoot", content=cuerpo, headers=cabeceras), cliente_falso)


def test_webhook_secreto_vacio_nunca_coincide(
    client: TestClient, config: ConfigNegocio, monkeypatch: pytest.MonkeyPatch, cliente_falso: ClienteFalso
) -> None:
    """R49: sin CHATWOOT_WEBHOOK_SECRET (o vacio) no coincide, ni con una firma hecha con el vacio."""
    _configurar_entorno(monkeypatch)
    cuerpo = json.dumps(_payload()).encode()
    for valor in (None, ""):
        if valor is None:
            monkeypatch.delenv("CHATWOOT_WEBHOOK_SECRET")
        else:
            monkeypatch.setenv("CHATWOOT_WEBHOOK_SECRET", valor)
        _sin_eco(_post(client, config, cuerpo, secreto=""), cliente_falso)
