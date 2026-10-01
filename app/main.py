"""Webhook de Chatwoot: autentica, filtra, junta las rafagas de archivos (R30) y manda el lote al turno."""

import hashlib
import hmac
import logging
import os
import sys
import threading
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from app.chatwoot import ClienteChatwoot, MensajeEntrante, parsear_estado, parsear_evento
from app.confirmacion import soltar_pedido_completo
from app.config import RUTA_POR_DEFECTO, ConfigNegocio, cargar_config
from app.formato import alias_conversacion, para_log
from app.memoria import Memoria
from app.turno import procesar_lote

# CLAUDE.md "Reglas duras del codigo": ruta explicita, sin ruta agarra el .env de otro checkout
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

logger = logging.getLogger(__name__)

CARPETA_CAPTURAS = Path(__file__).resolve().parent.parent / "capturas"
VARIABLES_CHATWOOT = (  # R55: sin el token de agente, R45 (el nombre en el contacto) queda apagado sin aviso
    "CHATWOOT_URL", "CHATWOOT_ACCOUNT_ID", "CHATWOOT_INBOX_ID",
    "CHATWOOT_BOT_TOKEN", "CHATWOOT_AGENTE_TOKEN", "CHATWOOT_WEBHOOK_SECRET",
)
FORMATO_LOG = "%(asctime)s %(levelname)s %(name)s: %(message)s"
TOPE_BYTES_BODY = 1_000_000
TOLERANCIA_SEGUNDOS = 300
ESPERA_LOTE_SEGUNDOS = 8.0  # R30: silencio que cierra la ventana de una rafaga de archivos
# Raiz del repo (`*.db` esta en .gitignore): la base nunca se versiona y no depende del cwd
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
    # R54: config o base rota se descubren al arrancar. Solo corre con el servidor (o `with TestClient`)
    if workers_pedidos(sys.argv, os.environ) > 1:
        # R28: dos procesos sobre el mismo archivo SQLite pisan la misma charla
        raise RuntimeError("R28: el bot corre con un solo worker (quitar --workers / WEB_CONCURRENCY)")
    # No hace nada si el root ya tiene handlers; los loggers de uvicorn no propagan, no se duplican
    logging.basicConfig(level=logging.INFO, format=FORMATO_LOG)
    app.state.config = cargar_config(RUTA_POR_DEFECTO)
    app.state.memoria = Memoria(os.environ.get("MEMORIA_RUTA") or MEMORIA_RUTA_POR_DEFECTO)
    try:
        yield
    finally:
        app.state.memoria.cerrar()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=_ciclo_de_vida)

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
    try:  # variable ausente o no numerica: int() lanza y la cuenta no es valida
        esperado = (int(os.environ.get("CHATWOOT_ACCOUNT_ID", "")), int(os.environ.get("CHATWOOT_INBOX_ID", "")))
    except ValueError:
        return False
    return (mensaje.account_id, mensaje.inbox_id) == esperado


@dataclass
class _Ventana:
    lote: list[MensajeEntrante] = field(default_factory=list)
    timer: threading.Timer | None = None


_ventanas: dict[int, _Ventana] = {}
_candado = threading.Lock()


def _acumular(mensaje: MensajeEntrante, cliente: ClienteChatwoot, config: ConfigNegocio, memoria: Memoria) -> bool:
    """R30: un adjunto abre la ventana de su conversacion; todo mensaje que llega con la ventana abierta se
    suma y reinicia la espera. False si no hay ventana ni adjunto: el mensaje sale directo."""
    conversacion = mensaje.id_conversacion
    with _candado:
        ventana = _ventanas.get(conversacion)
        if ventana is None:
            if not mensaje.adjuntos:
                return False
            ventana = _ventanas[conversacion] = _Ventana()
        ventana.lote.append(mensaje)
        if ventana.timer is not None:
            ventana.timer.cancel()
        # Timer y no una tarea del event loop: sobrevive al request y `procesar_lote` bloquea
        ventana.timer = threading.Timer(ESPERA_LOTE_SEGUNDOS, _cerrar_ventana, (conversacion, cliente, config, memoria))
        ventana.timer.daemon = True
        ventana.timer.start()
    return True


def _cerrar_ventana(conversacion: int, cliente: ClienteChatwoot, config: ConfigNegocio, memoria: Memoria) -> None:
    with _candado:
        ventana = _ventanas.pop(conversacion, None)
    if ventana is not None:
        _procesar_turno(conversacion, ventana.lote, cliente, config, memoria)


def _descartar_ventana(conversacion: int) -> None:
    """R47, R30: la conversacion salio de `pending`, lo acumulado no se procesa."""
    with _candado:
        ventana = _ventanas.pop(conversacion, None)
    if ventana is not None and ventana.timer is not None:
        ventana.timer.cancel()


def _procesar_turno(
    id_conversacion: int, lote: list[MensajeEntrante], cliente: ClienteChatwoot, config: ConfigNegocio, memoria: Memoria
) -> None:
    """Corre en el threadpool (BackgroundTasks) o en el hilo del timer: SQLite y la API bloquean, y el
    event loop tiene que seguir libre para el webhook (R50)."""
    resultado = procesar_lote(id_conversacion, lote, config, memoria)
    # R47: respuesta, nota y open en ese orden; un paso que falla no frena a los siguientes
    pasos = (("responder", resultado.texto, cliente.responder), ("nota_interna", resultado.nota, cliente.nota_interna))
    fallos = [paso for paso, texto, enviar in pasos if texto is not None and not enviar(id_conversacion, texto)]
    if resultado.derivar and not cliente.pasar_a_persona(id_conversacion):
        fallos.append("pasar_a_persona")
    for paso in fallos:
        logger.error("Webhook Chatwoot: fallo en el paso %s alias=%s", paso, alias_conversacion(id_conversacion))  # R52


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
    try:
        if int(request.headers.get("content-length", 0)) > TOPE_BYTES_BODY:
            return None
    except ValueError:  # no numerico o de miles de digitos: manda el conteo del stream
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
        return JSONResponse({"estado": "ignorado"}, status_code=413)  # R50: 413 no dispara reintentos (solo 429/500)
    firma = request.headers.get("x-chatwoot-signature", "")
    timestamp = request.headers.get("x-chatwoot-timestamp", "")
    if not _firma_valida(cuerpo, firma, timestamp, config):
        logger.warning("Webhook Chatwoot: autenticacion fallida")
        return {"estado": "ignorado"}
    mensaje = parsear_evento(cuerpo)
    evento = mensaje or parsear_estado(cuerpo)  # R47: los eventos de estado y de la persona tambien cuentan
    if evento is None or evento.id_conversacion is None:
        logger.info("Webhook Chatwoot: evento descartado")
        return {"estado": "ignorado"}
    if not _cuenta_valida(evento):
        logger.warning("Webhook Chatwoot: account o inbox inesperado")
        return {"estado": "ignorado"}
    alias = alias_conversacion(evento.id_conversacion)
    if evento.estado_conversacion != "pending":
        # R47: con `open`, `snoozed`, `resolved` o sin estado no es del bot: ni turno ni archivos pendientes, y un
        # "si" no confirma el resumen que vio una persona si la conversacion vuelve a `pending`
        logger.info("Webhook Chatwoot: conversacion no pendiente estado=%s alias=%s", para_log(evento.estado_conversacion), alias)
        _descartar_ventana(evento.id_conversacion)
        background_tasks.add_task(soltar_pedido_completo, evento.id_conversacion, config, memoria)
        return {"estado": "ignorado"}
    if mensaje is None:  # `pending` pero no es un mensaje entrante (R48)
        return {"estado": "ignorado"}
    logger.info("Webhook Chatwoot: mensaje aceptado alias=%s evento=%s adjuntos=%d", alias, para_log(mensaje.evento), len(mensaje.adjuntos))
    _capturar_payload(cuerpo)
    if not _acumular(mensaje, cliente, config, memoria):
        background_tasks.add_task(_procesar_turno, evento.id_conversacion, [mensaje], cliente, config, memoria)
    return {"estado": "ok"}


@app.get("/salud")
def salud() -> JSONResponse:
    # R55: def (no async), sin llamar a Chatwoot ni a ningun servicio externo
    completa = all(os.environ.get(nombre) for nombre in VARIABLES_CHATWOOT)
    return JSONResponse({"estado": "ok" if completa else "degradado"}, status_code=200 if completa else 503)
