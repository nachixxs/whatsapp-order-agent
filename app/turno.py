"""Un lote de mensajes de una conversación, con texto o adjuntos, de punta a punta (SPECS §3, §6, §7 y §11)."""

import logging
import threading
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime

from app import agente
from app.agente import (
    ConsultaGeneral,
    Decision,
    DerivarAAsesor,
    ErrorApi,
    PedirDatoFaltante,
    RegistrarPedido,
    SinTool,
)
from app.chatwoot import Adjunto, ClienteChatwoot, Contacto, MensajeEntrante
from app.confirmacion import (
    CONFIRMADO, ESCRIBIR, Salida, a_un_asesor, carrera, confirmar, derivada, pedido_confirmado,
    respuesta_al_resumen,
)
from app.config import ConfigNegocio
from app.contactos import Registro, leer_registro, nombre_del_perfil, plan_primer_contacto, pregunta_viva
from app.formato import alias_conversacion, para_log
from app.memoria import CONFIRMACION_FALLIDA, Charla, ErrorMemoria, Memoria
from app.pedidos import ArchivoAdjunto, Pedido, sumar_archivo, sumar_campos, texto_de_archivos
from app.respuestas import (
    MENSAJE_ERROR_INTERNO,
    MENSAJE_NO_ENTENDIDO,
    MENSAJE_TIPO_NO_SOPORTADO,
    acuse_de_archivos,
    acuse_de_archivos_despues_de_confirmar,
    aviso_de_descartados,
    con_aviso_de_material,
    con_pregunta_del_nombre,
    nota_de_archivos,
    pregunta_por_dato,
    respuesta_faq,
    resumen_pedido,
)
from app.sheets import Planilla
from app.tools import MATERIAL_A_DEFINIR

logger = logging.getLogger(__name__)

Decidir = Callable[..., Decision | SinTool | ErrorApi]
# Los motivos de SinTool con estado propio en el bot viejo; el resto es `sin_tool`
_SIN_TOOL_CON_ESTADO = frozenset({"argumentos_invalidos", "tool_desconocida"})
_RESUMEN = "pedido_pendiente_confirmacion"
_ERRORES = frozenset({"error_interno", CONFIRMACION_FALLIDA})  # R41: con estos, ni se registra ni se pregunta
TOPE_MENSAJE = 4096  # R35: el de WhatsApp; el historial entero vuelve a la API en cada llamada
NO_SOPORTADO = "[adjunto no soportado]"  # R36: lo que queda en el historial del lado del cliente
_planilla = Planilla()  # R53: abre la hoja recién en la primera escritura
# R28: con un solo proceso alcanza un candado en memoria. No se limpia: es un int y un Lock por
# conversación, y borrar uno que otro hilo está esperando dejaría entrar a dos turnos a la vez
_candados: dict[int, threading.Lock] = {}
_guarda = threading.Lock()


@dataclass(frozen=True)
class Resultado:  # R47: lo que sale de un lote, en este orden: la respuesta, la nota y el pase a una persona
    texto: str | None  # None: no se le manda nada al cliente
    nota: str | None = None
    derivar: bool = False


def procesar_lote(
    conversacion: int,
    mensajes: Sequence[MensajeEntrante],
    config: ConfigNegocio,
    memoria: Memoria,
    decidir: Decidir = agente.decidir,
    planilla: Planilla = _planilla,
    actualizar_contacto: Callable[[int, dict[str, object]], bool] = ClienteChatwoot().actualizar_contacto,
) -> Resultado:
    """Nunca lanza: ante un error, el error interno, sin nota ni pase (R24). Un lote, una respuesta (R30)."""
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
                    if not all(map(_de_diseno, mensaje.adjuntos)):  # R36
                        memoria.anotar_cliente(conversacion, NO_SOPORTADO, ahora)
            if not lote:
                logger.info("turno: sin mensajes nuevos alias=%s", alias)
                return Resultado(None)
            contacto = mensajes[-1].contacto
            registro = leer_registro(contacto.atributos, memoria.leer_charla(conversacion, ahora).atributos)
            archivos = _sumar_archivos(conversacion, lote, contacto.telefono, config, memoria, ahora)
            texto = None
            if any(mensaje.contenido.strip() or not mensaje.adjuntos for mensaje in lote):  # §3
                decision, texto = _turno(
                    conversacion, contacto, registro, config, memoria, decidir, planilla, ahora
                )
                if isinstance(decision, ErrorApi) and _reintentable(decision):
                    _desmarcar(memoria, nuevos)
            salida = _elegir(texto, archivos, lote, conversacion, config, memoria, ahora)
            if salida.motivo is not None:  # R47: un "sí" no confirma el resumen que ya vio una persona
                charla = memoria.leer_charla(conversacion, ahora)
                if charla.pedido is not None and charla.pedido.completo:
                    memoria.guardar_pedido(conversacion, None, charla.generacion, ahora)
                salida = derivada(salida, charla, registro and registro.nombre_cliente, config, ahora)
            if archivos is not None and archivos.nota and salida is not archivos:  # R32: no se pierde
                salida = replace(salida, nota="\n\n".join(filter(None, (salida.nota, archivos.nota))))
            if salida.marcar:
                memoria.anotar_marcador(conversacion, salida.camino, ahora)
            # R41 a R45: el primer contacto, sobre la respuesta elegida. R44: el nombre se calculó una vez
            nombre, confirmo = texto and texto.nombre, salida.camino == CONFIRMADO
            plan = plan_primer_contacto(
                registro, ahora, error=texto is not None and texto.camino in _ERRORES,
                respuesta_vacia=salida.texto is None, nombre_dicho=None if confirmo else nombre,
                nombre_perfil=nombre_del_perfil(contacto), nombre_confirmado=nombre if confirmo else None,
                repregunta_del_nombre=salida.camino == "dato_faltante: nombre_cliente",
            )
            if plan.escribir and contacto.id is not None and actualizar_contacto(contacto.id, plan.escribir):
                with suppress(ErrorMemoria):  # R3: con la fila escrita, la memoria ya no cambia la respuesta
                    memoria.anotar_atributos(conversacion, plan.escribir, ahora)
                if plan.preguntar:  # R41: solo con el alta escrita
                    salida = replace(salida, texto=con_pregunta_del_nombre(salida.texto))
        except Exception as error:  # R23, R24: el proceso falló; se desmarca y el cliente recibe el error
            # Sin marcador: no se sabe en qué quedó la memoria. R52: el tipo, nunca el mensaje del error
            logger.error("turno: fallo alias=%s error=%s", alias, type(error).__name__)
            _desmarcar(memoria, nuevos)
            return Resultado(MENSAJE_ERROR_INTERNO)
    tool = decision.tool if isinstance(decision, Decision) else "-"
    logger.info("turno: alias=%s tool=%s camino=%s", alias, para_log(tool), para_log(salida.camino))
    return Resultado(salida.texto, salida.nota, salida.motivo is not None)


def _sumar_archivos(
    conversacion: int, lote: list[MensajeEntrante], telefono: str | None, config: ConfigNegocio,
    memoria: Memoria, ahora: datetime,
) -> Salida | None:
    """R29: los archivos van al pedido en curso antes del modelo. Devuelve un error o el acuse de R32;
    None deja el acuse para cuando el texto del lote ya se aplicó (R31)."""
    adjuntos = [(mensaje, adjunto) for mensaje in lote for adjunto in mensaje.adjuntos]
    archivos = [_archivo(mensaje, adjunto, config, ahora) for mensaje, adjunto in adjuntos
                if _de_diseno(adjunto)]
    no_soportado = Salida("tipo_no_soportado", MENSAJE_TIPO_NO_SOPORTADO, error=True)  # R36: nunca silencio
    error = no_soportado if len(archivos) < len(adjuntos) else None
    if not archivos:
        return error
    charla = memoria.leer_charla(conversacion, ahora)
    confirmado = pedido_confirmado(charla)
    if confirmado is not None:  # R32: no toca la fila; lo suma la persona, con la nota. R33: el candado esperó
        acuse = Salida("archivo_despues_de_confirmar", acuse_de_archivos_despues_de_confirmar(len(archivos)))
        lineas = texto_de_archivos(confirmado.model_copy(update={"archivos": archivos}))
        return replace(error or acuse, nota=nota_de_archivos(lineas))
    pedido = charla.pedido or Pedido(telefono=telefono or "")  # R13
    for archivo in archivos:
        sumado = sumar_archivo(pedido, archivo)
        if sumado is None:  # R35: el archivo 61 no entra y se deriva (provisorio)
            error = a_un_asesor("fuera_de_alcance", config)
            break
        pedido = sumado
    memoria.guardar_pedido(conversacion, pedido, charla.generacion, ahora)  # R28: nadie lo tomó en el medio
    return error


def _de_diseno(adjunto: Adjunto) -> bool:  # R29: la imagen y el documento, como el bot viejo
    return adjunto.tipo == "file" or adjunto.tipo == "image" and adjunto.extension != "webp"  # R36: sticker


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
    recibidos = sum(_de_diseno(adjunto) for mensaje in lote for adjunto in mensaje.adjuntos)
    if texto is not None and (not recibidos or texto.texto is not None and not lote[-1].adjuntos):
        return texto
    if archivos is not None:  # R32
        return archivos
    pedido = memoria.leer_charla(conversacion, ahora).pedido  # R31: como quedó después del texto del lote
    camino = _RESUMEN if pedido.completo else "archivo_recibido"
    return Salida(camino, acuse_de_archivos(recibidos, pedido, config))


def _turno(
    conversacion: int, contacto: Contacto, registro: Registro | None, config: ConfigNegocio, memoria: Memoria,
    decidir: Decidir, planilla: Planilla, ahora: datetime,
) -> tuple[Decision | SinTool | ErrorApi, Salida]:
    """decidir → aplicar → escribir la fila (R1) o guardar el pedido (R5)."""
    charla = memoria.leer_charla(conversacion, ahora)
    confirmado = pedido_confirmado(charla)
    decision = decidir(
        config, ahora, charla, nombre_perfil=nombre_del_perfil(contacto), pedido=charla.pedido,
        nombre_preguntado=pregunta_viva(registro, ahora), confirmado=confirmado, conversacion=conversacion,
    )
    if isinstance(decision, ErrorApi):  # R12: el texto no dice qué se rompió
        return decision, Salida("error_interno", MENSAJE_ERROR_INTERNO, error=True)
    salida = carrera(decision, charla, config, ahora)
    registrado = registro and registro.nombre_cliente
    salida = salida or _aplicar(decision, charla, contacto.telefono, registrado, config, ahora)
    if salida is ESCRIBIR:
        salida = confirmar(conversacion, memoria, planilla, ahora)
    if salida.pedido is not None and not memoria.guardar_pedido(
        conversacion, salida.pedido, charla.generacion, ahora
    ):
        # R5: otro mensaje tomó el pedido mientras el modelo decidía. Con el candado (R28) no pasa dentro
        # de un proceso; si pasara, como R6 sin cambios: ni texto ni turno del bot
        salida = Salida("generacion_cambiada", None, marcar=False)
    return decision, salida


def _aplicar(
    decision: Decision | SinTool, charla: Charla, telefono: str | None, registrado: str | None,
    config: ConfigNegocio, ahora: datetime,
) -> Salida:
    if isinstance(decision, SinTool):  # R11: no se reintenta ni se improvisa
        motivo = decision.motivo
        return Salida(motivo if motivo in _SIN_TOOL_CON_ESTADO else "sin_tool", MENSAJE_NO_ENTENDIDO)
    argumentos = decision.argumentos
    if isinstance(argumentos, PedirDatoFaltante) and argumentos.dato == "nombre_cliente" and registrado:
        argumentos = RegistrarPedido()  # R43: con un nombre registrado no lo pregunta: lo completa
    if isinstance(argumentos, RegistrarPedido):
        # R13: el teléfono es el del contacto; sin él el Pedido no valida y el turno falla, no se inventa
        base = charla.pedido if charla.pedido is not None else Pedido(telefono=telefono or "")
        if base.nombre_cliente is None:  # R43: el registrado se precarga; si no parece un nombre, R14
            base = sumar_campos(base, {"nombre_cliente": registrado}, config, ahora)[0]
        return _registrar(argumentos, base, config, ahora)
    if isinstance(argumentos, PedirDatoFaltante):
        dato = argumentos.dato
        return Salida(f"dato_faltante: {dato}", pregunta_por_dato(dato, config))
    if isinstance(argumentos, ConsultaGeneral):
        tema = argumentos.tema
        return Salida(f"consulta_general: {tema}", respuesta_faq(tema, config))
    if isinstance(argumentos, DerivarAAsesor):
        return a_un_asesor(argumentos.motivo, config)
    return respuesta_al_resumen(argumentos, charla.pedido)


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
    dicho = pedido.nombre_cliente if argumentos.nombre_cliente and "nombre_cliente" not in descartados else None
    return Salida(camino, texto, pedido=pedido, nombre=dicho)  # R44: el nombre dicho, ya validado (R14)


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
