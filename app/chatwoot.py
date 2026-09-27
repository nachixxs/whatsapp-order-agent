"""Infraestructura de Chatwoot: parseo del webhook y cliente para responder."""

import json
import logging
import os
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

TIMEOUT_SEGUNDOS = 10.0


class Contacto(BaseModel):
    model_config = ConfigDict(extra="ignore")

    nombre: str | None = None
    telefono: str | None = None


class MensajeEntrante(BaseModel):
    """Lo que el bot necesita de un webhook de Chatwoot ya filtrado (R48)."""

    model_config = ConfigDict(extra="ignore")

    evento: str
    tipo_mensaje: str
    privado: bool
    id_mensaje: int | None = None
    contenido: str = ""
    cantidad_adjuntos: int = 0
    id_conversacion: int | None = None
    account_id: int | None = None
    inbox_id: int | None = None
    estado_conversacion: str | None = None
    contacto: Contacto = Contacto()


def _entero(valor: object) -> int | None:
    try:
        return int(valor)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _texto(valor: object) -> str:
    if not isinstance(valor, str):
        return ""
    # R51: un surrogate suelto no puede tumbar el parseo
    return valor.encode("utf-8", "replace").decode("utf-8")


def _booleano(valor: object) -> bool:
    if isinstance(valor, str):
        return valor.strip().lower() not in ("", "false", "0")
    return bool(valor)


def _como_dict(valor: object) -> dict[str, Any]:
    return valor if isinstance(valor, dict) else {}


def _armar_mensaje(crudo: dict[str, Any]) -> MensajeEntrante:
    conversacion = _como_dict(crudo.get("conversation"))
    account = _como_dict(crudo.get("account"))
    inbox = _como_dict(crudo.get("inbox"))
    sender = _como_dict(crudo.get("sender"))
    adjuntos = crudo.get("attachments")
    return MensajeEntrante(
        evento=_texto(crudo.get("event")) or "message_created",
        tipo_mensaje="incoming",
        privado=False,
        id_mensaje=_entero(crudo.get("id")),
        contenido=_texto(crudo.get("content")),
        cantidad_adjuntos=len(adjuntos) if isinstance(adjuntos, list) else 0,
        id_conversacion=_entero(conversacion.get("id")),
        account_id=_entero(account.get("id")),
        inbox_id=_entero(inbox.get("id")),
        estado_conversacion=_texto(conversacion.get("status")) or None,
        contacto=Contacto(
            nombre=_texto(sender.get("name")) or None,
            telefono=_texto(sender.get("phone_number")) or None,
        ),
    )


def parsear_evento(cuerpo: bytes) -> MensajeEntrante | None:
    """R51: nunca lanza. Devuelve un mensaje solo si es procesable (R48)."""
    try:
        crudo = json.loads(cuerpo)
    except Exception:  # R51: JSON invalido, anidado (RecursionError) o bytes raros
        return None
    if not isinstance(crudo, dict):
        return None
    if crudo.get("event") != "message_created":
        return None
    # R48: solo incoming; otras formas de message_type (numericas, etc.) se descartan
    # por ahora y se revisan contra un Chatwoot real en la tarea 1.6.
    if crudo.get("message_type") != "incoming":
        return None
    if _booleano(crudo.get("private")):
        return None
    try:
        return _armar_mensaje(crudo)
    except Exception:  # R51: un campo con un tipo inesperado no tumba el parseo
        return None


class ClienteChatwoot:
    """Responde en una conversacion de Chatwoot. Credenciales perezosas (R53)."""

    def __init__(self, cliente_http: httpx.Client | None = None) -> None:
        self._cliente_http = cliente_http

    def responder(self, id_conversacion: int, texto: str) -> None:
        # R53: credenciales perezosas, se leen al usar y nunca al construir el cliente
        base_url = os.environ.get("CHATWOOT_URL", "")
        account_id = os.environ.get("CHATWOOT_ACCOUNT_ID", "")
        token = os.environ.get("CHATWOOT_BOT_TOKEN", "")
        if not (base_url and account_id and token):
            logger.error("Chatwoot: faltan credenciales, no se pudo responder")
            return
        url = f"{base_url}/api/v1/accounts/{account_id}/conversations/{id_conversacion}/messages"
        propio = self._cliente_http is None
        cliente = self._cliente_http or httpx.Client(timeout=TIMEOUT_SEGUNDOS)
        try:
            respuesta = cliente.post(
                url,
                json={"content": texto, "message_type": "outgoing"},
                headers={"api_access_token": token},
            )
            respuesta.raise_for_status()
        except Exception:  # R53: un fallo de red no expone el token ni tumba el turno
            logger.error("Chatwoot: fallo al enviar la respuesta")
        finally:
            if propio:
                cliente.close()
