"""Webhook de Chatwoot: autentica, filtra y manda cada mensaje de texto al turno (R47 a R50)."""

import hashlib
import hmac
import logging
import os
import sys
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Request
import httpx
from fastapi.responses import JSONResponse

from app.chatwoot import ClienteChatwoot, MensajeEntrante, parsear_evento
from app.config import RUTA_POR_DEFECTO, ConfigNegocio, cargar_config
from app.formato import alias_conversacion, para_log
from app.memoria import Memoria
from app.turno import procesar_lote

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
TOLERANCIA_SEGUNDOS = 300
# Raiz del repo: `*.db` esta en .gitignore, asi que la base nunca se versiona; y no depende del cwd.
MEMORIA_RUTA_POR_DEFECTO = Path(__file__).resolve().parent.parent / "memoria.db"


def workers_pedidos(argv: Sequence[str], entorno: Mapping[str, str]) -> int:
    """R28: cuantos workers pidio quien arranco el servidor. La linea de comandos le gana a
    WEB_CONCURRENCY, igual que en uvicorn. Un valor que no es entero lanza ValueError (no arranca).

    Limite: solo ve `--workers N`, `--workers=N`, `-w N` y WEB_CONCURRENCY. `uvicorn.run(workers=N)`
    desde codigo o un gunicorn.conf.py no se detectan desde aca.
    """
    for i, arg in enumerate(argv):
        if arg in ("--workers", "-w") and i + 1 < len(argv):
            return int(argv[i + 1])
        if arg.startswith("--workers="):
            return int(arg.split("=", 1)[1])
    return int(entorno.get("WEB_CONCURRENCY") or 1)


@asynccontextmanager
async def _ciclo_de_vida(app: FastAPI) -> AsyncIterator[None]:
    # R54: la config rota se descubre al arrancar. Solo corre con el servidor (o `with TestClient`):
    # importar la app en pytest no toca el disco.
    if workers_pedidos(sys.argv, os.environ) > 1:
        # R28: dos procesos sobre el mismo archivo SQLite pisan la misma charla
        raise RuntimeError("R28: el bot corre con un solo worker (quitar --workers / WEB_CONCURRENCY)")
    app.state.config = cargar_config(RUTA_POR_DEFECTO)
    # R54: una base rota o una ruta invalida se descubre al arrancar, no con el primer mensaje
    app.state.memoria = Memoria(os.environ.get("MEMORIA_RUTA") or MEMORIA_RUTA_POR_DEFECTO)
    try:
        yield
    finally:
        app.state.memoria.cerrar()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=_ciclo_de_vida)

# R52: el secreto ya no viaja en la URL, asi que el access log de uvicorn no necesita filtro.
# R52: en INFO, httpx loguea la URL completa de sus pedidos (ahi va el id real de conversacion)
logging.getLogger("httpx").setLevel(logging.WARNING)


def get_cliente_chatwoot() -> ClienteChatwoot:
    return ClienteChatwoot()


def get_config(request: Request) -> ConfigNegocio:
    return request.app.state.config  # cargada en _ciclo_de_vida; los tests sobreescriben esta dependencia


def get_memoria(request: Request) -> Memoria:
    return request.app.state.memoria  # abierta en _ciclo_de_vida; los tests sobreescriben esta dependencia


def _firma_valida(cuerpo: bytes, firma: str, timestamp: str, config: ConfigNegocio) -> bool:
    """R49: firma = "sha256=" + HMAC-SHA256("<timestamp>.<body>") con el secret del Agent Bot."""
    secreto = os.environ.get("CHATWOOT_WEBHOOK_SECRET", "")
    if not secreto:
        return False  # R49: un secreto vacio o no configurado nunca coincide
    if not (timestamp.isascii() and timestamp.isdigit() and len(timestamp) <= 12):
        return False  # ausente o no numerico
    if abs(config.ahora().timestamp() - int(timestamp)) > TOLERANCIA_SEGUNDOS:
        return False  # R49: frena reenvios viejos y relojes adelantados
    firmado = timestamp.encode() + b"." + cuerpo
    esperada = "sha256=" + hmac.new(secreto.encode("utf-8"), firmado, hashlib.sha256).hexdigest()
    # R50: compare_digest sobre bytes, un header no ASCII no puede tumbar el webhook
    return hmac.compare_digest(esperada.encode(), firma.encode("utf-8", "replace"))


def _cuenta_valida(mensaje: MensajeEntrante) -> bool:
    esperado_cuenta = os.environ.get("CHATWOOT_ACCOUNT_ID")
    esperado_inbox = os.environ.get("CHATWOOT_INBOX_ID")
    if esperado_cuenta is None or esperado_inbox is None:
        return False
    try:
        return mensaje.account_id == int(esperado_cuenta) and mensaje.inbox_id == int(esperado_inbox)
    except ValueError:
        return False


def _procesar_turno(
    mensaje: MensajeEntrante, cliente: ClienteChatwoot, config: ConfigNegocio, memoria: Memoria
) -> None:
    """Corre en el threadpool (BackgroundTasks ejecuta las `def` con run_in_threadpool): SQLite y la
    API bloquean, y el event loop tiene que seguir libre para el webhook (R50)."""
    if mensaje.id_conversacion is None:
        logger.warning("Webhook Chatwoot: sin id de conversacion, no se responde")
        return
    if not mensaje.contenido.strip():
        # Tarea 3.3: los adjuntos todavia no van al turno. Hasta la 3.5 (debounce, R30) un mensaje es un lote.
        return
    texto = procesar_lote(mensaje.id_conversacion, [mensaje], config, memoria)
    if texto is None:
        return
    try:
        cliente.responder(mensaje.id_conversacion, texto)
    except (httpx.HTTPError, OSError) as error:
        # R52: solo el tipo del error, nunca su mensaje (puede traer la URL con el id de conversacion)
        logger.error("Webhook Chatwoot: fallo al responder alias=%s error=%s", alias_conversacion(mensaje.id_conversacion), type(error).__name__)


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
    cliente: ClienteChatwoot = Depends(get_cliente_chatwoot),
    config: ConfigNegocio = Depends(get_config),
    memoria: Memoria = Depends(get_memoria),
) -> dict[str, str] | JSONResponse:
    cuerpo = await _leer_cuerpo_con_tope(request)
    if cuerpo is None:
        # Seguridad: cuerpo demasiado grande, se corta sin leerlo entero.
        # 413 no dispara reintentos de Chatwoot (R50: solo reintenta 429/500).
        return JSONResponse({"estado": "ignorado"}, status_code=413)
    # R50: siempre 200, nunca se provocan reintentos en bucle
    firma = request.headers.get("x-chatwoot-signature", "")
    timestamp = request.headers.get("x-chatwoot-timestamp", "")
    if not _firma_valida(cuerpo, firma, timestamp, config):
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
    background_tasks.add_task(_procesar_turno, mensaje, cliente, config, memoria)
    return {"estado": "ok"}


@app.get("/salud")
def salud() -> JSONResponse:
    # R55: def (no async), sin llamar a Chatwoot ni a ningun servicio externo
    completa = all(os.environ.get(nombre) for nombre in VARIABLES_CHATWOOT)
    if completa:
        return JSONResponse({"estado": "ok"}, status_code=200)
    return JSONResponse({"estado": "degradado"}, status_code=503)
