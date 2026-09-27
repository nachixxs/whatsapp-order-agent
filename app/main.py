"""Webhook de Chatwoot: autentica, filtra y contesta un eco provisorio (CP1)."""

import hmac
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from app.chatwoot import ClienteChatwoot, MensajeEntrante, parsear_evento
from app.formato import alias_conversacion, para_log

# R37/CLAUDE.md: .env con ruta explicita, nunca sin ruta (agarraria el de otro checkout)
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

logger = logging.getLogger(__name__)

CARPETA_CAPTURAS = Path(__file__).resolve().parent.parent / "capturas"
VARIABLES_CHATWOOT = (
    "CHATWOOT_URL",
    "CHATWOOT_ACCOUNT_ID",
    "CHATWOOT_INBOX_ID",
    "CHATWOOT_BOT_TOKEN",
    "CHATWOOT_WEBHOOK_SECRET",
)

app = FastAPI()


def _quitar_query(record: logging.LogRecord) -> bool:
    # R52: el access log de uvicorn muestra la query string, y ahi viaja el secreto
    if record.args:
        record.args = tuple(
            arg.split("?", 1)[0] if isinstance(arg, str) and "?" in arg else arg
            for arg in record.args
        )
    return True


logging.getLogger("uvicorn.access").addFilter(_quitar_query)


def get_cliente_chatwoot() -> ClienteChatwoot:
    return ClienteChatwoot()


def _autenticado(token: str) -> bool:
    secreto = os.environ.get("CHATWOOT_WEBHOOK_SECRET", "")
    if not secreto:
        return False  # R49: un secreto vacio o no configurado nunca coincide
    return hmac.compare_digest(secreto, token or "")


def _cuenta_valida(mensaje: MensajeEntrante) -> bool:
    esperado_cuenta = os.environ.get("CHATWOOT_ACCOUNT_ID")
    esperado_inbox = os.environ.get("CHATWOOT_INBOX_ID")
    if esperado_cuenta is None or esperado_inbox is None:
        return False
    try:
        return mensaje.account_id == int(esperado_cuenta) and mensaje.inbox_id == int(esperado_inbox)
    except ValueError:
        return False


def _texto_eco(mensaje: MensajeEntrante) -> str:
    if mensaje.contenido:
        return f"Eco: {mensaje.contenido}"
    return f"Eco: recibí {mensaje.cantidad_adjuntos} archivo(s)"


def _procesar_eco(mensaje: MensajeEntrante, cliente: ClienteChatwoot) -> None:
    if mensaje.id_conversacion is None:
        logger.warning("Webhook Chatwoot: sin id de conversacion, no se responde")
        return
    cliente.responder(mensaje.id_conversacion, _texto_eco(mensaje))


def _capturar_payload(cuerpo: bytes) -> None:
    # Tarea 1.6: cuerpo crudo, una linea por evento. Apagado por defecto, nunca a otra ruta.
    if not os.environ.get("CHATWOOT_CAPTURAR_PAYLOADS"):
        return
    CARPETA_CAPTURAS.mkdir(exist_ok=True)
    linea = cuerpo.decode("utf-8", "replace").replace("\n", " ")
    with open(CARPETA_CAPTURAS / "payloads.jsonl", "a", encoding="utf-8") as archivo:
        archivo.write(linea + "\n")


@app.post("/webhook/chatwoot")
async def webhook_chatwoot(
    request: Request,
    background_tasks: BackgroundTasks,
    token: str = "",
    cliente: ClienteChatwoot = Depends(get_cliente_chatwoot),
) -> dict[str, str]:
    cuerpo = await request.body()
    # R50: siempre 200, nunca se provocan reintentos en bucle
    if not _autenticado(token):
        logger.warning("Webhook Chatwoot: autenticacion fallida")
        return {"estado": "ignorado"}
    mensaje = parsear_evento(cuerpo)
    if mensaje is None:
        logger.info("Webhook Chatwoot: evento descartado")
        return {"estado": "ignorado"}
    if not _cuenta_valida(mensaje):
        logger.warning("Webhook Chatwoot: account o inbox inesperado")
        return {"estado": "ignorado"}
    alias = alias_conversacion(mensaje.id_conversacion)
    logger.info(
        "Webhook Chatwoot: mensaje aceptado alias=%s evento=%s",
        alias,
        para_log(mensaje.evento),
    )
    _capturar_payload(cuerpo)
    background_tasks.add_task(_procesar_eco, mensaje, cliente)
    return {"estado": "ok"}


@app.get("/salud")
def salud() -> JSONResponse:
    # R55: def (no async), sin llamar a Chatwoot ni a ningun servicio externo
    completa = all(os.environ.get(nombre) for nombre in VARIABLES_CHATWOOT)
    if completa:
        return JSONResponse({"estado": "ok"}, status_code=200)
    return JSONResponse({"estado": "degradado"}, status_code=503)
