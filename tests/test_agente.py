import logging
import os
import subprocess
import sys
from datetime import date, timedelta
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types import Message

from app import agente
from app.agente import (
    ARGUMENTOS_POR_TOOL,
    CLAVE_MAL_FORMADA,
    FALTA_LA_CLAVE,
    MODELO,
    ConfirmarPedido,
    ConsultaGeneral,
    Decision,
    DerivarAAsesor,
    ErrorApi,
    ErrorCredencial,
    PedirDatoFaltante,
    RegistrarPedido,
    SinTool,
    cliente_api,
    decidir,
    historial_para_la_api,
)
from app.config import RAIZ, ConfigNegocio
from app.memoria import Charla, Mensaje
from app.pedidos import Pedido
from app.prompt import bloques_de_sistema
from app.tools import definir_tools
from tests.conftest import TELEFONO

CENTINELA = "Centinela-7Q"  # un valor que nunca puede aparecer en un log
_PEDIDO = Pedido(telefono=TELEFONO, producto="sellos", cantidad=3, fecha_necesita=date(2026, 10, 9))
_REQUEST_HTTP = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
_TODO_NULL = dict.fromkeys(RegistrarPedido.model_fields)


def _charla(*mensajes: tuple[str, str]) -> Charla:
    lista = [Mensaje(role=role, content=content) for role, content in mensajes]  # type: ignore[arg-type]
    return Charla(mensajes=lista)


HOLA = _charla(("user", "hola, quiero sellos"))


def _tool(nombre: str, **argumentos: object) -> dict[str, Any]:
    return {"type": "tool_use", "id": "toolu_prueba", "name": nombre, "input": argumentos}


def _respuesta(*bloques: dict[str, Any], stop_reason: str = "tool_use") -> Message:
    """Una respuesta con la forma de la API, como si se hubiera grabado."""
    return Message.model_validate({
        "id": "msg_prueba", "type": "message", "role": "assistant", "model": MODELO,
        "content": list(bloques), "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 320, "output_tokens": 49,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 3150},
    })


PENSAMIENTO = {"type": "thinking", "thinking": "elijo la tool", "signature": "firma"}
REGISTRAR = _respuesta(PENSAMIENTO, _tool("registrar_pedido", **_TODO_NULL | {"producto": "sellos"}))


class _Mensajes:
    def __init__(self, respuestas: tuple[Message | Exception, ...]) -> None:
        self.respuestas = list(respuestas)
        self.llamadas: list[dict[str, Any]] = []

    def create(self, **request: Any) -> Message:
        self.llamadas.append(request)
        respuesta = self.respuestas.pop(0)
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta


class ClienteFalso:
    """Doble del SDK: devuelve o lanza lo que se le da, en orden, y guarda cada request."""

    def __init__(self, *respuestas: Message | Exception) -> None:
        self.messages = _Mensajes(respuestas)


def _decidir(config: ConfigNegocio, cliente: ClienteFalso, charla: Charla = HOLA, **cambios: Any) -> Any:
    argumentos: dict[str, Any] = {"nombre_perfil": "Ana Prueba", "pedido": None} | cambios
    return decidir(config, config.ahora(), charla, cliente=cliente, **argumentos)  # type: ignore[arg-type]


@pytest.fixture
def sin_cliente(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agente, "_cliente", None)


# ── El request ───────────────────────────────────────────────────────────


def test_el_request_lleva_modelo_tool_choice_thinking_y_effort(config: ConfigNegocio) -> None:
    """R11: sonnet-5-5 con auto sin paralelas (no acepta any), thinking adaptive y effort low."""
    cliente = ClienteFalso(REGISTRAR)
    _decidir(config, cliente)
    (request,) = cliente.messages.llamadas
    assert set(request) == {
        "model", "max_tokens", "thinking", "output_config", "system", "tools", "tool_choice", "messages"
    }
    assert request["model"] == "claude-sonnet-5-5"
    assert request["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"] == {"effort": "low"}


def test_el_request_lleva_el_system_del_prompt_y_las_tools_en_orden(config: ConfigNegocio) -> None:
    """R18: system son los dos bloques de bloques_de_sistema; las tools, en el orden de definir_tools."""
    cliente = ClienteFalso(REGISTRAR)
    _decidir(config, cliente, pedido=_PEDIDO, nombre_preguntado=True)
    (request,) = cliente.messages.llamadas
    esperado = bloques_de_sistema(
        config, config.ahora(), nombre_perfil="Ana Prueba", pedido=_PEDIDO, nombre_preguntado=True
    )
    assert request["system"] == esperado
    assert request["tools"] == definir_tools(config)
    assert [tool["name"] for tool in request["tools"]] == list(ARGUMENTOS_POR_TOOL)


def test_el_estatico_y_las_tools_salen_iguales_entre_dos_requests(config: ConfigNegocio) -> None:
    """R18: con otro pedido y otra hora, el estático y las tools salen iguales byte a byte."""
    cliente = ClienteFalso(REGISTRAR, REGISTRAR)
    _decidir(config, cliente)
    config.fijar_ahora(config.ahora() + timedelta(days=6, hours=9))
    _decidir(config, cliente, pedido=_PEDIDO, nombre_perfil=None, confirmado=_PEDIDO)
    primero, segundo = cliente.messages.llamadas
    assert primero["system"][0]["text"].encode("utf-8") == segundo["system"][0]["text"].encode("utf-8")
    assert primero["system"][0] == segundo["system"][0]
    assert repr(primero["tools"]).encode("utf-8") == repr(segundo["tools"]).encode("utf-8")
    assert primero["system"][1] != segundo["system"][1]


# ── El historial ─────────────────────────────────────────────────────────


def test_el_historial_arranca_en_user() -> None:
    """R20: si el recorte deja turnos del bot primero, se sacan."""
    charla = _charla(
        ("assistant", "[resumen_mostrado]"), ("assistant", "[dato_faltante: material]"),
        ("user", "en lona"), ("assistant", "[resumen_mostrado]"), ("user", "dale"),
    )
    assert historial_para_la_api(charla) == [
        {"role": "user", "content": "en lona"},
        {"role": "assistant", "content": "[resumen_mostrado]"},
        {"role": "user", "content": "dale"},
    ]


def test_dos_seguidos_del_mismo_rol_van_en_un_mensaje_uno_por_linea() -> None:
    """R20: la API espera turnos alternados; los globos seguidos del cliente se leen en orden."""
    charla = _charla(
        ("user", "hola"), ("user", "quiero sellos"),
        ("assistant", "[dato_faltante: cantidad]"), ("assistant", "[consulta_general: horarios]"),
        ("user", "   "), ("user", "3"),
    )
    assert historial_para_la_api(charla) == [
        {"role": "user", "content": "hola\nquiero sellos"},
        {"role": "assistant", "content": "[dato_faltante: cantidad]\n[consulta_general: horarios]"},
        {"role": "user", "content": "3"},
    ]


def test_el_request_lleva_el_historial_que_arranca_en_user(config: ConfigNegocio) -> None:
    """R20, R21: al request va el historial recortado, con los marcadores tal cual."""
    cliente = ClienteFalso(REGISTRAR)
    _decidir(config, cliente, _charla(("assistant", "[resumen_mostrado]"), ("user", "dale")))
    assert cliente.messages.llamadas[0]["messages"] == [{"role": "user", "content": "dale"}]


@pytest.mark.parametrize(
    "charla", [_charla(), _charla(("user", "hola"), ("assistant", "[dato_faltante: producto]"))]
)
def test_sin_mensaje_del_cliente_no_se_llama_a_la_api(config: ConfigNegocio, charla: Charla) -> None:
    """R20: sin un turno del cliente al final no hay nada que decidir; no se gasta una llamada."""
    cliente = ClienteFalso()
    assert _decidir(config, cliente, charla) == SinTool("sin_mensaje")
    assert cliente.messages.llamadas == []


# ── La decisión ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("bloque", "esperado"),
    [
        (_tool("registrar_pedido", **_TODO_NULL | {"cantidad": 3}), RegistrarPedido(cantidad=3)),
        (_tool("pedir_dato_faltante", dato="material"), PedirDatoFaltante(dato="material")),
        (_tool("consulta_general", tema="horarios"), ConsultaGeneral(tema="horarios")),
        (_tool("derivar_a_asesor", motivo="sin_stock"), DerivarAAsesor(motivo="sin_stock")),
        (_tool("confirmar_pedido", acepta=True), ConfirmarPedido(acepta=True)),
    ],
)
def test_la_decision_trae_la_tool_y_sus_argumentos_tipados(
    config: ConfigNegocio, bloque: dict[str, Any], esperado: object
) -> None:
    """R11: una tool por turno, con sus argumentos validados; el thinking se ignora."""
    decision = _decidir(config, ClienteFalso(_respuesta(PENSAMIENTO, bloque)))
    assert decision == Decision(bloque["name"], esperado)  # type: ignore[arg-type]


def test_los_modelos_de_argumentos_tienen_las_claves_de_cada_esquema(config: ConfigNegocio) -> None:
    """R13: una clave por campo del esquema, ninguna más; telefono no está en ninguno."""
    for tool in definir_tools(config):
        claves = set(ARGUMENTOS_POR_TOOL[tool["name"]].model_fields)
        assert claves == set(tool["input_schema"]["properties"])
        assert "telefono" not in claves


def test_sin_tool_use_es_sin_tool_y_no_se_reintenta(config: ConfigNegocio) -> None:
    """R11: una respuesta en prosa no sale ni se reintenta: cada reintento es plata."""
    cliente = ClienteFalso(_respuesta(PENSAMIENTO, {"type": "text", "text": "¡Hola!"}, stop_reason="end_turn"))
    assert _decidir(config, cliente) == SinTool("sin_tool_use")
    assert len(cliente.messages.llamadas) == 1


def test_refusal_es_sin_tool(config: ConfigNegocio) -> None:
    """R11: un refusal se trata igual que una respuesta sin tool, aunque traiga una."""
    respuesta = _respuesta(_tool("confirmar_pedido", acepta=True), stop_reason="refusal")
    cliente = ClienteFalso(respuesta)
    assert _decidir(config, cliente) == SinTool("refusal")
    assert len(cliente.messages.llamadas) == 1


@pytest.mark.parametrize(
    "bloque",
    [
        _tool("registrar_pedido", **_TODO_NULL | {"telefono": "+54 11 5555-0000"}),
        _tool("confirmar_pedido", acepta=True, telefono=None),
        _tool("consulta_general", tema="horarios", urgente=True),
        _tool("consulta_general", tema="precios"),
        _tool("pedir_dato_faltante"),
    ],
)
def test_argumentos_que_no_validan_son_sin_tool(config: ConfigNegocio, bloque: dict[str, Any]) -> None:
    """R13: telefono, una clave extra, un valor fuera del enum o uno que falta dan sin_tool."""
    assert _decidir(config, ClienteFalso(_respuesta(bloque))) == SinTool("argumentos_invalidos")


def test_una_tool_que_no_existe_es_sin_tool(config: ConfigNegocio) -> None:
    """R11: solo las 5 tools del SPECS."""
    respuesta = _respuesta(_tool("cotizar", producto="sellos"))
    assert _decidir(config, ClienteFalso(respuesta)) == SinTool("tool_desconocida")


# ── Errores y topes ──────────────────────────────────────────────────────


def test_un_error_de_conexion_se_reintenta_una_vez(config: ConfigNegocio) -> None:
    """R12: un reintento ante un error de conexión."""
    cliente = ClienteFalso(anthropic.APIConnectionError(request=_REQUEST_HTTP), REGISTRAR)
    assert isinstance(_decidir(config, cliente), Decision)
    assert len(cliente.messages.llamadas) == 2
    assert cliente.messages.llamadas[0] == cliente.messages.llamadas[1]


def test_dos_errores_de_conexion_dan_error_sin_tercer_intento(config: ConfigNegocio) -> None:
    """R12: un reintento y no más; el error vuelve tipado, sin excepción."""
    error = anthropic.APIConnectionError(request=_REQUEST_HTTP)
    cliente = ClienteFalso(error, error, REGISTRAR)
    assert _decidir(config, cliente) == ErrorApi("conexion")
    assert len(cliente.messages.llamadas) == 2


def test_un_timeout_no_se_reintenta(config: ConfigNegocio) -> None:
    """R12: un timeout no se reintenta: duplicaría la espera del cliente."""
    cliente = ClienteFalso(anthropic.APITimeoutError(request=_REQUEST_HTTP), REGISTRAR)
    assert _decidir(config, cliente) == ErrorApi("timeout")
    assert len(cliente.messages.llamadas) == 1


@pytest.mark.parametrize(("clase", "estado"), [(anthropic.OverloadedError, 529), (anthropic.BadRequestError, 400)])
def test_un_error_http_da_error_tipado_sin_reintento(
    config: ConfigNegocio, clase: type[anthropic.APIStatusError], estado: int
) -> None:
    """R12: un 4xx o 5xx vuelve como error con su estado, sin reintento."""
    error = clase("falló", response=httpx2.Response(estado, request=_REQUEST_HTTP), body=None)
    cliente = ClienteFalso(error, REGISTRAR)
    assert _decidir(config, cliente) == ErrorApi("http", estado)
    assert len(cliente.messages.llamadas) == 1


def test_el_cliente_tiene_los_topes_y_sin_reintentos_del_sdk(
    monkeypatch: pytest.MonkeyPatch, sin_cliente: None
) -> None:
    """R12: 25 s en total y 5 s de conexión; max_retries=0, el reintento es propio."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-prueba")
    cliente = cliente_api()
    assert cliente.max_retries == 0
    assert (cliente.timeout.connect, cliente.timeout.read) == (5.0, 25.0)  # type: ignore[union-attr]
    assert cliente_api() is cliente


# ── Credencial ───────────────────────────────────────────────────────────


def test_la_credencial_no_se_lee_al_importar() -> None:
    """R53: importar el módulo sin clave no construye el cliente ni lanza."""
    entorno = {nombre: valor for nombre, valor in os.environ.items() if not nombre.startswith("ANTHROPIC_")}
    codigo = (
        "import anthropic\n"
        "def _explota(*a, **k): raise AssertionError('cliente construido al importar')\n"
        "anthropic.Anthropic = _explota\n"
        "import app.agente\n"
        "assert app.agente._cliente is None\n"
    )
    corrida = subprocess.run(
        [sys.executable, "-c", codigo], cwd=RAIZ, env=entorno, capture_output=True, text=True, timeout=60
    )
    assert corrida.returncode == 0, corrida.stderr


def test_sin_clave_el_error_es_fijo(sin_cliente: None) -> None:
    """R53: mensaje fijo y from None, sin traceback encadenado."""
    with pytest.raises(ErrorCredencial) as error:
        cliente_api()
    assert str(error.value) == FALTA_LA_CLAVE
    assert error.value.__cause__ is None and error.value.__suppress_context__


def test_sin_clave_decidir_da_error_tipado_sin_lanzar(
    config: ConfigNegocio, sin_cliente: None, caplog: pytest.LogCaptureFixture
) -> None:
    """R53: la credencial que falta es un error tipado, no un 500 que provoque reintentos."""
    caplog.set_level(logging.INFO)
    decision = decidir(config, config.ahora(), HOLA, nombre_perfil="Ana Prueba", pedido=None)
    assert decision == ErrorApi("credencial")
    assert FALTA_LA_CLAVE in caplog.text


@pytest.mark.parametrize("clave", [f"sk-{CENTINELA}\n", f" sk-{CENTINELA}", f"sk-{CENTINELA}ñ"])
def test_una_clave_mal_pegada_da_el_error_fijo_sin_la_clave(
    monkeypatch: pytest.MonkeyPatch, sin_cliente: None, caplog: pytest.LogCaptureFixture,
    config: ConfigNegocio, clave: str,
) -> None:
    """R53: una clave con espacios o saltos no llega a un header; el error no la muestra."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", clave)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ErrorCredencial) as error:
        cliente_api()
    assert str(error.value) == CLAVE_MAL_FORMADA
    assert decidir(config, config.ahora(), HOLA, nombre_perfil=None, pedido=None) == ErrorApi("credencial")
    assert CENTINELA not in caplog.text


# ── Logs ─────────────────────────────────────────────────────────────────


def test_el_log_lleva_tool_claves_y_uso_pero_ningun_valor(
    config: ConfigNegocio, caplog: pytest.LogCaptureFixture
) -> None:
    """R52: tool, claves, stop_reason, tokens de caché y alias; ni valores ni el id de la conversación."""
    caplog.set_level(logging.DEBUG)
    argumentos = _TODO_NULL | {"nombre_cliente": CENTINELA, "material": CENTINELA}
    charla = _charla(("user", f"soy {CENTINELA}"))
    decision = _decidir(
        config, ClienteFalso(_respuesta(_tool("registrar_pedido", **argumentos))), charla,
        nombre_perfil=CENTINELA, conversacion=987654321,
    )
    assert isinstance(decision, Decision)
    assert CENTINELA not in caplog.text
    assert "987654321" not in caplog.text
    assert "tool=registrar_pedido claves=material,nombre_cliente stop_reason=tool_use" in caplog.text
    assert "cache_escrita=0 cache_leida=3150" in caplog.text
    assert all(registro.levelno >= logging.INFO for registro in caplog.records if registro.name == "app.agente")


def test_el_log_de_sin_tool_dice_sin_tool_sin_valores(
    config: ConfigNegocio, caplog: pytest.LogCaptureFixture
) -> None:
    """R52, R11: sin_tool se loguea con el motivo; un telefono inventado no deja su valor."""
    caplog.set_level(logging.DEBUG)
    bloque = _tool("registrar_pedido", **_TODO_NULL | {"telefono": CENTINELA})
    assert _decidir(config, ClienteFalso(_respuesta(bloque))) == SinTool("argumentos_invalidos")
    assert "sin_tool motivo=argumentos_invalidos" in caplog.text
    assert "claves=telefono" in caplog.text
    assert CENTINELA not in caplog.text


def test_el_log_de_un_error_http_no_lleva_el_mensaje_de_la_api(
    config: ConfigNegocio, caplog: pytest.LogCaptureFixture
) -> None:
    """R52: del error va el tipo y el estado; el cuerpo puede traer texto del request."""
    caplog.set_level(logging.DEBUG)
    error = anthropic.BadRequestError(
        f"messages.0: {CENTINELA}", response=httpx2.Response(400, request=_REQUEST_HTTP), body=None
    )
    assert _decidir(config, ClienteFalso(error)) == ErrorApi("http", 400)
    assert "tipo=http estado=400" in caplog.text
    assert CENTINELA not in caplog.text


def test_los_loggers_del_sdk_no_quedan_en_debug(monkeypatch: pytest.MonkeyPatch, sin_cliente: None) -> None:
    """R52: el SDK en DEBUG vuelca requests enteros; al crear el cliente sube a INFO."""
    for nombre in ("anthropic", "httpx2"):
        monkeypatch.setattr(logging.getLogger(nombre), "level", logging.DEBUG)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-prueba")
    cliente_api()
    assert all(logging.getLogger(nombre).getEffectiveLevel() >= logging.INFO for nombre in ("anthropic", "httpx2"))
