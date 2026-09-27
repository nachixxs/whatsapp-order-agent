"""Webhook de Chatwoot: autentica, filtra y contesta un eco provisorio (CP1)."""

import hmac
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from app.chatwoot import ClienteChatwoot, MensajeEntrante, parsear_evento
from app.formato import alias_conversacion, en_una_linea, para_log

# CLAUDE.md "Reglas duras del codigo": .env con ruta explicita, nunca sin ruta
# (sin ruta sube carpetas hasta encontrar uno, y desde un worktree puede agarrar
# las credenciales de otro checkout)
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
TOPE_BYTES_BODY = 1_000_000
TOPE_CARACTERES_ECO = 4_096

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def _quitar_query(record: logging.LogRecord) -> bool:
    # R52: el access log de uvicorn muestra la query string, y ahi viaja el secreto
    if record.args:
        record.args = tuple(
            arg.split("?", 1)[0] if isinstance(arg, str) and "?" in arg else arg
            for arg in record.args
        )
    return True


logging.getLogger("uvicorn.access").addFilter(_quitar_query)
# R52: en INFO, httpx loguea la URL completa de sus pedidos (ahi va el id real de conversacion)
logging.getLogger("httpx").setLevel(logging.WARNING)


def get_cliente_chatwoot() -> ClienteChatwoot:
    return ClienteChatwoot()


def _autenticado(token: str) -> bool:
    secreto = os.environ.get("CHATWOOT_WEBHOOK_SECRET", "")
    if not secreto:
        return False  # R49: un secreto vacio o no configurado nunca coincide
    # R50: compare_digest exige bytes, un token no ASCII no puede tumbar el webhook
    return hmac.compare_digest(secreto.encode("utf-8"), (token or "").encode("utf-8"))


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
        texto = f"Eco: {mensaje.contenido}"
    else:
        texto = f"Eco: recibí {mensaje.cantidad_adjuntos} archivo(s)"
    # R35: ningun mensaje pasa los 4.096 caracteres
    return en_una_linea(texto)[:TOPE_CARACTERES_ECO]


def _procesar_eco(mensaje: MensajeEntrante, cliente: ClienteChatwoot) -> None:
    if mensaje.id_conversacion is None:
        logger.warning("Webhook Chatwoot: sin id de conversacion, no se responde")
        return
    cliente.responder(mensaje.id_conversacion, _texto_eco(mensaje))


def _capturar_payload(cuerpo: bytes) -> None:
    # Tarea 1.6: cuerpo crudo, una linea por evento. Apagado por defecto, nunca a otra ruta.
    if not os.environ.get("CHATWOOT_CAPTURAR_PAYLOADS"):
        return
    try:
        CARPETA_CAPTURAS.mkdir(exist_ok=True)
        linea = cuerpo.decode("utf-8", "replace").replace("\n", " ")
        with open(CARPETA_CAPTURAS / "payloads.jsonl", "a", encoding="utf-8") as archivo:
            archivo.write(linea + "\n")
    except OSError:
        # R50: un disco lleno o sin permisos no puede tumbar el webhook
        logger.warning("Webhook Chatwoot: no se pudo escribir la captura del payload")


async def _leer_cuerpo_con_tope(request: Request) -> bytes | None:
    """None si el body supera TOPE_BYTES_BODY, aunque el Content-Length mienta."""
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > TOPE_BYTES_BODY:
                return None
        except ValueError:
            pass
    partes = bytearray()
    async for fragmento in request.stream():
        partes.extend(fragmento)
        if len(partes) > TOPE_BYTES_BODY:
            return None
    return bytes(partes)


@app.post("/webhook/chatwoot", response_model=None)
async def webhook_chatwoot(
    request: Request,
    background_tasks: BackgroundTasks,
    token: str = "",
    cliente: ClienteChatwoot = Depends(get_cliente_chatwoot),
) -> dict[str, str] | JSONResponse:
    cuerpo = await _leer_cuerpo_con_tope(request)
    if cuerpo is None:
        # Seguridad: cuerpo demasiado grande, se corta sin leerlo entero.
        # 413 no dispara reintentos de Chatwoot (R50: solo reintenta 429/500).
        return JSONResponse({"estado": "ignorado"}, status_code=413)
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
