import logging
from datetime import timedelta

import pytest

from app.agente import Decision, PedirDatoFaltante
from app.config import ConfigNegocio
from app.memoria import CARRERA, ErrorMemoria, Memoria
from app.pedidos import Pedido
from app.respuestas import (
    MENSAJE_CAMBIO_DURANTE_LA_CONFIRMACION,
    MENSAJE_ERROR_AL_GUARDAR,
    MENSAJE_ERROR_INTERNO,
    MENSAJE_PEDIDO_CONFIRMADO,
    acuse_de_archivos,
    con_frase_de_horario,
    nota_de_archivos,
    nota_de_derivacion,
    pregunta_por_dato,
    respuesta_faq,
)
from app.sheets import ErrorPlanilla
from app.turno import Resultado, procesar_lote
from tests.conftest import HORA_DE_PRUEBA, TELEFONO
from tests.test_turno import (
    COMPLETO,
    CONV,
    HORARIOS,
    NOMBRE,
    _adjunto,
    _Agente,
    _archivo,
    _charla,
    _confirmado,
    _confirmar,
    _historial,
    _lanzar,
    _lote,
    _mensaje,
    _pedido,
    _pendiente,
    _Planilla,
    _registrar,
    _turno,
    memoria,  # el fixture: pytest lo pide por nombre
)

# R9: las columnas de §5 menos anticipo; fecha_ingreso con el reloj del negocio (HORA_DE_PRUEBA)
FILA = {
    "fecha_ingreso": "2026-10-06 10:00:00", "nombre_cliente": NOMBRE, "telefono": TELEFONO,
    "producto": "sellos", "material": "goma", "medidas": "4x2 cm", "cantidad": "3", "tiene_diseno": "si",
    "archivos": "", "fecha_necesita": "2026-10-09",
}
CAMBIO = "cambio_sobre_pedido_confirmado"  # R6: el motivo de la derivación
REPREGUNTA = Decision("pedir_dato_faltante", PedirDatoFaltante(dato="producto"))


def _fallar(*_: object) -> None:
    raise ErrorMemoria("confirmaciones: OperationalError")


# R1 a R3 · la escritura


def test_el_si_escribe_la_fila_y_dice_listo(memoria: Memoria, config: ConfigNegocio) -> None:
    """R1, R9, R22: el "sí" sobre el pedido completo escribe su fila, dice "¡Listo!" y cierra la charla."""
    _pendiente(memoria, config)
    planilla = _Planilla()

    texto = _turno(memoria, config, _confirmar(True), "sí", planilla)

    assert texto == MENSAJE_PEDIDO_CONFIRMADO
    assert planilla.filas == [FILA]
    charla = _charla(memoria)
    assert charla.pedido is None and charla.mensajes == []
    assert charla.toma is not None and charla.toma.escrita


def test_la_fila_lleva_los_archivos_del_pedido(memoria: Memoria, config: ConfigNegocio) -> None:
    """R29, R9: la celda archivos lleva una línea por archivo, con su hora del negocio y su referencia."""
    adjunto = _adjunto("file", "pdf")
    _lote(CONV, [_archivo(adjunto)], config, memoria, _Agente(), _Planilla())
    _turno(memoria, config, _registrar(**COMPLETO))
    planilla = _Planilla()

    _turno(memoria, config, _confirmar(True), "sí", planilla)

    assert planilla.filas[0]["archivos"] == f"1. 06/10 10:00 · pdf · adjunto #{adjunto.id}"


def test_si_falla_la_planilla_el_pedido_vuelve_y_el_si_siguiente_reintenta(
    memoria: Memoria, config: ConfigNegocio
) -> None:
    """R2: sin fila no hay "¡Listo!": sale el error de guardado, el pedido vuelve a pendiente con su marcador
    y un "sí" nuevo lo escribe."""
    pedido = _pendiente(memoria, config)

    texto = _turno(memoria, config, _confirmar(True), "sí", _Planilla(ErrorPlanilla("no se pudo")))

    assert texto == MENSAJE_ERROR_AL_GUARDAR
    charla = _charla(memoria)
    assert charla.pedido == pedido and charla.toma is None
    assert _historial(memoria)[-2:] == ["sí", "[confirmacion_fallida]"]
    planilla = _Planilla()
    assert _turno(memoria, config, _confirmar(True), "dale", planilla) == MENSAJE_PEDIDO_CONFIRMADO
    assert planilla.filas == [FILA]


def test_cualquier_otra_falla_al_escribir_tambien_devuelve_el_pedido(
    memoria: Memoria, config: ConfigNegocio
) -> None:
    """R2: una falla que no es de la planilla tampoco deja la toma abierta ni dice "¡Listo!"."""
    pedido = _pendiente(memoria, config)

    texto = _turno(memoria, config, _confirmar(True), "sí", _Planilla(RuntimeError("roto")))

    assert texto == MENSAJE_ERROR_INTERNO
    charla = _charla(memoria)
    assert charla.pedido == pedido and charla.toma is None


def test_una_toma_que_quedo_en_escritura_no_cuenta_como_confirmada(
    memoria: Memoria, config: ConfigNegocio, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2, R7, R32: si falla la planilla y también devolver el pedido, la toma queda en escritura sin fila:
    el prompt no la recibe como confirmada y un archivo no recibe "Tu pedido ya estaba confirmado"."""
    _pendiente(memoria, config)

    def fallar(*_: object) -> None:
        raise ErrorMemoria("confirmaciones: OperationalError")

    monkeypatch.setattr(memoria, "devolver_a_pendiente", fallar)
    planilla = _Planilla(ErrorPlanilla("no se pudo"))
    assert _turno(memoria, config, _confirmar(True), "sí", planilla) == MENSAJE_ERROR_INTERNO
    agente = _Agente(HORARIOS)

    _lote(CONV, [_mensaje("¿a qué hora abren?")], config, memoria, agente, _Planilla())
    texto = _lote(CONV, [_archivo()], config, memoria, _Agente(), _Planilla())

    assert agente.llamadas[0]["confirmado"] is None
    assert texto == acuse_de_archivos(1, _pedido(memoria), config)


def test_lo_que_falla_despues_de_escribir_no_desdice_la_fila(
    memoria: Memoria, config: ConfigNegocio, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """R3: si cerrar la toma falla después de escribir, el cliente igual recibe "¡Listo!", y la fila es una."""
    _pendiente(memoria, config)

    def fallar(*_: object) -> None:
        raise ErrorMemoria("confirmaciones: OperationalError")

    monkeypatch.setattr(memoria, "confirmar_escrito", fallar)
    planilla = _Planilla()

    assert _turno(memoria, config, _confirmar(True), "sí", planilla) == MENSAJE_PEDIDO_CONFIRMADO
    assert len(planilla.filas) == 1
    assert "error=ErrorMemoria" in caplog.text


def test_la_confirmacion_gana_sobre_el_error_de_otro_mensaje_del_lote(
    memoria: Memoria, config: ConfigNegocio
) -> None:
    """R3: en un lote con un audio y el "sí", el cliente recibe "¡Listo!", no el aviso del tipo no soportado."""
    _pendiente(memoria, config)
    planilla = _Planilla()
    lote = [_archivo(_adjunto("audio", "ogg")), _mensaje("sí")]

    texto = _lote(CONV, lote, config, memoria, _Agente(_confirmar(True)), planilla)

    assert texto == MENSAJE_PEDIDO_CONFIRMADO
    assert len(planilla.filas) == 1


# R4 a R7 · la carrera del "sí"


def test_si_y_dale_a_la_vez_escriben_una_sola_fila(memoria: Memoria, config: ConfigNegocio) -> None:
    """R4: "sí" y "dale" a la vez: el "dale" espera la escritura, no encuentra pendiente y no contesta (R6)."""
    _pendiente(memoria, config)
    planilla = _Planilla(esperar=True)
    resultados: dict[str, str | None] = {}
    si = _lanzar(resultados, "sí", [_mensaje("sí")], config, memoria, _Agente(_confirmar(True)), planilla)
    assert planilla.entro.wait(timeout=5)
    dale = _lanzar(resultados, "dale", [_mensaje("dale")], config, memoria, _Agente(_confirmar(True)), planilla)
    dale.join(timeout=0.3)
    esperaba = dale.is_alive()
    planilla.soltar.set()
    for hilo in (si, dale):
        hilo.join(timeout=5)

    assert esperaba
    assert resultados == {"sí": MENSAJE_PEDIDO_CONFIRMADO, "dale": None}
    assert planilla.filas == [FILA]


def test_un_segundo_si_despues_de_confirmar_no_contesta(
    memoria: Memoria, config: ConfigNegocio, caplog: pytest.LogCaptureFixture
) -> None:
    """R6: un "sí" sin pendiente y con el pedido recién confirmado: sin texto, turno del bot ni otra fila."""
    planilla = _Planilla()
    _confirmado(memoria, config, planilla)
    caplog.set_level(logging.INFO, logger="app.turno")

    assert _turno(memoria, config, _confirmar(True), "dale", planilla) is None
    assert len(planilla.filas) == 1
    assert _historial(memoria) == ["dale"]
    assert "camino=pedido_confirmado_sin_cambios" in caplog.text


@pytest.mark.parametrize(
    "decision",
    [
        _registrar(),
        _registrar(**COMPLETO),
        _registrar(cantidad=3, fecha_necesita="2026-10-09"),
        _registrar(**COMPLETO | {"nombre_cliente": "Otro Nombre"}),
        Decision("pedir_dato_faltante", PedirDatoFaltante(dato="producto")),
    ],
)
def test_lo_que_no_cambia_el_confirmado_no_contesta(
    memoria: Memoria, config: ConfigNegocio, decision: Decision
) -> None:
    """R6: sin campos, los mismos datos (por valor; el nombre no es un cambio) o una repregunta: ni texto ni
    turno del bot, y no arranca otro pedido."""
    _confirmado(memoria, config)

    assert _turno(memoria, config, decision, "gracias!") is None
    assert _charla(memoria).pedido is None
    assert _historial(memoria) == ["gracias!"]


def test_el_confirmado_reenviado_con_un_campo_descartado_no_abre_otro_pedido(
    memoria: Memoria, config: ConfigNegocio
) -> None:
    """R6, R4: pasada la medianoche y dentro de los 5 minutos, el modelo reenvía el confirmado y su fecha, que
    ya pasó, se descarta. Por valor no cambió nada: ni texto ni turno del bot, y no arranca otro pedido."""
    config.fijar_ahora(HORA_DE_PRUEBA.replace(hour=23, minute=58))
    mismo = _registrar(**COMPLETO | {"fecha_necesita": "2026-10-06"})
    _turno(memoria, config, mismo)
    assert _turno(memoria, config, _confirmar(True), "sí") == MENSAJE_PEDIDO_CONFIRMADO
    config.fijar_ahora(HORA_DE_PRUEBA.replace(day=7, hour=0, minute=1))

    assert _turno(memoria, config, mismo, "gracias!") is None
    charla = memoria.leer_charla(CONV, config.ahora())
    assert charla.pedido is None
    assert [mensaje.content for mensaje in charla.mensajes] == ["gracias!"]


def test_la_carrera_dura_5_minutos_despues_de_la_fila(memoria: Memoria, config: ConfigNegocio) -> None:
    """R6, R7: una repregunta a los 4:59 de escrita la fila no contesta; a los 5:00 es un mensaje normal y se
    contesta, con el confirmado todavía en el prompt."""
    confirmado = _confirmado(memoria, config)  # la fila se escribe en HORA_DE_PRUEBA
    agente = _Agente(REPREGUNTA, REPREGUNTA)

    config.fijar_ahora(HORA_DE_PRUEBA + CARRERA - timedelta(seconds=1))
    antes = _lote(CONV, [_mensaje("quiero otro pedido")], config, memoria, agente, _Planilla())
    config.fijar_ahora(HORA_DE_PRUEBA + CARRERA)
    despues = _lote(CONV, [_mensaje("quiero otro pedido")], config, memoria, agente, _Planilla())

    assert antes is None
    assert despues == pregunta_por_dato("producto", config)
    assert [llamada["confirmado"] for llamada in agente.llamadas] == [confirmado, confirmado]


def test_una_toma_trabada_no_traba_el_pedido_siguiente(
    memoria: Memoria, config: ConfigNegocio, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R25, R3: si cerrar la toma falla con la fila escrita, la toma queda en escritura; el pedido siguiente la
    reemplaza y su "sí" escribe su propia fila, sin esperar las 6 horas."""
    planilla = _Planilla()
    _pendiente(memoria, config)
    with monkeypatch.context() as parche:
        parche.setattr(memoria, "confirmar_escrito", _fallar)
        assert _turno(memoria, config, _confirmar(True), "sí", planilla) == MENSAJE_PEDIDO_CONFIRMADO
    toma = _charla(memoria).toma
    assert toma is not None and not toma.escrita

    _confirmado(memoria, config, planilla)

    assert planilla.filas == [FILA, FILA]
    toma = _charla(memoria).toma
    assert toma is not None and toma.escrita and toma.generacion == 2


def test_si_y_dale_seguidos_con_la_toma_trabada_escriben_una_sola_fila(
    memoria: Memoria, config: ConfigNegocio, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4: el "sí" escribe la fila pero no cierra la toma; el "dale" que entra después no la reemplaza: no
    encuentra pendiente y no deja otra fila."""
    _pendiente(memoria, config)
    monkeypatch.setattr(memoria, "confirmar_escrito", _fallar)
    planilla = _Planilla()

    assert _turno(memoria, config, _confirmar(True), "sí", planilla) == MENSAJE_PEDIDO_CONFIRMADO
    _turno(memoria, config, _confirmar(True), "dale", planilla)

    assert planilla.filas == [FILA]
    assert _charla(memoria).pedido is None


def test_un_rechazo_sobre_el_confirmado_es_un_cambio(memoria: Memoria, config: ConfigNegocio) -> None:
    """R6, R47: un rechazo después de confirmar no toca el pedido: deriva con la frase de horario (R38) y la
    nota con el confirmado como lo entendió el bot."""
    confirmado = _confirmado(memoria, config)

    resultado = procesar_lote(CONV, [_mensaje("no, esperá")], config, memoria, _Agente(_confirmar(False)))

    assert resultado == Resultado(
        con_frase_de_horario(MENSAJE_CAMBIO_DURANTE_LA_CONFIRMACION, config, HORA_DE_PRUEBA),
        nota_de_derivacion(CAMBIO, confirmado, NOMBRE, config), derivar=True,
    )
    charla = _charla(memoria)
    assert charla.pedido is None
    assert charla.toma is not None and charla.toma.pedido == confirmado
    assert _historial(memoria)[-1] == f"[{CAMBIO}]"


def test_el_acuse_del_archivo_no_tapa_un_cambio_sobre_el_confirmado(
    memoria: Memoria, config: ConfigNegocio
) -> None:
    """R6, R30, R32: una foto con "no, esperá" sobre el confirmado: el cliente recibe el aviso del cambio, no
    el acuse del archivo; la nota suma el archivo a la de la derivación (R47)."""
    confirmado = _confirmado(memoria, config)
    adjunto = _adjunto("image", "jpg")

    lote = [_mensaje("no, esperá", adjuntos=[adjunto])]
    resultado = procesar_lote(CONV, lote, config, memoria, _Agente(_confirmar(False)), _Planilla())

    derivacion = nota_de_derivacion(CAMBIO, confirmado, NOMBRE, config)
    archivo = nota_de_archivos(f"1. 06/10 10:00 · jpg · adjunto #{adjunto.id}")
    aviso = con_frase_de_horario(MENSAJE_CAMBIO_DURANTE_LA_CONFIRMACION, config, HORA_DE_PRUEBA)
    assert resultado == Resultado(aviso, f"{derivacion}\n\n{archivo}", derivar=True)
    assert _historial(memoria) == ["no, esperá", f"[{CAMBIO}]"]


def test_el_pedido_recien_confirmado_va_al_prompt(memoria: Memoria, config: ConfigNegocio) -> None:
    """R7: después de confirmar, decidir recibe el pedido confirmado y ningún pedido en curso."""
    confirmado = _confirmado(memoria, config)
    agente = _Agente(HORARIOS)

    _lote(CONV, [_mensaje("¿a qué hora abren?")], config, memoria, agente, _Planilla())

    assert agente.llamadas[0]["confirmado"] == confirmado
    assert agente.llamadas[0]["pedido"] is None


def test_otro_trabajo_despues_de_confirmar_es_un_pedido_nuevo(memoria: Memoria, config: ConfigNegocio) -> None:
    """R7: con el confirmado en el prompt, registrar_pedido con otros datos arranca un pedido nuevo, y desde
    ahí el confirmado ya no va al prompt."""
    _confirmado(memoria, config)
    agente = _Agente(_registrar(producto="acabados", cantidad=200), HORARIOS)

    texto = _lote(CONV, [_mensaje("también 200 anillados")], config, memoria, agente, _Planilla())
    _lote(CONV, [_mensaje("¿a qué hora abren?")], config, memoria, agente, _Planilla())

    assert texto == pregunta_por_dato("material", config)
    assert _pedido(memoria) == Pedido(telefono=TELEFONO, producto="acabados", cantidad=200)
    assert agente.llamadas[1]["confirmado"] is None


def test_una_consulta_despues_de_confirmar_se_contesta(memoria: Memoria, config: ConfigNegocio) -> None:
    """R6: una pregunta frecuente no es parte de la carrera: se contesta como siempre."""
    _confirmado(memoria, config)

    assert _turno(memoria, config, HORARIOS, "¿a qué hora abren?") == respuesta_faq("horarios", config)
