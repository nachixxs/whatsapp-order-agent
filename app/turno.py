"""Un lote de mensajes de texto de una conversación, de punta a punta (SPECS §3, §6 y §7)."""

import logging
import threading
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime

from app import agente
from app.agente import (
    ConfirmarPedido,
    ConsultaGeneral,
    Decision,
    DerivarAAsesor,
    ErrorApi,
    PedirDatoFaltante,
    RegistrarPedido,
    SinTool,
)
from app.chatwoot import Contacto, MensajeEntrante
from app.config import ConfigNegocio
from app.formato import alias_conversacion, en_una_linea, para_log, solo_digitos
from app.memoria import Charla, ErrorMemoria, Memoria
from app.pedidos import TOPE_NOMBRE, Pedido, sumar_campos
from app.respuestas import (
    MENSAJE_ERROR_INTERNO,
    MENSAJE_NO_ENTENDIDO,
    MENSAJE_PEDIDO_RECHAZADO,
    aviso_de_descartados,
    con_aviso_de_material,
    pregunta_por_dato,
    respuesta_faq,
    resumen_pedido,
    texto_derivacion,
)
from app.tools import MATERIAL_A_DEFINIR

logger = logging.getLogger(__name__)

Decidir = Callable[..., Decision | SinTool | ErrorApi]
# Los motivos de SinTool con estado propio en el bot viejo; el resto es `sin_tool`
_SIN_TOOL_CON_ESTADO = frozenset({"argumentos_invalidos", "tool_desconocida"})
_RESUMEN = "pedido_pendiente_confirmacion"
TOPE_MENSAJE = 4096  # R35: el de WhatsApp; el historial entero vuelve a la API en cada llamada
# R28: con un solo proceso alcanza un candado en memoria. No se limpia: es un int y un Lock por
# conversación, y borrar uno que otro hilo está esperando dejaría entrar a dos turnos a la vez
_candados: dict[int, threading.Lock] = {}
_guarda = threading.Lock()


@dataclass(frozen=True)
class _Salida:
    camino: str  # va al log y, con `marcar`, al historial como marcador sin corchetes (R21)
    texto: str | None  # R19: solo textos de respuestas.py; None es no mandar nada
    pedido: Pedido | None = None  # el que hay que guardar; None no toca el guardado
    marcar: bool = True


def procesar_lote(
    conversacion: int,
    mensajes: Sequence[MensajeEntrante],
    config: ConfigNegocio,
    memoria: Memoria,
    decidir: Decidir = agente.decidir,
) -> str | None:
    """El texto para el cliente, o None si no se le manda nada. Nunca lanza.

    Quien llama (main.py) pasa mensajes de texto de esta conversación ya filtrados (R47 a R49), lo
    corre fuera del event loop (SQLite y la API bloquean) y, si vuelve un texto, lo manda con
    `ClienteChatwoot.responder(conversacion, texto)`. Los archivos no pasan por acá (tarea 3.3).
    """
    alias = alias_conversacion(conversacion)
    nuevos: list[int] = []
    with _candado(conversacion):  # R28, R5: el mensaje siguiente lee la charla con este turno guardado
        try:
            ahora = config.ahora()  # R37: un solo reloj para todo el lote
            memoria.barrer(ahora)
            for mensaje in mensajes:
                # R23. Sin id no hay compuerta, y sin compuerta no se procesa (R24)
                if mensaje.id_mensaje is not None and memoria.marcar_procesado(mensaje.id_mensaje, ahora):
                    nuevos.append(mensaje.id_mensaje)
                    memoria.anotar_cliente(conversacion, mensaje.contenido[:TOPE_MENSAJE], ahora)
            if not nuevos:
                logger.info("turno: sin mensajes nuevos alias=%s", alias)
                return None
            decision, salida = _turno(conversacion, mensajes[-1].contacto, config, memoria, decidir, ahora)
            if isinstance(decision, ErrorApi) and _reintentable(decision):
                _desmarcar(memoria, nuevos)
        except Exception as error:  # R23, R24: el proceso falló; se desmarca y el cliente recibe el error
            # Sin marcador: no se sabe en qué quedó la memoria. R52: el tipo, nunca el mensaje del error
            logger.error("turno: fallo alias=%s error=%s", alias, type(error).__name__)
            _desmarcar(memoria, nuevos)
            return MENSAJE_ERROR_INTERNO
    tool = decision.tool if isinstance(decision, Decision) else "-"
    logger.info("turno: alias=%s tool=%s camino=%s", alias, para_log(tool), para_log(salida.camino))
    return salida.texto


def _turno(
    conversacion: int, contacto: Contacto, config: ConfigNegocio, memoria: Memoria, decidir: Decidir,
    ahora: datetime,
) -> tuple[Decision | SinTool | ErrorApi, _Salida]:
    """decidir → aplicar → guardar el pedido (R5) → marcador (R21)."""
    charla = memoria.leer_charla(conversacion, ahora)
    # nombre_preguntado (R42) llega con la 4.5 y confirmado (R7) con la 3.4
    decision = decidir(
        config, ahora, charla, nombre_perfil=_nombre_perfil(contacto), pedido=charla.pedido,
        conversacion=conversacion,
    )
    if isinstance(decision, ErrorApi):  # R12: el texto no dice qué se rompió
        salida = _Salida("error_interno", MENSAJE_ERROR_INTERNO)
    else:
        salida = _aplicar(decision, charla, contacto.telefono, config, ahora)
    if salida.pedido is not None and not memoria.guardar_pedido(
        conversacion, salida.pedido, charla.generacion, ahora
    ):
        # R5: otro mensaje tomó el pedido mientras el modelo decidía, y este no lo pisa. Como R6 sin
        # cambios: ni texto ni turno del bot. La 3.4 separa el que cambia algo, que deriva
        salida = _Salida("generacion_cambiada", None, marcar=False)
    if salida.marcar:
        memoria.anotar_marcador(conversacion, salida.camino, ahora)
    return decision, salida


def _aplicar(
    decision: Decision | SinTool, charla: Charla, telefono: str | None, config: ConfigNegocio,
    ahora: datetime,
) -> _Salida:
    if isinstance(decision, SinTool):  # R11: no se reintenta ni se improvisa
        motivo = decision.motivo
        return _Salida(motivo if motivo in _SIN_TOOL_CON_ESTADO else "sin_tool", MENSAJE_NO_ENTENDIDO)
    argumentos = decision.argumentos
    if isinstance(argumentos, RegistrarPedido):
        # R13: el teléfono es el del contacto; sin él el Pedido no valida y el turno falla, no se inventa
        base = charla.pedido if charla.pedido is not None else Pedido(telefono=telefono or "")
        return _registrar(argumentos, base, config, ahora)
    if isinstance(argumentos, PedirDatoFaltante):  # R43 (4.5): con un nombre registrado no lo pregunta
        dato = argumentos.dato
        return _Salida(f"dato_faltante: {dato}", pregunta_por_dato(dato, config))
    if isinstance(argumentos, ConsultaGeneral):
        tema = argumentos.tema
        return _Salida(f"consulta_general: {tema}", respuesta_faq(tema, config))
    if isinstance(argumentos, DerivarAAsesor):
        # R47 (CP4): acá se deriva en Chatwoot: la conversación pasa a una persona, con la nota interna
        motivo = argumentos.motivo
        return _Salida(f"derivado_a_asesor: {motivo}", texto_derivacion(motivo, config))
    return _confirmar(argumentos, charla.pedido)


def _registrar(
    argumentos: RegistrarPedido, base: Pedido, config: ConfigNegocio, ahora: datetime
) -> _Salida:
    """R14: suma campo por campo; contesta el aviso de lo descartado, el resumen o la repregunta."""
    pedido, descartados = sumar_campos(base, argumentos.model_dump(), config, ahora)
    aviso = aviso_de_descartados(descartados, pedido, config)
    if aviso is not None:  # va en lugar de la repregunta o del resumen
        camino = "producto_invalido" if "producto" in descartados else "fecha_invalida"
        texto = aviso
    elif pedido.completo:  # §7: queda PENDIENTE en SQLite; la planilla no se toca
        camino, texto = _RESUMEN, resumen_pedido(pedido, config)
    else:
        dato = pedido.faltantes()[0]
        camino, texto = f"dato_faltante: {dato}", pregunta_por_dato(dato, config)
    # R15: el aviso sale en el turno que lo dejó a definir, no en cada reenvío del modelo
    if pedido.material == MATERIAL_A_DEFINIR and base.material != MATERIAL_A_DEFINIR:
        texto = con_aviso_de_material(texto, es_resumen=camino == _RESUMEN)
    return _Salida(camino, texto, pedido=pedido)


def _confirmar(argumentos: ConfirmarPedido, pedido: Pedido | None) -> _Salida:
    if pedido is None or not pedido.completo:  # R1
        return _Salida("sin_pedido_para_confirmar", MENSAJE_NO_ENTENDIDO)
    if not argumentos.acepta:  # R8: se vuelve a recolectar con los campos que ya tenía
        return _Salida("pedido_rechazado", MENSAJE_PEDIDO_RECHAZADO)
    # R1, R2: en el CP2 no hay planilla, así que no hay fila: ni "¡Listo!" ni marcador de confirmado, y
    # el pedido sigue PENDIENTE. La 3.x pone acá la toma atómica (R4), la escritura y el cierre (R22)
    return _Salida("confirmacion_sin_planilla", None, marcar=False)


def _nombre_perfil(contacto: Contacto) -> str | None:
    """R43: sin perfil, Chatwoot le pone el teléfono de nombre; el teléfono nunca va a la API (R13)."""
    # R35: va al prompt entre comillas; un salto de línea metería texto en el bloque dinámico
    nombre = en_una_linea(contacto.nombre or "")[:TOPE_NOMBRE].rstrip()
    telefono = solo_digitos(contacto.telefono or "")
    if not nombre or (telefono and solo_digitos(nombre) == telefono):
        return None
    return nombre


def _reintentable(error: ErrorApi) -> bool:
    """R23: se desmarca lo transitorio. Credencial, un 4xx o un error desconocido fallarían igual."""
    if error.tipo in ("timeout", "conexion"):
        return True
    estado = error.estado_http or 0
    return error.tipo == "http" and (estado in (408, 409, 429) or estado >= 500)  # los del SDK


def _desmarcar(memoria: Memoria, ids: list[int]) -> None:
    with suppress(ErrorMemoria):  # la memoria ya dejó en su log la tabla y el tipo de error
        for id_mensaje in ids:
            memoria.desmarcar_procesado(id_mensaje)


def _candado(conversacion: int) -> threading.Lock:
    with _guarda:  # sin la guarda, dos hilos podrían crear cada uno su Lock para la misma conversación
        return _candados.setdefault(conversacion, threading.Lock())
