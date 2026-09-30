"""La confirmación de un pedido: la toma, la fila en la planilla y la carrera del "sí" (SPECS §7, R1 a R7)."""

import logging
from dataclasses import dataclass
from datetime import datetime

from app.agente import ConfirmarPedido, Decision, PedirDatoFaltante, RegistrarPedido, SinTool
from app.config import ConfigNegocio
from app.memoria import CONFIRMACION_FALLIDA, Charla, Memoria
from app.pedidos import Pedido, sumar_campos, texto_de_archivos
from app.respuestas import (
    MENSAJE_CAMBIO_DURANTE_LA_CONFIRMACION, MENSAJE_ERROR_AL_GUARDAR, MENSAJE_NO_ENTENDIDO,
    MENSAJE_PEDIDO_CONFIRMADO,
)
from app.sheets import ErrorPlanilla, Planilla

logger = logging.getLogger(__name__)

CONFIRMADO = "pedido_confirmado"


@dataclass(frozen=True)
class Salida:
    camino: str  # va al log y, con `marcar`, al historial como marcador sin corchetes (R21)
    texto: str | None  # R19: solo textos de respuestas.py; None es no mandar nada
    pedido: Pedido | None = None  # el que hay que guardar; None no toca el guardado
    marcar: bool = True
    error: bool = False  # R30: en el lote, ningún acuse lo tapa


def pedido_confirmado(charla: Charla) -> Pedido | None:
    """R6, R7: el pedido ya escrito (R2), si no hay otro en curso (como el bot viejo)."""
    toma = charla.toma
    return toma.pedido if toma is not None and toma.escrita and charla.pedido is None else None


def carrera(
    decision: Decision | SinTool, confirmado: Pedido, config: ConfigNegocio, ahora: datetime
) -> Salida | None:
    """R6. None es el camino normal: con el confirmado en el prompt (R7), un cambio llega como
    derivar_a_asesor y un registrar_pedido con otros datos es un pedido nuevo."""
    argumentos = decision.argumentos if isinstance(decision, Decision) else None
    if isinstance(argumentos, ConfirmarPedido) and not argumentos.acepta:  # un rechazo cambia, como en el viejo
        return Salida("cambio_sobre_pedido_confirmado", MENSAJE_CAMBIO_DURANTE_LA_CONFIRMACION)
    if isinstance(argumentos, RegistrarPedido):
        campos = argumentos.model_dump(exclude={"nombre_cliente"})  # el nombre no es un cambio
        pedido, _ = sumar_campos(confirmado, campos, config, ahora)  # por valor; un descartado no cambia nada
        if pedido != confirmado:
            return None
    elif not isinstance(argumentos, ConfirmarPedido | PedirDatoFaltante):
        return None
    return Salida("pedido_confirmado_sin_cambios", None, marcar=False)


def confirmar(conversacion: int, memoria: Memoria, planilla: Planilla, ahora: datetime) -> Salida:
    """R1 a R4: la única escritura de la planilla. La toma se cierra en todos los caminos."""
    toma = memoria.tomar_para_confirmar(conversacion, ahora)  # R4: un segundo "sí" no la encuentra
    if toma is None:
        return Salida("sin_pedido_para_confirmar", MENSAJE_NO_ENTENDIDO)
    try:
        planilla.escribir_fila(_fila(toma.pedido, ahora))
    except Exception as error:  # R2: una toma sin cerrar trabaría la conversación 6 horas
        memoria.devolver_a_pendiente(conversacion, toma, ahora)  # deja el marcador de la falla
        if not isinstance(error, ErrorPlanilla):
            raise
        return Salida(CONFIRMACION_FALLIDA, MENSAJE_ERROR_AL_GUARDAR, marcar=False, error=True)
    try:
        memoria.confirmar_escrito(conversacion, toma, ahora)
    except Exception as error:  # R3: una fila escrita no se desdice
        logger.error("confirmacion: fila escrita sin cerrar la toma error=%s", type(error).__name__)
    return Salida(CONFIRMADO, MENSAJE_PEDIDO_CONFIRMADO, marcar=False)  # R22: la charla ya se cerró


def _fila(pedido: Pedido, ahora: datetime) -> dict[str, str]:
    """R9: las columnas de §5 como texto; las fechas como el bot viejo, con `ahora` en la zona del negocio."""
    fila = {campo: str(valor) for campo, valor in pedido.model_dump(exclude={"archivos"}).items()}
    return fila | {"fecha_ingreso": f"{ahora:%Y-%m-%d %H:%M:%S}", "archivos": texto_de_archivos(pedido)}
