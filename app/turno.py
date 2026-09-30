"""Un lote de mensajes de una conversación, con texto o adjuntos, de punta a punta (SPECS §3, §6, §7 y §11)."""

import logging
import threading
from collections.abc import Callable, Sequence
from contextlib import suppress
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
from app.chatwoot import Adjunto, Contacto, MensajeEntrante
from app.confirmacion import CONFIRMADO, Salida, carrera, confirmar, pedido_confirmado
from app.config import ConfigNegocio
from app.formato import alias_conversacion, en_una_linea, para_log, solo_digitos
from app.memoria import Charla, ErrorMemoria, Memoria
from app.pedidos import TOPE_NOMBRE, ArchivoAdjunto, Pedido, sumar_archivo, sumar_campos
from app.respuestas import (
    MENSAJE_ERROR_INTERNO,
    MENSAJE_NO_ENTENDIDO,
    MENSAJE_PEDIDO_RECHAZADO,
    MENSAJE_TIPO_NO_SOPORTADO,
    acuse_de_archivos,
    acuse_de_archivos_despues_de_confirmar,
    aviso_de_descartados,
    con_aviso_de_material,
    pregunta_por_dato,
    respuesta_faq,
    resumen_pedido,
    texto_derivacion,
)
from app.sheets import Planilla
from app.tools import MATERIAL_A_DEFINIR

logger = logging.getLogger(__name__)

Decidir = Callable[..., Decision | SinTool | ErrorApi]
# Los motivos de SinTool con estado propio en el bot viejo; el resto es `sin_tool`
_SIN_TOOL_CON_ESTADO = frozenset({"argumentos_invalidos", "tool_desconocida"})
_RESUMEN = "pedido_pendiente_confirmacion"
TOPE_MENSAJE = 4096  # R35: el de WhatsApp; el historial entero vuelve a la API en cada llamada
TIPOS_DE_DISENO = frozenset({"image", "file"})  # R29: la imagen y el documento del bot viejo; el resto, R36
NO_SOPORTADO = "[adjunto no soportado]"  # R36: lo que queda en el historial del lado del cliente
_ESCRIBIR = Salida("escribir_fila", None)  # R1: la fila la escribe _turno, que tiene la memoria y la planilla
_planilla = Planilla()  # R53: abre la hoja recién en la primera escritura
# R28: con un solo proceso alcanza un candado en memoria. No se limpia: es un int y un Lock por
# conversación, y borrar uno que otro hilo está esperando dejaría entrar a dos turnos a la vez
_candados: dict[int, threading.Lock] = {}
_guarda = threading.Lock()


def procesar_lote(
    conversacion: int,
    mensajes: Sequence[MensajeEntrante],
    config: ConfigNegocio,
    memoria: Memoria,
    decidir: Decidir = agente.decidir,
    planilla: Planilla = _planilla,
) -> str | None:
    """El texto para el cliente, o None si no se le manda nada. Nunca lanza.

    Quien llama (main.py) pasa los mensajes de esta conversación ya filtrados (R47 a R49), con texto o con
    adjuntos; lo corre fuera del event loop (SQLite, la API y la planilla bloquean) y, si vuelve un texto,
    lo manda con `ClienteChatwoot.responder(conversacion, texto)`. Un lote, una respuesta (R30).
    """
    alias = alias_conversacion(conversacion)
    nuevos: list[int] = []
    lote: list[MensajeEntrante] = []
    decision: Decision | SinTool | ErrorApi | None = None
    with _candado(conversacion):  # R28, R5. R4, R33: lo que llega durante la escritura espera acá
        try:
            ahora = config.ahora()  # R37: un solo reloj para todo el lote
            memoria.barrer(ahora)
            for mensaje in mensajes:
                # R23. Sin id no hay compuerta, y sin compuerta no se procesa (R24)
                if mensaje.id_mensaje is not None and memoria.marcar_procesado(mensaje.id_mensaje, ahora):
                    nuevos.append(mensaje.id_mensaje)
                    lote.append(mensaje)
                    if mensaje.contenido.strip():  # R34: también el epígrafe de un archivo
                        memoria.anotar_cliente(conversacion, mensaje.contenido[:TOPE_MENSAJE], ahora)
                    if any(adjunto.tipo not in TIPOS_DE_DISENO for adjunto in mensaje.adjuntos):  # R36
                        memoria.anotar_cliente(conversacion, NO_SOPORTADO, ahora)
            if not lote:
                logger.info("turno: sin mensajes nuevos alias=%s", alias)
                return None
            contacto = mensajes[-1].contacto
            archivos = _sumar_archivos(conversacion, lote, contacto.telefono, config, memoria, ahora)
            texto = None
            if any(mensaje.contenido.strip() or not mensaje.adjuntos for mensaje in lote):  # §3
                decision, texto = _turno(conversacion, contacto, config, memoria, decidir, planilla, ahora)
                if isinstance(decision, ErrorApi) and _reintentable(decision):
                    _desmarcar(memoria, nuevos)
            salida = _elegir(texto, archivos, lote, conversacion, config, memoria, ahora)
            if salida.marcar:
                memoria.anotar_marcador(conversacion, salida.camino, ahora)
        except Exception as error:  # R23, R24: el proceso falló; se desmarca y el cliente recibe el error
            # Sin marcador: no se sabe en qué quedó la memoria. R52: el tipo, nunca el mensaje del error
            logger.error("turno: fallo alias=%s error=%s", alias, type(error).__name__)
            _desmarcar(memoria, nuevos)
            return MENSAJE_ERROR_INTERNO
    tool = decision.tool if isinstance(decision, Decision) else "-"
    logger.info("turno: alias=%s tool=%s camino=%s", alias, para_log(tool), para_log(salida.camino))
    return salida.texto


def _sumar_archivos(
    conversacion: int, lote: list[MensajeEntrante], telefono: str | None, config: ConfigNegocio,
    memoria: Memoria, ahora: datetime,
) -> Salida | None:
    """R29: los archivos van al pedido en curso antes del modelo. Devuelve un error o el acuse de R32;
    None deja el acuse para cuando el texto del lote ya se aplicó (R31)."""
    adjuntos = [(mensaje, adjunto) for mensaje in lote for adjunto in mensaje.adjuntos]
    archivos = [_archivo(mensaje, adjunto, config, ahora) for mensaje, adjunto in adjuntos
                if adjunto.tipo in TIPOS_DE_DISENO]
    no_soportado = Salida("tipo_no_soportado", MENSAJE_TIPO_NO_SOPORTADO, error=True)  # R36: nunca silencio
    error = no_soportado if len(archivos) < len(adjuntos) else None
    if not archivos:
        return error
    charla = memoria.leer_charla(conversacion, ahora)
    if pedido_confirmado(charla) is not None:  # R32: no toca la fila. R33: el candado esperó la escritura
        acuse = acuse_de_archivos_despues_de_confirmar(len(archivos))
        return error or Salida("archivo_despues_de_confirmar", acuse)
    pedido = charla.pedido or Pedido(telefono=telefono or "")  # R13
    for archivo in archivos:
        sumado = sumar_archivo(pedido, archivo)
        if sumado is None:  # R35: el archivo 61 no entra y se deriva (provisorio)
            motivo = "fuera_de_alcance"
            error = Salida(f"derivado_a_asesor: {motivo}", texto_derivacion(motivo, config), error=True)
            break
        pedido = sumado
    memoria.guardar_pedido(conversacion, pedido, charla.generacion, ahora)  # R28: nadie lo tomó en el medio
    return error


def _archivo(
    mensaje: MensajeEntrante, adjunto: Adjunto, config: ConfigNegocio, ahora: datetime
) -> ArchivoAdjunto:
    hora = (mensaje.creado or ahora).astimezone(config.zona).replace(tzinfo=None)  # R29: hora de pared
    return ArchivoAdjunto(
        id_adjunto=adjunto.id, id_mensaje=mensaje.id_mensaje, tipo=adjunto.extension or adjunto.tipo,
        tamano=adjunto.tamano, hora=hora,
    )


def _elegir(
    texto: Salida | None, archivos: Salida | None, lote: list[MensajeEntrante], conversacion: int,
    config: ConfigNegocio, memoria: Memoria, ahora: datetime,
) -> Salida:
    """R30: una respuesta por lote. Como el bot viejo: la confirmación exitosa gana (R3), después un error
    o una derivación (ningún acuse los tapa) y, si no, responde el último mensaje."""
    if texto is not None and texto.camino == CONFIRMADO:
        return texto
    for salida in (archivos, texto):
        if salida is not None and salida.error:
            return salida
    recibidos = sum(adjunto.tipo in TIPOS_DE_DISENO for mensaje in lote for adjunto in mensaje.adjuntos)
    if texto is not None and (not recibidos or texto.texto is not None and not lote[-1].adjuntos):
        return texto
    if archivos is not None:  # R32
        return archivos
    pedido = memoria.leer_charla(conversacion, ahora).pedido  # R31: como quedó después del texto del lote
    camino = _RESUMEN if pedido.completo else "archivo_recibido"
    return Salida(camino, acuse_de_archivos(recibidos, pedido, config))


def _turno(
    conversacion: int, contacto: Contacto, config: ConfigNegocio, memoria: Memoria, decidir: Decidir,
    planilla: Planilla, ahora: datetime,
) -> tuple[Decision | SinTool | ErrorApi, Salida]:
    """decidir → aplicar → escribir la fila (R1) o guardar el pedido (R5)."""
    charla = memoria.leer_charla(conversacion, ahora)
    confirmado = pedido_confirmado(charla)
    # nombre_preguntado (R42) llega con la 4.5
    decision = decidir(
        config, ahora, charla, nombre_perfil=_nombre_perfil(contacto), pedido=charla.pedido,
        confirmado=confirmado, conversacion=conversacion,
    )
    if isinstance(decision, ErrorApi):  # R12: el texto no dice qué se rompió
        return decision, Salida("error_interno", MENSAJE_ERROR_INTERNO, error=True)
    salida = None if confirmado is None else carrera(decision, confirmado, config, ahora)
    salida = salida or _aplicar(decision, charla, contacto.telefono, config, ahora)
    if salida is _ESCRIBIR:
        salida = confirmar(conversacion, memoria, planilla, ahora)
    if salida.pedido is not None and not memoria.guardar_pedido(
        conversacion, salida.pedido, charla.generacion, ahora
    ):
        # R5: otro mensaje tomó el pedido mientras el modelo decidía. Con el candado (R28) no pasa dentro
        # de un proceso; si pasara, como R6 sin cambios: ni texto ni turno del bot
        salida = Salida("generacion_cambiada", None, marcar=False)
    return decision, salida


def _aplicar(
    decision: Decision | SinTool, charla: Charla, telefono: str | None, config: ConfigNegocio,
    ahora: datetime,
) -> Salida:
    if isinstance(decision, SinTool):  # R11: no se reintenta ni se improvisa
        motivo = decision.motivo
        return Salida(motivo if motivo in _SIN_TOOL_CON_ESTADO else "sin_tool", MENSAJE_NO_ENTENDIDO)
    argumentos = decision.argumentos
    if isinstance(argumentos, RegistrarPedido):
        # R13: el teléfono es el del contacto; sin él el Pedido no valida y el turno falla, no se inventa
        base = charla.pedido if charla.pedido is not None else Pedido(telefono=telefono or "")
        return _registrar(argumentos, base, config, ahora)
    if isinstance(argumentos, PedirDatoFaltante):  # R43 (4.5): con un nombre registrado no lo pregunta
        dato = argumentos.dato
        return Salida(f"dato_faltante: {dato}", pregunta_por_dato(dato, config))
    if isinstance(argumentos, ConsultaGeneral):
        tema = argumentos.tema
        return Salida(f"consulta_general: {tema}", respuesta_faq(tema, config))
    if isinstance(argumentos, DerivarAAsesor):
        # R47 (CP4): acá se deriva en Chatwoot: la conversación pasa a una persona, con la nota interna
        motivo = argumentos.motivo  # R15, R30: como un error, ningún acuse del lote tapa la derivación
        return Salida(f"derivado_a_asesor: {motivo}", texto_derivacion(motivo, config), error=True)
    return _confirmar(argumentos, charla.pedido)


def _registrar(
    argumentos: RegistrarPedido, base: Pedido, config: ConfigNegocio, ahora: datetime
) -> Salida:
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
    return Salida(camino, texto, pedido=pedido)


def _confirmar(argumentos: ConfirmarPedido, pedido: Pedido | None) -> Salida:
    if pedido is None or not pedido.completo:  # R1
        return Salida("sin_pedido_para_confirmar", MENSAJE_NO_ENTENDIDO)
    if not argumentos.acepta:  # R8: se vuelve a recolectar con los campos que ya tenía
        return Salida("pedido_rechazado", MENSAJE_PEDIDO_RECHAZADO)
    return _ESCRIBIR


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
