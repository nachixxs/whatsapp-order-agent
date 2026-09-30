import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.agente import Decision, PedirDatoFaltante
from app.config import ConfigNegocio
from app.memoria import ErrorMemoria, Memoria
from app.pedidos import Pedido
from app.respuestas import (
    MENSAJE_CAMBIO_DURANTE_LA_CONFIRMACION,
    MENSAJE_ERROR_AL_GUARDAR,
    MENSAJE_ERROR_INTERNO,
    MENSAJE_PEDIDO_CONFIRMADO,
    pregunta_por_dato,
    respuesta_faq,
)
from app.sheets import ErrorPlanilla
from app.turno import procesar_lote
from tests.conftest import TELEFONO
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
    _mensaje,
    _pedido,
    _pendiente,
    _Planilla,
    _registrar,
    _turno,
)

# R9: las columnas de §5 menos anticipo; fecha_ingreso con el reloj del negocio (HORA_DE_PRUEBA)
FILA = {
    "fecha_ingreso": "2026-10-06 10:00:00", "nombre_cliente": NOMBRE, "telefono": TELEFONO,
    "producto": "sellos", "material": "goma", "medidas": "4x2 cm", "cantidad": "3", "tiene_diseno": "si",
    "archivos": "", "fecha_necesita": "2026-10-09",
}


@pytest.fixture
def memoria(tmp_path: Path) -> Iterator[Memoria]:
    abierta = Memoria(tmp_path / "memoria.db")
    yield abierta
    abierta.cerrar()


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
    procesar_lote(CONV, [_archivo(adjunto)], config, memoria, _Agente(), _Planilla())
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

    texto = procesar_lote(CONV, lote, config, memoria, _Agente(_confirmar(True)), planilla)

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


def test_un_rechazo_sobre_el_confirmado_es_un_cambio(memoria: Memoria, config: ConfigNegocio) -> None:
    """R6: un rechazo después de confirmar no toca el pedido: contesta que el cambio lo ve un asesor."""
    confirmado = _confirmado(memoria, config)

    texto = _turno(memoria, config, _confirmar(False), "no, esperá")

    assert texto == MENSAJE_CAMBIO_DURANTE_LA_CONFIRMACION
    charla = _charla(memoria)
    assert charla.pedido is None
    assert charla.toma is not None and charla.toma.pedido == confirmado
    assert _historial(memoria)[-1] == "[cambio_sobre_pedido_confirmado]"


def test_el_pedido_recien_confirmado_va_al_prompt(memoria: Memoria, config: ConfigNegocio) -> None:
    """R7: después de confirmar, decidir recibe el pedido confirmado y ningún pedido en curso."""
    confirmado = _confirmado(memoria, config)
    agente = _Agente(HORARIOS)

    procesar_lote(CONV, [_mensaje("¿a qué hora abren?")], config, memoria, agente, _Planilla())

    assert agente.llamadas[0]["confirmado"] == confirmado
    assert agente.llamadas[0]["pedido"] is None


def test_otro_trabajo_despues_de_confirmar_es_un_pedido_nuevo(memoria: Memoria, config: ConfigNegocio) -> None:
    """R7: con el confirmado en el prompt, registrar_pedido con otros datos arranca un pedido nuevo, y desde
    ahí el confirmado ya no va al prompt."""
    _confirmado(memoria, config)
    agente = _Agente(_registrar(producto="acabados", cantidad=200), HORARIOS)

    texto = procesar_lote(CONV, [_mensaje("también 200 anillados")], config, memoria, agente, _Planilla())
    procesar_lote(CONV, [_mensaje("¿a qué hora abren?")], config, memoria, agente, _Planilla())

    assert texto == pregunta_por_dato("material", config)
    assert _pedido(memoria) == Pedido(telefono=TELEFONO, producto="acabados", cantidad=200)
    assert agente.llamadas[1]["confirmado"] is None


def test_una_consulta_despues_de_confirmar_se_contesta(memoria: Memoria, config: ConfigNegocio) -> None:
    """R6: una pregunta frecuente no es parte de la carrera: se contesta como siempre."""
    _confirmado(memoria, config)

    assert _turno(memoria, config, HORARIOS, "¿a qué hora abren?") == respuesta_faq("horarios", config)
