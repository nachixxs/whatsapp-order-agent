"""Infraestructura de Chatwoot: parseo del webhook y cliente para responder."""

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

TIMEOUT_SEGUNDOS = 10.0
TIMEOUT_CONTACTO_SEGUNDOS = 5.0  # R45


class Contacto(BaseModel):
    model_config = ConfigDict(extra="ignore")

    nombre: str | None = None
    telefono: str | None = None
    id: int | None = None
    atributos: dict[str, Any] | None = None  # custom_attributes (R45); None = sin dato. Nunca al log (R52)


class Adjunto(BaseModel):
    """Sin `data_url`: no hace falta y nunca va al log (R29, R52)."""

    id: int
    tipo: str
    extension: str | None = None
    tamano: int | None = None


class MensajeEntrante(BaseModel):
    """Lo que el bot necesita de un webhook de Chatwoot ya filtrado (R48)."""

    model_config = ConfigDict(extra="ignore")

    evento: str
    id_mensaje: int | None = None
    contenido: str = ""
    adjuntos: list[Adjunto] = []
    creado: datetime | None = None
    id_conversacion: int | None = None
    account_id: int | None = None
    inbox_id: int | None = None
    estado_conversacion: str | None = None
    contacto: Contacto = Contacto()


def _entero(valor: object) -> int | None:
    try:
        return int(valor)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
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


def _adjunto(crudo: object) -> Adjunto | None:
    datos = _como_dict(crudo)
    id_adjunto = _entero(datos.get("id"))
    if id_adjunto is None:
        return None
    return Adjunto(
        id=id_adjunto,
        tipo=_texto(datos.get("file_type")),
        extension=_texto(datos.get("extension")) or None,
        tamano=_entero(datos.get("file_size")),
    )


def _creado(valor: object) -> datetime | None:
    """R29: el created_at del mensaje en UTC; si falta o es invalido, None (se usa la hora de llegada)."""
    try:
        fecha = datetime.fromisoformat(valor)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return fecha.astimezone(UTC) if fecha.tzinfo else fecha.replace(tzinfo=UTC)


def _armar_mensaje(crudo: dict[str, Any]) -> MensajeEntrante:
    conversacion = _como_dict(crudo.get("conversation"))
    account = _como_dict(crudo.get("account"))
    inbox = _como_dict(crudo.get("inbox"))
    sender = _como_dict(crudo.get("sender"))
    adjuntos = crudo.get("attachments")
    adjuntos = adjuntos if isinstance(adjuntos, list) else []
    return MensajeEntrante(
        evento=_texto(crudo.get("event")) or "message_created",
        id_mensaje=_entero(crudo.get("id")),
        contenido=_texto(crudo.get("content")),
        adjuntos=[a for a in map(_adjunto, adjuntos) if a is not None],
        creado=_creado(crudo.get("created_at")),
        id_conversacion=_entero(conversacion.get("id")),
        account_id=_entero(account.get("id")),
        inbox_id=_entero(inbox.get("id")),
        estado_conversacion=_texto(conversacion.get("status")) or None,
        contacto=Contacto(
            nombre=_texto(sender.get("name")) or None,
            telefono=_texto(sender.get("phone_number")) or None,
            id=_entero(sender.get("id")),
            atributos=sender.get("custom_attributes") if isinstance(sender.get("custom_attributes"), dict) else None,
        ),
    )


def _cargar(cuerpo: bytes) -> dict[str, Any] | None:
    try:
        crudo = json.loads(cuerpo)
    except Exception:  # R51: JSON invalido, anidado (RecursionError) o bytes raros
        return None
    return crudo if isinstance(crudo, dict) else None


def parsear_evento(cuerpo: bytes) -> MensajeEntrante | None:
    """R51: nunca lanza. Devuelve un mensaje solo si es procesable (R48): incoming y no privado."""
    crudo = _cargar(cuerpo)
    if crudo is None or crudo.get("event") != "message_created" or crudo.get("message_type") != "incoming":
        return None
    try:
        return None if _booleano(crudo.get("private")) else _armar_mensaje(crudo)
    except Exception:  # R51: un campo con un tipo inesperado no tumba el parseo
        return None


def parsear_estado(cuerpo: bytes) -> MensajeEntrante | None:
    """R47, R51: id, estado, cuenta e inbox de cualquier evento. Nunca lanza. Un evento de conversacion los trae en la
    raiz (`id`, `status`, `inbox_id`, `account`); uno de mensaje, en `conversation` e `inbox` (Chatwoot v4.18.0)."""
    crudo = _cargar(cuerpo)
    if crudo is not None and _texto(crudo.get("event")).startswith("conversation_"):
        crudo = {**crudo, "conversation": crudo, "inbox": {"id": crudo.get("inbox_id")}}
    try:
        return None if crudo is None else _armar_mensaje(crudo)
    except Exception:  # R51
        return None


class ClienteChatwoot:
    """Habla con la API de Chatwoot. Credenciales perezosas (R53). Nada lanza: devuelven True solo con 2xx."""

    def __init__(self, cliente_http: httpx.Client | None = None) -> None:
        self._cliente_http = cliente_http

    def _pedir(self, metodo: str, ruta: str, cuerpo: dict[str, Any], token_env: str, timeout: float, accion: str) -> bool:
        """`ruta` cuelga de la cuenta. True solo con 2xx; nunca lanza (R53)."""
        base_url = os.environ.get("CHATWOOT_URL", "")
        account_id = os.environ.get("CHATWOOT_ACCOUNT_ID", "")
        token = os.environ.get(token_env, "")
        if not (base_url and account_id and token):
            logger.error("Chatwoot: faltan credenciales, no se pudo %s", accion)
            return False
        url = f"{base_url}/api/v1/accounts/{account_id}/{ruta}"
        propio = self._cliente_http is None
        cliente = self._cliente_http or httpx.Client()
        try:
            cliente.request(metodo, url, json=cuerpo, headers={"api_access_token": token}, timeout=timeout).raise_for_status()
            return True
        except Exception:  # R53: un fallo de red no expone el token ni tumba el turno; nunca el texto ni el error
            logger.error("Chatwoot: fallo al %s", accion)
            return False
        finally:
            if propio:
                cliente.close()

    def _post(self, id_conversacion: int, ruta: str, cuerpo: dict[str, Any], accion: str) -> bool:
        ruta_completa = f"conversations/{id_conversacion}/{ruta}"
        return self._pedir("POST", ruta_completa, cuerpo, "CHATWOOT_BOT_TOKEN", TIMEOUT_SEGUNDOS, accion)

    def actualizar_contacto(self, id_contacto: int, atributos: dict[str, Any]) -> bool:
        """R45: escribe custom_attributes del contacto (Chatwoot los mezcla). Token de agente: el del bot no puede.

        Tope de 5 s y sin reintento: si falla, el turno sigue (R41).
        """
        cuerpo = {"custom_attributes": atributos}
        return self._pedir("PUT", f"contacts/{id_contacto}", cuerpo, "CHATWOOT_AGENTE_TOKEN", TIMEOUT_CONTACTO_SEGUNDOS, "actualizar el contacto")

    def responder(self, id_conversacion: int, texto: str) -> bool:
        return self._post(id_conversacion, "messages", {"content": texto, "message_type": "outgoing"}, "enviar la respuesta")

    def nota_interna(self, id_conversacion: int, texto: str) -> bool:
        """Aviso al asesor: un mensaje privado, el cliente no lo ve."""
        cuerpo = {"content": texto, "message_type": "outgoing", "private": True}
        return self._post(id_conversacion, "messages", cuerpo, "enviar la nota interna")

    def pasar_a_persona(self, id_conversacion: int) -> bool:
        """R47: derivar es dejar la conversacion en `open`; desde ahi el bot ya no contesta."""
        return self._post(id_conversacion, "toggle_status", {"status": "open"}, "pasar la conversacion a open")
