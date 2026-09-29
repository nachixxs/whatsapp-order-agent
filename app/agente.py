"""Llamada a la Claude API: historial, request y decisión tipada. Aplicarla es de turno.py."""

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import anthropic
from anthropic.types import Message, ToolUseBlock
from pydantic import BaseModel, ConfigDict, ValidationError

from app.config import ConfigNegocio
from app.formato import alias_conversacion, para_log
from app.memoria import Charla
from app.pedidos import Pedido
from app.prompt import bloques_de_sistema
from app.tools import CAMPOS_DEL_PEDIDO, MOTIVOS_DERIVACION, TEMAS_CONSULTA, definir_tools

logger = logging.getLogger(__name__)

MODELO = "claude-sonnet-5-5"
MAX_TOKENS = 16000  # techo para que la respuesta no se corte, no un objetivo
EFFORT = "low"  # R11
# R12. `anthropic.Timeout` es el de httpx2, el transporte del SDK: un httpx.Timeout no le sirve
TOPE = anthropic.Timeout(25.0, connect=5.0)
FALTA_LA_CLAVE = "falta ANTHROPIC_API_KEY en el entorno"
CLAVE_MAL_FORMADA = "ANTHROPIC_API_KEY mal formada: tiene espacios o caracteres no imprimibles"
_CLAVE = re.compile(r"[\x21-\x7e]+")

_cliente: anthropic.Anthropic | None = None


class ErrorCredencial(Exception):
    """La clave falta o está mal formada. Mensaje fijo, nunca la clave (R53)."""


class _Argumentos(BaseModel):
    # R13: extra="forbid" deja afuera `telefono` y cualquier clave que no esté en el esquema
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class RegistrarPedido(_Argumentos):
    # R14: los valores los valida pedidos.sumar_campos campo por campo; acá solo la forma
    producto: str | None = None
    material: str | None = None
    medidas: str | None = None
    cantidad: int | None = None
    fecha_necesita: str | None = None
    tiene_diseno: str | None = None
    nombre_cliente: str | None = None


class PedirDatoFaltante(_Argumentos):
    dato: Literal[CAMPOS_DEL_PEDIDO]  # type: ignore[valid-type]


class ConsultaGeneral(_Argumentos):
    tema: Literal[TEMAS_CONSULTA]  # type: ignore[valid-type]


class DerivarAAsesor(_Argumentos):
    motivo: Literal[MOTIVOS_DERIVACION]  # type: ignore[valid-type]


class ConfirmarPedido(_Argumentos):
    acepta: bool


Argumentos = RegistrarPedido | PedirDatoFaltante | ConsultaGeneral | DerivarAAsesor | ConfirmarPedido
ARGUMENTOS_POR_TOOL: dict[str, type[Argumentos]] = {
    "registrar_pedido": RegistrarPedido,
    "pedir_dato_faltante": PedirDatoFaltante,
    "consulta_general": ConsultaGeneral,
    "derivar_a_asesor": DerivarAAsesor,
    "confirmar_pedido": ConfirmarPedido,
}


@dataclass(frozen=True)
class Decision:
    tool: str
    argumentos: Argumentos


@dataclass(frozen=True)
class SinTool:
    """R11: no se reintenta; el texto fijo lo elige turno.py (R19)."""

    motivo: Literal["sin_tool_use", "refusal", "tool_desconocida", "argumentos_invalidos", "sin_mensaje"]


@dataclass(frozen=True)
class ErrorApi:
    tipo: Literal["credencial", "timeout", "conexion", "http", "api"]
    estado_http: int | None = None


def historial_para_la_api(charla: Charla) -> list[dict[str, str]]:
    """R20: arranca en `user`. Dos seguidos del mismo rol van en un mensaje, uno por línea."""
    mensajes: list[dict[str, str]] = []
    for mensaje in charla.mensajes:
        if not mensaje.content.strip():
            continue  # la API rechaza un texto vacío con un 400
        if not mensajes and mensaje.role != "user":
            continue  # R20
        if mensajes and mensajes[-1]["role"] == mensaje.role:
            mensajes[-1]["content"] += "\n" + mensaje.content
        else:
            mensajes.append({"role": mensaje.role, "content": mensaje.content})
    return mensajes


def armar_request(
    config: ConfigNegocio, ahora: datetime, charla: Charla, *, nombre_perfil: str | None,
    pedido: Pedido | None, nombre_preguntado: bool = False, confirmado: Pedido | None = None,
) -> dict[str, Any]:
    """Los parámetros de `messages.create`; los reusa la batería de ruteo."""
    return {
        "model": MODELO,
        "max_tokens": MAX_TOKENS,
        "thinking": {"type": "adaptive"},  # R11: sin thinking, la tool salía escrita como texto
        "output_config": {"effort": EFFORT},
        "system": bloques_de_sistema(
            config, ahora, nombre_perfil=nombre_perfil, pedido=pedido,
            nombre_preguntado=nombre_preguntado, confirmado=confirmado,
        ),
        "tools": definir_tools(config),
        "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},  # R11: da 400 con "any"
        "messages": historial_para_la_api(charla),
    }


def cliente_api() -> anthropic.Anthropic:
    """R53: se construye en la primera llamada, nunca al importar ni al arrancar."""
    global _cliente
    if _cliente is None:
        clave = os.environ.get("ANTHROPIC_API_KEY", "")
        if not clave:
            raise ErrorCredencial(FALTA_LA_CLAVE) from None
        if not _CLAVE.fullmatch(clave):  # R53: si no, httpx2 la pondría en el error del header
            raise ErrorCredencial(CLAVE_MAL_FORMADA) from None
        for nombre in ("anthropic", "httpx2"):  # R52: en DEBUG vuelcan el request entero
            if logging.getLogger(nombre).getEffectiveLevel() < logging.INFO:
                logging.getLogger(nombre).setLevel(logging.INFO)
        _cliente = anthropic.Anthropic(api_key=clave, timeout=TOPE, max_retries=0)  # R12
    return _cliente


def _pedir(cliente: anthropic.Anthropic, request: dict[str, Any]) -> Message:
    try:
        return cliente.messages.create(**request)
    except anthropic.APITimeoutError:
        raise  # R12: hereda de APIConnectionError; reintentarlo duplicaría la espera
    except anthropic.APIConnectionError:
        logger.warning("agente: error de conexión, un reintento")  # R12
        return cliente.messages.create(**request)


def _error_tipado(error: anthropic.AnthropicError) -> ErrorApi:
    if isinstance(error, anthropic.APITimeoutError):
        return ErrorApi("timeout")
    if isinstance(error, anthropic.APIConnectionError):
        return ErrorApi("conexion")
    if isinstance(error, anthropic.APIStatusError):
        return ErrorApi("http", error.status_code)
    return ErrorApi("api")


def _tool_use(respuesta: Message) -> ToolUseBlock | None:
    # R11: disable_parallel_tool_use deja una sola; si vinieran más, vale la primera
    return next((bloque for bloque in respuesta.content if bloque.type == "tool_use"), None)


def leer_respuesta(respuesta: Message) -> Decision | SinTool:
    """La tool y sus argumentos validados; cualquier otra cosa es `sin_tool` (R11, R13)."""
    if respuesta.stop_reason == "refusal":
        return SinTool("refusal")
    bloque = _tool_use(respuesta)
    if bloque is None:
        return SinTool("sin_tool_use")
    modelo = ARGUMENTOS_POR_TOOL.get(bloque.name)
    if modelo is None:
        return SinTool("tool_desconocida")
    try:
        return Decision(bloque.name, modelo.model_validate(bloque.input))
    except ValidationError:
        return SinTool("argumentos_invalidos")


def _claves(bloque: ToolUseBlock | None) -> str:
    # R52: las claves que el modelo llenó, nunca sus valores
    if bloque is None or not isinstance(bloque.input, dict):
        return "-"
    return ",".join(sorted(para_log(clave) for clave, valor in bloque.input.items() if valor is not None))


def _loguear(alias: str, respuesta: Message, resultado: Decision | SinTool) -> None:
    bloque, uso = _tool_use(respuesta), respuesta.usage
    texto = "alias=%s tool=%s claves=%s stop_reason=%s entrada=%d salida=%d cache_escrita=%d cache_leida=%d"
    datos = (
        alias, para_log(bloque.name) if bloque else "-", _claves(bloque), para_log(respuesta.stop_reason),
        uso.input_tokens, uso.output_tokens,
        uso.cache_creation_input_tokens or 0, uso.cache_read_input_tokens or 0,
    )
    if isinstance(resultado, SinTool):
        logger.warning("agente: sin_tool motivo=%s " + texto, resultado.motivo, *datos)
    else:
        logger.info("agente: " + texto, *datos)


def decidir(
    config: ConfigNegocio, ahora: datetime, charla: Charla, *, nombre_perfil: str | None,
    pedido: Pedido | None, nombre_preguntado: bool = False, confirmado: Pedido | None = None,
    conversacion: int | None = None, cliente: anthropic.Anthropic | None = None,
) -> Decision | SinTool | ErrorApi:
    """Una llamada, una tool. La charla ya trae el mensaje del cliente. Nunca lanza por la API."""
    alias = alias_conversacion(conversacion) if conversacion is not None else "-"
    request = armar_request(
        config, ahora, charla, nombre_perfil=nombre_perfil, pedido=pedido,
        nombre_preguntado=nombre_preguntado, confirmado=confirmado,
    )
    if not request["messages"] or request["messages"][-1]["role"] != "user":
        logger.warning("agente: sin_tool motivo=sin_mensaje alias=%s", alias)  # no se gasta una llamada
        return SinTool("sin_mensaje")
    try:
        respuesta = _pedir(cliente if cliente is not None else cliente_api(), request)
    except ErrorCredencial as error:
        logger.error("agente: error de API tipo=credencial alias=%s: %s", alias, error)  # mensaje fijo
        return ErrorApi("credencial")
    except anthropic.AnthropicError as error:  # R12: ningún error del SDK llega al webhook
        fallo = _error_tipado(error)
        estado = fallo.estado_http or "-"
        logger.error("agente: error de API tipo=%s estado=%s alias=%s", fallo.tipo, estado, alias)
        return fallo
    resultado = leer_respuesta(respuesta)
    _loguear(alias, respuesta, resultado)
    return resultado
