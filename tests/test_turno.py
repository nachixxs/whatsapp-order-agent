import itertools
import logging
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

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
from app.memoria import RETENCION_PROCESADOS, Charla, ErrorMemoria, Memoria
from app.pedidos import Pedido
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
from app.tools import MARCADOR_REPREGUNTA_MATERIAL, MATERIAL_A_DEFINIR
from app.turno import procesar_lote
from tests.conftest import HORA_DE_PRUEBA, TELEFONO

CONV = 73915  # un número que no puede aparecer por azar en el alias ni en el teléfono
NOMBRE = "Cliente Prueba"
_IDS = itertools.count(1)
# Los siete datos; HORA_DE_PRUEBA es el martes 6 de octubre de 2026
COMPLETO: dict[str, Any] = {
    "producto": "sellos", "material": "goma", "medidas": "4x2 cm", "cantidad": 3,
    "fecha_necesita": "2026-10-09", "tiene_diseno": "si", "nombre_cliente": NOMBRE,
}
HORARIOS = Decision("consulta_general", ConsultaGeneral(tema="horarios"))


class _Agente:
    """El `decidir` falso: devuelve las decisiones en orden y guarda con qué lo llamaron."""

    def __init__(self, *decisiones: Decision | SinTool | ErrorApi) -> None:
        self.decisiones = list(decisiones)
        self.llamadas: list[dict[str, Any]] = []

    def __call__(self, config: ConfigNegocio, ahora: Any, charla: Charla, **resto: Any) -> Any:
        self.llamadas.append({"ahora": ahora, "charla": charla, **resto})
        return self.decisiones.pop(0)


@pytest.fixture
def memoria(tmp_path: Path) -> Iterator[Memoria]:
    abierta = Memoria(tmp_path / "memoria.db")
    yield abierta
    abierta.cerrar()


def _mensaje(
    texto: str = "hola", *, nombre: str | None = NOMBRE, telefono: str | None = TELEFONO
) -> MensajeEntrante:
    return MensajeEntrante(
        evento="message_created", id_mensaje=next(_IDS), contenido=texto, id_conversacion=CONV,
        contacto=Contacto(nombre=nombre, telefono=telefono),
    )


def _registrar(**campos: Any) -> Decision:
    return Decision("registrar_pedido", RegistrarPedido(**campos))


def _confirmar(acepta: bool) -> Decision:
    return Decision("confirmar_pedido", ConfirmarPedido(acepta=acepta))


def _turno(
    memoria: Memoria, config: ConfigNegocio, decision: Decision | SinTool | ErrorApi, texto: str = "hola"
) -> str | None:
    return procesar_lote(CONV, [_mensaje(texto)], config, memoria, _Agente(decision))


def _charla(memoria: Memoria) -> Charla:
    return memoria.leer_charla(CONV, HORA_DE_PRUEBA)


def _historial(memoria: Memoria) -> list[str]:
    return [mensaje.content for mensaje in _charla(memoria).mensajes]


def _pedido(memoria: Memoria) -> Pedido:
    pedido = _charla(memoria).pedido
    assert pedido is not None
    return pedido


# R21 · marcadores


def test_repreguntar_el_material_deja_el_marcador_exacto(memoria: Memoria, config: ConfigNegocio) -> None:
    """R21: después de repreguntar el material, el historial termina en el marcador que citan prompt y tools."""
    texto = _turno(memoria, config, Decision("pedir_dato_faltante", PedirDatoFaltante(dato="material")))

    assert texto == pregunta_por_dato("material", config)
    assert _historial(memoria)[-1] == MARCADOR_REPREGUNTA_MATERIAL


def test_registrar_sin_material_deja_el_mismo_marcador(memoria: Memoria, config: ConfigNegocio) -> None:
    """R21: registrar_pedido que deja el material como primer faltante repregunta con el mismo marcador."""
    texto = _turno(memoria, config, _registrar(producto="sellos"))

    assert texto == pregunta_por_dato("material", config)
    assert _historial(memoria)[-1] == MARCADOR_REPREGUNTA_MATERIAL


def test_el_historial_guarda_el_marcador_y_no_la_prosa(memoria: Memoria, config: ConfigNegocio) -> None:
    """R21: del lado del bot queda `[consulta_general: horarios]`, nunca el texto que leyó el cliente."""
    texto = _turno(memoria, config, HORARIOS, "¿a qué hora abren?")

    assert texto == respuesta_faq("horarios", config)
    assert _historial(memoria) == ["¿a qué hora abren?", "[consulta_general: horarios]"]


# SPECS §6 · un camino por tool


def test_derivar_contesta_el_texto_del_motivo(memoria: Memoria, config: ConfigNegocio) -> None:
    """R15: sin_stock deriva con el texto de su motivo, sin alternativas."""
    texto = _turno(memoria, config, Decision("derivar_a_asesor", DerivarAAsesor(motivo="sin_stock")))

    assert texto == texto_derivacion("sin_stock", config)
    assert _historial(memoria)[-1] == "[derivado_a_asesor: sin_stock]"


def test_un_pedido_a_medias_pregunta_el_primer_faltante(memoria: Memoria, config: ConfigNegocio) -> None:
    """R14: se suman los campos y se pregunta el primero que falta."""
    texto = _turno(memoria, config, _registrar(producto="sellos", material="goma", cantidad=3))

    assert texto == pregunta_por_dato("medidas", config)
    assert _pedido(memoria) == Pedido(telefono=TELEFONO, producto="sellos", material="goma", cantidad=3)
    assert _historial(memoria)[-1] == "[dato_faltante: medidas]"


def test_el_pedido_completo_muestra_el_resumen_y_queda_pendiente(
    memoria: Memoria, config: ConfigNegocio
) -> None:
    """R1: con los siete datos se muestra el resumen y el pedido queda pendiente en SQLite."""
    texto = _turno(memoria, config, _registrar(**COMPLETO))

    pedido = _pedido(memoria)
    assert pedido.completo
    assert texto == resumen_pedido(pedido, config)
    assert _historial(memoria)[-1] == "[pedido_pendiente_confirmacion]"


def test_registrar_sin_campos_frente_al_resumen_lo_vuelve_a_mostrar(
    memoria: Memoria, config: ConfigNegocio
) -> None:
    """R16: el "gracias" pelado frente al resumen es registrar_pedido sin campos, y vuelve a mostrarlo."""
    resumen = _turno(memoria, config, _registrar(**COMPLETO))

    assert _turno(memoria, config, _registrar(), "gracias!") == resumen


def test_rechazar_el_resumen_conserva_los_campos(memoria: Memoria, config: ConfigNegocio) -> None:
    """R8: acepta=false contesta el rechazo y vuelve a recolectar sin perder lo que ya dio."""
    _turno(memoria, config, _registrar(**COMPLETO))
    antes = _pedido(memoria)

    texto = _turno(memoria, config, _confirmar(False), "no, así no")

    assert texto == MENSAJE_PEDIDO_RECHAZADO
    assert _pedido(memoria) == antes
    assert _historial(memoria)[-1] == "[pedido_rechazado]"


def test_aceptar_en_el_cp2_no_dice_listo_ni_deja_marcador(
    memoria: Memoria, config: ConfigNegocio, caplog: pytest.LogCaptureFixture
) -> None:
    """R2: sin planilla no hay fila: no sale ningún texto, no queda turno del bot y el pedido sigue pendiente."""
    _turno(memoria, config, _registrar(**COMPLETO))
    antes = _pedido(memoria)
    caplog.set_level(logging.INFO, logger="app.turno")

    texto = _turno(memoria, config, _confirmar(True), "dale")

    assert texto is None
    assert _pedido(memoria) == antes
    assert _historial(memoria)[-2:] == ["[pedido_pendiente_confirmacion]", "dale"]
    assert "camino=confirmacion_sin_planilla" in caplog.text


def test_confirmar_sin_pedido_completo_no_confirma(memoria: Memoria, config: ConfigNegocio) -> None:
    """R1: confirmar un pedido a medias da sin_pedido_para_confirmar y la repregunta fija."""
    _turno(memoria, config, _registrar(producto="sellos"))

    texto = _turno(memoria, config, _confirmar(True), "sí")

    assert texto == MENSAJE_NO_ENTENDIDO
    assert _historial(memoria)[-1] == "[sin_pedido_para_confirmar]"


@pytest.mark.parametrize(
    ("motivo", "marcador"),
    [
        ("sin_tool_use", "[sin_tool]"),
        ("refusal", "[sin_tool]"),
        ("argumentos_invalidos", "[argumentos_invalidos]"),
        ("tool_desconocida", "[tool_desconocida]"),
    ],
)
def test_sin_tool_contesta_la_repregunta_fija(
    memoria: Memoria, config: ConfigNegocio, motivo: Any, marcador: str
) -> None:
    """R11, R19: sin una tool que aplicar sale el texto fijo; al historial va el marcador, nunca un error."""
    assert _turno(memoria, config, SinTool(motivo)) == MENSAJE_NO_ENTENDIDO
    assert _historial(memoria)[-1] == marcador


# R14 y R15 · descartados y material a definir


def test_un_producto_fuera_del_catalogo_avisa_y_guarda_el_resto(
    memoria: Memoria, config: ConfigNegocio
) -> None:
    """R14: se descarta el producto, no la llamada; el aviso va en lugar de la repregunta."""
    texto = _turno(memoria, config, _registrar(producto="remeras", cantidad=3))

    assert _pedido(memoria) == Pedido(telefono=TELEFONO, cantidad=3)
    assert texto == aviso_de_descartados(["producto"], _pedido(memoria), config)
    assert _historial(memoria)[-1] == "[producto_invalido]"


def test_una_fecha_pasada_se_descarta_y_se_repregunta(memoria: Memoria, config: ConfigNegocio) -> None:
    """R14: la fecha pasada se descarta y se repregunta; el resto del pedido queda."""
    texto = _turno(memoria, config, _registrar(producto="sellos", fecha_necesita="2026-10-05"))

    pedido = _pedido(memoria)
    assert pedido.producto == "sellos" and pedido.fecha_necesita is None
    assert texto == aviso_de_descartados(["fecha_necesita"], pedido, config)
    assert _historial(memoria)[-1] == "[fecha_invalida]"


def test_un_nombre_descartado_se_pregunta_sin_aviso(memoria: Memoria, config: ConfigNegocio) -> None:
    """R14: un nombre que no parece un nombre se descarta; el pedido queda sin él y se pregunta."""
    texto = _turno(memoria, config, _registrar(**COMPLETO | {"nombre_cliente": "🙂"}))

    assert texto == pregunta_por_dato("nombre_cliente", config)
    assert _pedido(memoria).nombre_cliente is None


def test_el_material_a_definir_avisa_delante_de_la_repregunta(
    memoria: Memoria, config: ConfigNegocio
) -> None:
    """R15: el turno que deja el material a definir antepone el aviso a la repregunta."""
    texto = _turno(memoria, config, _registrar(producto="sellos", material="A definir con el asesor."))

    assert _pedido(memoria).material == MATERIAL_A_DEFINIR
    assert texto == con_aviso_de_material(pregunta_por_dato("medidas", config), es_resumen=False)


def test_el_material_a_definir_avisa_delante_del_resumen(memoria: Memoria, config: ConfigNegocio) -> None:
    """R15: con el pedido completo, el aviso va delante del resumen."""
    texto = _turno(memoria, config, _registrar(**COMPLETO | {"material": MATERIAL_A_DEFINIR}))

    assert texto == con_aviso_de_material(resumen_pedido(_pedido(memoria), config), es_resumen=True)


def test_el_aviso_del_material_sale_una_sola_vez(memoria: Memoria, config: ConfigNegocio) -> None:
    """R15: el modelo reenvía el material a definir en cada turno; el aviso sale solo en el que lo dejó."""
    _turno(memoria, config, _registrar(producto="sellos", material=MATERIAL_A_DEFINIR))

    texto = _turno(memoria, config, _registrar(material=MATERIAL_A_DEFINIR, medidas="4x2 cm"))

    assert texto == pregunta_por_dato("cantidad", config)


def test_un_descartado_con_el_material_a_definir_lleva_los_dos_avisos(
    memoria: Memoria, config: ConfigNegocio
) -> None:
    """R14, R15: si el mismo turno descarta un campo y deja el material a definir, salen los dos avisos."""
    texto = _turno(memoria, config, _registrar(producto="remeras", material=MATERIAL_A_DEFINIR))

    aviso = aviso_de_descartados(["producto"], _pedido(memoria), config)
    assert aviso is not None
    assert texto == con_aviso_de_material(aviso, es_resumen=False)


# R13 y R43 · el contacto de Chatwoot


def test_el_telefono_del_pedido_sale_del_contacto(memoria: Memoria, config: ConfigNegocio) -> None:
    """R13: el teléfono del pedido es el del contacto de Chatwoot."""
    otro = "+54 9 11 5555-0001"
    procesar_lote(CONV, [_mensaje(telefono=otro)], config, memoria, _Agente(_registrar(producto="sellos")))

    assert _pedido(memoria).telefono == otro


def test_un_contacto_sin_telefono_no_arma_un_pedido(memoria: Memoria, config: ConfigNegocio) -> None:
    """R13: sin teléfono no se inventa uno: el turno falla con el mensaje de error y se desmarca (R23)."""
    mensaje = _mensaje(telefono=None)

    texto = procesar_lote(CONV, [mensaje], config, memoria, _Agente(_registrar(producto="sellos")))

    assert texto == MENSAJE_ERROR_INTERNO
    assert _charla(memoria).pedido is None
    assert mensaje.id_mensaje is not None
    assert memoria.marcar_procesado(mensaje.id_mensaje, HORA_DE_PRUEBA) is True


@pytest.mark.parametrize(
    ("nombre", "esperado"),
    [(NOMBRE, NOMBRE), (None, None), ("  ", None), (TELEFONO, None), ("+5491155550000", None)],
)
def test_el_perfil_que_es_el_telefono_no_va_a_la_api(
    memoria: Memoria, config: ConfigNegocio, nombre: str | None, esperado: str | None
) -> None:
    """R43, R13: sin perfil, Chatwoot pone el teléfono de nombre; ese, o uno vacío, va como None."""
    agente = _Agente(HORARIOS)

    procesar_lote(CONV, [_mensaje(nombre=nombre)], config, memoria, agente)

    assert agente.llamadas[0]["nombre_perfil"] == esperado


# R23 y R24 · dedup y compuerta


def test_el_mismo_id_dos_veces_se_procesa_una_sola(memoria: Memoria, config: ConfigNegocio) -> None:
    """R23: el reintento de un mensaje ya procesado no llama a la API ni contesta."""
    mensaje, agente = _mensaje(), _Agente(HORARIOS, HORARIOS)

    assert procesar_lote(CONV, [mensaje], config, memoria, agente) == respuesta_faq("horarios", config)
    assert procesar_lote(CONV, [mensaje], config, memoria, agente) is None

    assert len(agente.llamadas) == 1
    assert _historial(memoria) == ["hola", "[consulta_general: horarios]"]


def test_un_lote_es_un_solo_turno(memoria: Memoria, config: ConfigNegocio) -> None:
    """R30: los mensajes nuevos del lote van juntos en una sola llamada; el repetido entra una vez (R23)."""
    primero, segundo = _mensaje("quiero sellos"), _mensaje("3 de goma")
    agente = _Agente(_registrar(producto="sellos", material="goma", cantidad=3))

    procesar_lote(CONV, [primero, segundo, primero], config, memoria, agente)

    assert len(agente.llamadas) == 1
    assert [m.content for m in agente.llamadas[0]["charla"].mensajes] == ["quiero sellos", "3 de goma"]


def test_un_mensaje_sin_id_no_se_procesa(memoria: Memoria, config: ConfigNegocio) -> None:
    """R24: sin id no hay compuerta de dedup, y sin compuerta no se procesa."""
    agente = _Agente()
    mensaje = _mensaje().model_copy(update={"id_mensaje": None})

    assert procesar_lote(CONV, [mensaje], config, memoria, agente) is None
    assert agente.llamadas == []


def test_si_falla_el_dedup_contesta_el_error_sin_llamar_a_la_api(
    memoria: Memoria, config: ConfigNegocio, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R24: si la consulta de dedup falla, no se procesa y el cliente recibe el mensaje de error."""
    def fallar(*_: object) -> bool:
        raise ErrorMemoria("procesados: OperationalError")

    monkeypatch.setattr(memoria, "marcar_procesado", fallar)
    agente = _Agente()

    assert procesar_lote(CONV, [_mensaje()], config, memoria, agente) == MENSAJE_ERROR_INTERNO
    assert agente.llamadas == []


@pytest.mark.parametrize(
    "error", [ErrorApi("timeout"), ErrorApi("conexion"), ErrorApi("http", 429), ErrorApi("http", 529)]
)
def test_un_error_transitorio_de_la_api_desmarca_el_mensaje(
    memoria: Memoria, config: ConfigNegocio, error: ErrorApi
) -> None:
    """R12, R23: error interno con su marcador, no uno de éxito; el reintento del mismo id se procesa."""
    mensaje, agente = _mensaje(), _Agente(error, HORARIOS)

    assert procesar_lote(CONV, [mensaje], config, memoria, agente) == MENSAJE_ERROR_INTERNO
    assert _historial(memoria) == ["hola", "[error_interno]"]
    assert procesar_lote(CONV, [mensaje], config, memoria, agente) == respuesta_faq("horarios", config)


@pytest.mark.parametrize("error", [ErrorApi("credencial"), ErrorApi("http", 400), ErrorApi("api")])
def test_un_error_que_se_repetiria_deja_el_mensaje_marcado(
    memoria: Memoria, config: ConfigNegocio, error: ErrorApi
) -> None:
    """R23: credencial, un 4xx o un error desconocido fallarían igual; el reintento no gasta otra llamada."""
    mensaje, agente = _mensaje(), _Agente(error, HORARIOS)

    assert procesar_lote(CONV, [mensaje], config, memoria, agente) == MENSAJE_ERROR_INTERNO
    assert procesar_lote(CONV, [mensaje], config, memoria, agente) is None
    assert len(agente.llamadas) == 1


# R5, R27, R37 y R52


def test_si_la_generacion_cambio_no_pisa_ni_contesta(
    memoria: Memoria, config: ConfigNegocio, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5: un pedido leído antes de una toma no se guarda; como R6 sin cambios, ni texto ni turno del bot."""
    monkeypatch.setattr(memoria, "guardar_pedido", lambda *_: False)

    assert _turno(memoria, config, _registrar(producto="sellos"), "sellos") is None
    assert _historial(memoria) == ["sellos"]


def test_el_lote_barre_lo_vencido(memoria: Memoria, config: ConfigNegocio) -> None:
    """R23, R27: cada lote barre; un id más viejo que la retención se olvida."""
    memoria.marcar_procesado(999_999, HORA_DE_PRUEBA - RETENCION_PROCESADOS)

    _turno(memoria, config, HORARIOS)

    assert memoria.marcar_procesado(999_999, HORA_DE_PRUEBA) is True


def test_el_lote_usa_el_reloj_del_negocio(memoria: Memoria, config: ConfigNegocio) -> None:
    """R37: el ahora del lote es config.ahora(), y es el que recibe el agente."""
    agente = _Agente(HORARIOS)

    procesar_lote(CONV, [_mensaje()], config, memoria, agente)

    assert agente.llamadas[0]["ahora"] == HORA_DE_PRUEBA


def test_el_log_del_turno_no_lleva_nada_del_cliente(
    memoria: Memoria, config: ConfigNegocio, caplog: pytest.LogCaptureFixture
) -> None:
    """R52: alias, tool y camino; ni el texto, ni el nombre, ni el teléfono, ni los valores del pedido."""
    caplog.set_level(logging.INFO)
    centinela = "Centinela-7Q"
    mensaje = _mensaje(centinela, nombre=f"{centinela} Nombre")

    procesar_lote(CONV, [mensaje], config, memoria, _Agente(_registrar(producto="sellos", medidas=centinela)))

    assert "alias=" in caplog.text
    assert "tool=registrar_pedido camino=dato_faltante: material" in caplog.text
    for dato in (centinela, TELEFONO, str(CONV)):
        assert dato not in caplog.text


# SPECS §3 · una charla entera


def test_una_charla_de_varios_turnos_llega_al_resumen(memoria: Memoria, config: ConfigNegocio) -> None:
    """R14, R15, R21: el pedido se acumula entre mensajes, con una consulta en el medio, hasta el resumen."""
    agente = _Agente(
        _registrar(producto="sellos", nombre_cliente=NOMBRE),
        _registrar(material=MATERIAL_A_DEFINIR),
        Decision("consulta_general", ConsultaGeneral(tema="direccion")),
        _registrar(medidas="4x2 cm", cantidad=3),
        _registrar(fecha_necesita="2026-10-09", tiene_diseno="requiere_servicio"),
    )
    dichos = ("quiero sellos", "no sé, decidilo vos", "¿dónde están?", "4x2, 3", "el viernes, diseñámelo")

    textos = [procesar_lote(CONV, [_mensaje(dicho)], config, memoria, agente) for dicho in dichos]

    pedido = _pedido(memoria)
    assert pedido == Pedido(
        telefono=TELEFONO, nombre_cliente=NOMBRE, producto="sellos", material=MATERIAL_A_DEFINIR,
        medidas="4x2 cm", cantidad=3, fecha_necesita=date(2026, 10, 9), tiene_diseno="requiere_servicio",
    )
    assert textos == [
        pregunta_por_dato("material", config),
        con_aviso_de_material(pregunta_por_dato("medidas", config), es_resumen=False),
        respuesta_faq("direccion", config),
        pregunta_por_dato("fecha_necesita", config),
        resumen_pedido(pedido, config),
    ]
    assert _historial(memoria)[1::2] == [
        MARCADOR_REPREGUNTA_MATERIAL, "[dato_faltante: medidas]", "[consulta_general: direccion]",
        "[dato_faltante: fecha_necesita]", "[pedido_pendiente_confirmacion]",
    ]
    assert agente.llamadas[3]["pedido"].material == MATERIAL_A_DEFINIR
