import itertools
import logging
import threading
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
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
from app.chatwoot import Adjunto, Contacto, MensajeEntrante
from app.config import ConfigNegocio
from app.memoria import RETENCION_PROCESADOS, Charla, ErrorMemoria, Memoria
from app.pedidos import ArchivoAdjunto, Pedido
from app.respuestas import (
    MENSAJE_ERROR_AL_GUARDAR,
    MENSAJE_ERROR_INTERNO,
    MENSAJE_NO_ENTENDIDO,
    MENSAJE_PEDIDO_CONFIRMADO,
    MENSAJE_PEDIDO_RECHAZADO,
    MENSAJE_TIPO_NO_SOPORTADO,
    acuse_de_archivos,
    acuse_de_archivos_despues_de_confirmar,
    aviso_de_descartados,
    con_aviso_de_material,
    con_frase_de_horario,
    con_pregunta_del_nombre,
    nota_de_archivos,
    nota_de_derivacion,
    pregunta_por_dato,
    respuesta_faq,
    resumen_pedido,
    texto_derivacion,
)
from app.sheets import ErrorPlanilla
from app.tools import MARCADOR_REPREGUNTA_MATERIAL, MATERIAL_A_DEFINIR
from app.turno import NO_SOPORTADO, Resultado, procesar_lote
from tests.conftest import HORA_DE_PRUEBA, TELEFONO

CONV = 73915  # un número que no puede aparecer por azar en el alias ni en el teléfono
ID_CONTACTO = 4821
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


class _Planilla:
    """La planilla falsa: guarda las filas o falla con `error`. Con `esperar`, se queda en escribir_fila
    hasta que el test la suelta."""

    def __init__(self, error: Exception | None = None, esperar: bool = False) -> None:
        self.filas: list[dict[str, str]] = []
        self.error, self.esperar = error, esperar
        self.entro, self.soltar = threading.Event(), threading.Event()

    def escribir_fila(self, valores: Mapping[str, str]) -> None:
        self.entro.set()
        if self.esperar:
            self.soltar.wait(timeout=5)  # con tope: un test roto no cuelga la suite
        if self.error is not None:
            raise self.error
        self.filas.append(dict(valores))


class _Contactos:
    """El `actualizar_contacto` falso: guarda lo que se escribiría en Chatwoot y contesta `sale`."""

    def __init__(self, sale: bool = True) -> None:
        self.sale = sale
        self.escritos: list[tuple[int, dict[str, object]]] = []

    def __call__(self, id_contacto: int, atributos: dict[str, object]) -> bool:
        self.escritos.append((id_contacto, atributos))
        return self.sale


@pytest.fixture
def memoria(tmp_path: Path) -> Iterator[Memoria]:
    abierta = Memoria(tmp_path / "memoria.db")
    yield abierta
    abierta.cerrar()


def _mensaje(
    texto: str = "hola", *, nombre: str | None = NOMBRE, telefono: str | None = TELEFONO,
    adjuntos: Sequence[Adjunto] = (), creado: datetime | None = None, atributos: dict[str, Any] | None = None,
) -> MensajeEntrante:
    """Sin `atributos`, el contacto llega sin registro (R41) y el turno no escribe nada en Chatwoot."""
    contacto = Contacto(nombre=nombre, telefono=telefono, id=ID_CONTACTO, atributos=atributos)
    return MensajeEntrante(
        evento="message_created", id_mensaje=next(_IDS), contenido=texto, id_conversacion=CONV,
        contacto=contacto, adjuntos=list(adjuntos), creado=creado,
    )


def _adjunto(tipo: str = "file", extension: str | None = "pdf") -> Adjunto:
    ident = next(_IDS)
    return Adjunto(id=ident, tipo=tipo, extension=extension, tamano=1000 + ident)  # otro tamaño: otro archivo


def _archivo(*adjuntos: Adjunto) -> MensajeEntrante:
    """Un mensaje sin epígrafe con los adjuntos dados, o con un PDF."""
    return _mensaje("", adjuntos=adjuntos or [_adjunto()])


def _lote(*argumentos: Any, **opciones: Any) -> str | None:
    """El texto de procesar_lote, para los tests que miran solo la respuesta al cliente."""
    return procesar_lote(*argumentos, **opciones).texto


def _derivado(motivo: str, config: ConfigNegocio, ahora: datetime = HORA_DE_PRUEBA) -> str:
    """R38: el texto del motivo con la frase de horario."""
    return con_frase_de_horario(texto_derivacion(motivo, config), config, ahora)


def _registrar(**campos: Any) -> Decision:
    return Decision("registrar_pedido", RegistrarPedido(**campos))


def _confirmar(acepta: bool) -> Decision:
    return Decision("confirmar_pedido", ConfirmarPedido(acepta=acepta))


def _turno(
    memoria: Memoria, config: ConfigNegocio, decision: Decision | SinTool | ErrorApi, texto: str = "hola",
    planilla: _Planilla | None = None,
) -> str | None:
    return _lote(CONV, [_mensaje(texto)], config, memoria, _Agente(decision), planilla or _Planilla())


def _pendiente(memoria: Memoria, config: ConfigNegocio) -> Pedido:
    """Deja el pedido completo con el resumen mostrado, y lo devuelve."""
    _turno(memoria, config, _registrar(**COMPLETO))
    return _pedido(memoria)


def _confirmado(memoria: Memoria, config: ConfigNegocio, planilla: _Planilla | None = None) -> Pedido:
    """Deja el pedido confirmado, con su fila escrita, y lo devuelve."""
    pedido = _pendiente(memoria, config)
    assert _turno(memoria, config, _confirmar(True), "sí", planilla) == MENSAJE_PEDIDO_CONFIRMADO
    return pedido


def _lanzar(
    resultados: dict[str, str | None], clave: str, lote: list[MensajeEntrante], config: ConfigNegocio,
    memoria: Memoria, agente: _Agente, planilla: _Planilla,
) -> threading.Thread:
    """Corre el lote en otro hilo y deja su respuesta en `resultados[clave]`."""
    def correr() -> None:
        resultados[clave] = _lote(CONV, lote, config, memoria, agente, planilla)

    hilo = threading.Thread(target=correr)
    hilo.start()
    return hilo


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


# SPECS §6 · un camino por tool


def test_derivar_contesta_el_texto_del_motivo(memoria: Memoria, config: ConfigNegocio) -> None:
    """R15: sin_stock deriva con el texto de su motivo, sin alternativas."""
    texto = _turno(memoria, config, Decision("derivar_a_asesor", DerivarAAsesor(motivo="sin_stock")))

    assert texto == _derivado("sin_stock", config)
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


def test_confirmar_sin_pedido_completo_no_confirma(memoria: Memoria, config: ConfigNegocio) -> None:
    """R1: confirmar un pedido a medias da sin_pedido_para_confirmar y la repregunta fija, sin fila."""
    _turno(memoria, config, _registrar(producto="sellos"))
    planilla = _Planilla()

    texto = _turno(memoria, config, _confirmar(True), "sí", planilla)

    assert texto == MENSAJE_NO_ENTENDIDO
    assert planilla.filas == []
    assert _historial(memoria)[-1] == "[sin_pedido_para_confirmar]"


def test_sin_credenciales_de_la_planilla_no_dice_listo(memoria: Memoria, config: ConfigNegocio) -> None:
    """R2, R53: con la planilla por defecto y sin credenciales, el "sí" recibe el error de guardado, sin red."""
    pedido = _pendiente(memoria, config)

    texto = _lote(CONV, [_mensaje("sí")], config, memoria, _Agente(_confirmar(True)))

    assert texto == MENSAJE_ERROR_AL_GUARDAR
    assert _pedido(memoria) == pedido


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
    _lote(CONV, [_mensaje(telefono=otro)], config, memoria, _Agente(_registrar(producto="sellos")))

    assert _pedido(memoria).telefono == otro


def test_un_contacto_sin_telefono_no_arma_un_pedido(memoria: Memoria, config: ConfigNegocio) -> None:
    """R13: sin teléfono no se inventa uno: el turno falla con el mensaje de error y se desmarca (R23)."""
    mensaje = _mensaje(telefono=None)

    texto = _lote(CONV, [mensaje], config, memoria, _Agente(_registrar(producto="sellos")))

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

    _lote(CONV, [_mensaje(nombre=nombre)], config, memoria, agente)

    assert agente.llamadas[0]["nombre_perfil"] == esperado


def test_el_perfil_va_al_prompt_en_una_linea_y_con_tope(memoria: Memoria, config: ConfigNegocio) -> None:
    """R43, R35, R19: un perfil con saltos de línea no mete renglones en el prompt, y se recorta a 60."""
    agente = _Agente(HORARIOS)

    _lote(CONV, [_mensaje(nombre="Ana\n" + "b" * 96)], config, memoria, agente)

    assert agente.llamadas[0]["nombre_perfil"] == "Ana " + "b" * 56


# R35 · topes


def test_un_mensaje_enorme_entra_recortado_a_la_memoria_y_a_la_api(
    memoria: Memoria, config: ConfigNegocio
) -> None:
    """R35: de un mensaje de 10.000 caracteres se guardan y se mandan a la API los 4.096 de WhatsApp."""
    agente = _Agente(HORARIOS)

    _lote(CONV, [_mensaje("x" * 10_000)], config, memoria, agente)

    assert agente.llamadas[0]["charla"].mensajes[0].content == "x" * 4096
    assert _historial(memoria)[0] == "x" * 4096


# R23 y R24 · dedup y compuerta


def test_el_mismo_id_dos_veces_se_procesa_una_sola(memoria: Memoria, config: ConfigNegocio) -> None:
    """R23: el reintento de un mensaje ya procesado no llama a la API ni contesta."""
    mensaje, agente = _mensaje(), _Agente(HORARIOS, HORARIOS)

    assert _lote(CONV, [mensaje], config, memoria, agente) == respuesta_faq("horarios", config)
    assert _lote(CONV, [mensaje], config, memoria, agente) is None

    assert len(agente.llamadas) == 1
    assert _historial(memoria) == ["hola", "[consulta_general: horarios]"]


def test_un_lote_es_un_solo_turno(memoria: Memoria, config: ConfigNegocio) -> None:
    """R30: los mensajes nuevos del lote van juntos en una sola llamada; el repetido entra una vez (R23)."""
    primero, segundo = _mensaje("quiero sellos"), _mensaje("3 de goma")
    agente = _Agente(_registrar(producto="sellos", material="goma", cantidad=3))

    _lote(CONV, [primero, segundo, primero], config, memoria, agente)

    assert len(agente.llamadas) == 1
    assert [m.content for m in agente.llamadas[0]["charla"].mensajes] == ["quiero sellos", "3 de goma"]


def test_un_mensaje_sin_id_no_se_procesa(memoria: Memoria, config: ConfigNegocio) -> None:
    """R24: sin id no hay compuerta de dedup, y sin compuerta no se procesa."""
    agente = _Agente()
    mensaje = _mensaje().model_copy(update={"id_mensaje": None})

    assert _lote(CONV, [mensaje], config, memoria, agente) is None
    assert agente.llamadas == []


def test_si_falla_el_dedup_contesta_el_error_sin_llamar_a_la_api(
    memoria: Memoria, config: ConfigNegocio, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R24: si la consulta de dedup falla, no se procesa y el cliente recibe el mensaje de error."""
    def fallar(*_: object) -> bool:
        raise ErrorMemoria("procesados: OperationalError")

    monkeypatch.setattr(memoria, "marcar_procesado", fallar)
    agente = _Agente()

    assert _lote(CONV, [_mensaje()], config, memoria, agente) == MENSAJE_ERROR_INTERNO
    assert agente.llamadas == []


@pytest.mark.parametrize(
    "error", [ErrorApi("timeout"), ErrorApi("conexion"), ErrorApi("http", 429), ErrorApi("http", 529)]
)
def test_un_error_transitorio_de_la_api_desmarca_el_mensaje(
    memoria: Memoria, config: ConfigNegocio, error: ErrorApi
) -> None:
    """R12, R23: error interno con su marcador, no uno de éxito; el reintento del mismo id se procesa."""
    mensaje, agente = _mensaje(), _Agente(error, HORARIOS)

    assert _lote(CONV, [mensaje], config, memoria, agente) == MENSAJE_ERROR_INTERNO
    assert _historial(memoria) == ["hola", "[error_interno]"]
    assert _lote(CONV, [mensaje], config, memoria, agente) == respuesta_faq("horarios", config)


@pytest.mark.parametrize("error", [ErrorApi("credencial"), ErrorApi("http", 400), ErrorApi("api")])
def test_un_error_que_se_repetiria_deja_el_mensaje_marcado(
    memoria: Memoria, config: ConfigNegocio, error: ErrorApi
) -> None:
    """R23: credencial, un 4xx o un error desconocido fallarían igual; el reintento no gasta otra llamada."""
    mensaje, agente = _mensaje(), _Agente(error, HORARIOS)

    assert _lote(CONV, [mensaje], config, memoria, agente) == MENSAJE_ERROR_INTERNO
    assert _lote(CONV, [mensaje], config, memoria, agente) is None
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

    _lote(CONV, [_mensaje()], config, memoria, agente)

    assert agente.llamadas[0]["ahora"] == HORA_DE_PRUEBA


def test_el_log_del_turno_no_lleva_nada_del_cliente(
    memoria: Memoria, config: ConfigNegocio, caplog: pytest.LogCaptureFixture
) -> None:
    """R52: alias, tool y camino; ni el texto, ni el nombre, ni el teléfono, ni los valores del pedido."""
    caplog.set_level(logging.INFO)
    centinela = "Centinela-7Q"
    mensaje = _mensaje(centinela, nombre=f"{centinela} Nombre")

    _lote(CONV, [mensaje], config, memoria, _Agente(_registrar(producto="sellos", medidas=centinela)))

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

    textos = [_lote(CONV, [_mensaje(dicho)], config, memoria, agente) for dicho in dichos]

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


# R28 · un turno a la vez por conversación


class _AgenteQueEspera(_Agente):
    """Se queda en `decidir` hasta que el test lo suelta; cuenta cuántos turnos hay adentro a la vez."""

    def __init__(self, *decisiones: Decision | SinTool | ErrorApi) -> None:
        super().__init__(*decisiones)
        self.entro, self.soltar = threading.Semaphore(0), threading.Event()
        self.adentro = self.maximo = 0
        self._cuenta = threading.Lock()

    def __call__(self, config: ConfigNegocio, ahora: Any, charla: Charla, **resto: Any) -> Any:
        with self._cuenta:
            self.adentro += 1
            self.maximo = max(self.maximo, self.adentro)
            decision = super().__call__(config, ahora, charla, **resto)
        self.entro.release()
        self.soltar.wait(timeout=5)  # con tope: un test roto no cuelga la suite
        with self._cuenta:
            self.adentro -= 1
        return decision


def _en_hilo(
    hilos: list[threading.Thread], conversacion: int, texto: str, config: ConfigNegocio, memoria: Memoria,
    agente: _Agente,
) -> None:
    mensaje = _mensaje(texto).model_copy(update={"id_conversacion": conversacion})
    hilo = threading.Thread(target=procesar_lote, args=(conversacion, [mensaje], config, memoria, agente))
    hilos.append(hilo)
    hilo.start()


def _soltar(agente: _AgenteQueEspera, hilos: list[threading.Thread]) -> None:
    agente.soltar.set()
    for hilo in hilos:
        hilo.join(timeout=5)
    assert not any(hilo.is_alive() for hilo in hilos)


def test_dos_mensajes_de_la_misma_conversacion_van_de_a_uno(memoria: Memoria, config: ConfigNegocio) -> None:
    """R28, R5: el segundo no entra a decidir hasta que el primero guardó; lee su pedido y no lo pisa."""
    agente = _AgenteQueEspera(_registrar(producto="sellos"), _registrar(material="goma"))
    hilos: list[threading.Thread] = []
    try:
        _en_hilo(hilos, CONV, "quiero sellos", config, memoria, agente)
        assert agente.entro.acquire(timeout=5)
        _en_hilo(hilos, CONV, "de goma", config, memoria, agente)
        assert not agente.entro.acquire(timeout=0.3)  # sin candado ya estaría adentro
    finally:
        _soltar(agente, hilos)

    assert agente.maximo == 1
    segunda = agente.llamadas[1]
    assert [m.content for m in segunda["charla"].mensajes] == [
        "quiero sellos", MARCADOR_REPREGUNTA_MATERIAL, "de goma",
    ]
    assert segunda["pedido"] == Pedido(telefono=TELEFONO, producto="sellos")
    assert _pedido(memoria) == Pedido(telefono=TELEFONO, producto="sellos", material="goma")


def test_dos_conversaciones_distintas_deciden_a_la_vez(memoria: Memoria, config: ConfigNegocio) -> None:
    """R28: el candado es por conversación; la de otro cliente no espera a que termine la primera."""
    agente = _AgenteQueEspera(HORARIOS, HORARIOS)
    hilos: list[threading.Thread] = []
    try:
        for conversacion in (CONV, CONV + 1):
            _en_hilo(hilos, conversacion, "hola", config, memoria, agente)
        assert agente.entro.acquire(timeout=5)
        assert agente.entro.acquire(timeout=5)
    finally:
        _soltar(agente, hilos)

    assert agente.maximo == 2


# R29 a R36 · archivos


def test_un_archivo_se_suma_al_pedido_sin_pasar_por_la_api(memoria: Memoria, config: ConfigNegocio) -> None:
    """R29: el archivo se suma con la hora de pared del negocio, sin zona, y fuerza el diseño en "si"."""
    adjunto = _adjunto("file", "pdf")
    mensaje = _mensaje("", adjuntos=[adjunto], creado=datetime(2026, 10, 6, 12, 30, tzinfo=UTC))
    agente = _Agente()

    _lote(CONV, [mensaje], config, memoria, agente, _Planilla())

    assert agente.llamadas == []  # §3
    assert _pedido(memoria) == Pedido(
        telefono=TELEFONO, tiene_diseno="si",
        archivos=[ArchivoAdjunto(
            id_adjunto=adjunto.id, id_mensaje=mensaje.id_mensaje, tipo="pdf", tamano=adjunto.tamano,
            hora=datetime(2026, 10, 6, 9, 30),  # 12:30 UTC en Buenos Aires
        )],
    )


def test_sin_hora_ni_extension_van_la_hora_del_lote_y_el_file_type(
    memoria: Memoria, config: ConfigNegocio
) -> None:
    """R29: sin created_at, la hora del lote; sin extensión, el file_type de Chatwoot."""
    _lote(CONV, [_archivo(_adjunto("image", None))], config, memoria, _Agente(), _Planilla())

    archivo = _pedido(memoria).archivos[0]
    assert (archivo.tipo, archivo.hora) == ("image", datetime(2026, 10, 6, 10, 0))


def test_tres_archivos_en_un_lote_reciben_un_solo_acuse(memoria: Memoria, config: ConfigNegocio) -> None:
    """R30: tres archivos del mismo lote se suman y reciben un acuse que los cuenta."""
    texto = _lote(CONV, [_archivo(), _archivo(), _archivo()], config, memoria, _Agente(), _Planilla())

    pedido = _pedido(memoria)
    assert len(pedido.archivos) == 3
    assert texto == acuse_de_archivos(3, pedido, config)
    assert _historial(memoria) == ["[archivo_recibido]"]


@pytest.mark.parametrize("audio_al_final", [True, False])
def test_el_error_de_un_archivo_no_lo_tapa_el_acuse_de_otro(
    memoria: Memoria, config: ConfigNegocio, audio_al_final: bool
) -> None:
    """R30, R36: un PDF y un audio en el mismo lote: el PDF se suma, y la respuesta es la del audio."""
    lote = [_archivo(), _archivo(_adjunto("audio", "ogg"))]

    texto = _lote(CONV, lote if audio_al_final else lote[::-1], config, memoria, _Agente(), _Planilla())

    assert texto == MENSAJE_TIPO_NO_SOPORTADO
    assert len(_pedido(memoria).archivos) == 1


@pytest.mark.parametrize("tipo", ["audio", "location", "video"])
def test_un_tipo_no_soportado_contesta_y_queda_en_el_historial(
    memoria: Memoria, config: ConfigNegocio, tipo: str
) -> None:
    """R36: un audio, una ubicación o un video reciben la respuesta fija, sin API, y quedan en el historial."""
    agente = _Agente()

    texto = _lote(CONV, [_archivo(_adjunto(tipo, None))], config, memoria, agente, _Planilla())

    assert texto == MENSAJE_TIPO_NO_SOPORTADO
    assert agente.llamadas == []
    assert _historial(memoria) == [NO_SOPORTADO, "[tipo_no_soportado]"]
    assert _charla(memoria).pedido is None


def test_un_sticker_no_cuenta_como_archivo_del_diseno(memoria: Memoria, config: ConfigNegocio) -> None:
    """R36, R29: Chatwoot guarda el sticker de WhatsApp como imagen webp; como en el bot viejo, recibe la
    respuesta fija, queda en el historial y no se suma al pedido ni fuerza el diseño."""
    _turno(memoria, config, _registrar(producto="sellos"))

    texto = _lote(CONV, [_archivo(_adjunto("image", "webp"))], config, memoria, _Agente(), _Planilla())

    assert texto == MENSAJE_TIPO_NO_SOPORTADO
    assert _pedido(memoria) == Pedido(telefono=TELEFONO, producto="sellos")
    assert _historial(memoria)[-2:] == [NO_SOPORTADO, "[tipo_no_soportado]"]


def test_una_foto_jpg_cuenta_como_archivo_del_diseno(memoria: Memoria, config: ConfigNegocio) -> None:
    """R29: una imagen jpg se suma al pedido y fuerza el diseño en "si"."""
    adjunto = _adjunto("image", "jpg")

    texto = _lote(CONV, [_archivo(adjunto)], config, memoria, _Agente(), _Planilla())

    pedido = _pedido(memoria)
    assert [(archivo.id_adjunto, archivo.tipo) for archivo in pedido.archivos] == [(adjunto.id, "jpg")]
    assert pedido.tiene_diseno == "si" and texto == acuse_de_archivos(1, pedido, config)


def test_el_archivo_que_completa_el_pedido_muestra_el_resumen(memoria: Memoria, config: ConfigNegocio) -> None:
    """R31: si con el archivo el pedido queda completo, el acuse sigue con el resumen y deja su marcador."""
    _turno(memoria, config, _registrar(**COMPLETO | {"tiene_diseno": None}))

    texto = _lote(CONV, [_archivo()], config, memoria, _Agente(), _Planilla())

    pedido = _pedido(memoria)
    assert pedido.completo
    assert texto == acuse_de_archivos(1, pedido, config)
    assert _historial(memoria)[-1] == "[pedido_pendiente_confirmacion]"


def test_el_epigrafe_va_al_historial_y_al_modelo(memoria: Memoria, config: ConfigNegocio) -> None:
    """R34, R31: el epígrafe va al modelo, que ya ve el archivo sumado; el acuse sale con el pedido final."""
    agente = _Agente(_registrar(**COMPLETO | {"tiene_diseno": None}))

    texto = _lote(CONV, [_mensaje("3 sellos de goma", adjuntos=[_adjunto()])], config, memoria, agente)

    llamada = agente.llamadas[0]
    assert [m.content for m in llamada["charla"].mensajes] == ["3 sellos de goma"]
    assert llamada["pedido"].tiene_diseno == "si"
    pedido = _pedido(memoria)
    assert pedido.completo and len(pedido.archivos) == 1
    assert texto == acuse_de_archivos(1, pedido, config)  # el último mensaje del lote trae el archivo
    assert _historial(memoria) == ["3 sellos de goma", "[pedido_pendiente_confirmacion]"]


def test_si_el_ultimo_del_lote_es_un_texto_responde_el_texto(memoria: Memoria, config: ConfigNegocio) -> None:
    """R30: como el bot viejo, un archivo y después una pregunta: responde la pregunta y el archivo queda."""
    direccion = Decision("consulta_general", ConsultaGeneral(tema="direccion"))

    texto = _lote(CONV, [_archivo(), _mensaje("¿dónde están?")], config, memoria, _Agente(direccion))

    assert texto == respuesta_faq("direccion", config)
    assert len(_pedido(memoria).archivos) == 1
    assert _historial(memoria) == ["¿dónde están?", "[consulta_general: direccion]"]


def test_una_derivacion_no_la_tapa_el_acuse_del_archivo(memoria: Memoria, config: ConfigNegocio) -> None:
    """R15, R30: una foto con "¿la tienen en lona blanca?" deriva por sin_stock; el acuse no tapa la
    derivación, ni al cliente ni en el historial, y el archivo igual queda en el pedido."""
    sin_stock = Decision("derivar_a_asesor", DerivarAAsesor(motivo="sin_stock"))
    mensaje = _mensaje("¿la tienen en lona blanca?", adjuntos=[_adjunto("image", "jpg")])

    texto = _lote(CONV, [mensaje], config, memoria, _Agente(sin_stock), _Planilla())

    assert texto == _derivado("sin_stock", config)
    assert _historial(memoria) == ["¿la tienen en lona blanca?", "[derivado_a_asesor: sin_stock]"]
    assert len(_pedido(memoria).archivos) == 1


def test_un_archivo_despues_de_confirmar_no_toca_la_fila(memoria: Memoria, config: ConfigNegocio) -> None:
    """R32, R47: con el pedido recién confirmado, el archivo no toca la fila ni arranca otro pedido; el acuse no
    dice "guardado" y la persona recibe la nota con el archivo, sin que la conversación le pase a ella."""
    planilla = _Planilla()
    confirmado = _confirmado(memoria, config, planilla)
    adjunto = _adjunto()

    resultado = procesar_lote(CONV, [_archivo(adjunto)], config, memoria, _Agente(), planilla)

    nota = nota_de_archivos(f"1. 06/10 10:00 · pdf · adjunto #{adjunto.id}")
    assert resultado == Resultado(acuse_de_archivos_despues_de_confirmar(1), nota, derivar=False)
    charla = _charla(memoria)
    assert charla.pedido is None
    assert charla.toma is not None and charla.toma.pedido == confirmado
    assert len(planilla.filas) == 1


def test_un_archivo_con_un_gracias_despues_de_confirmar_igual_se_acusa(
    memoria: Memoria, config: ConfigNegocio
) -> None:
    """R32, R6: el "gracias" que no cambia nada no contesta, pero el archivo del mismo lote se acusa igual."""
    _confirmado(memoria, config)

    texto = _lote(CONV, [_archivo(), _mensaje("gracias")], config, memoria, _Agente(_registrar()))

    assert texto == acuse_de_archivos_despues_de_confirmar(1)


def test_si_responde_el_texto_la_nota_del_archivo_igual_sale(memoria: Memoria, config: ConfigNegocio) -> None:
    """R32, R30: un archivo y una pregunta después de confirmar: contesta la pregunta y la nota no se pierde."""
    _confirmado(memoria, config)
    adjunto = _adjunto()
    lote = [_archivo(adjunto), _mensaje("¿a qué hora abren?")]

    resultado = procesar_lote(CONV, lote, config, memoria, _Agente(HORARIOS))

    nota = nota_de_archivos(f"1. 06/10 10:00 · pdf · adjunto #{adjunto.id}")
    assert resultado == Resultado(respuesta_faq("horarios", config), nota, derivar=False)


def _archivo_durante_la_escritura(
    memoria: Memoria, config: ConfigNegocio, planilla: _Planilla
) -> dict[str, str | None]:
    """El "sí" entra a escribir la fila y en ese momento llega un archivo, que tiene que esperar."""
    _pendiente(memoria, config)
    resultados: dict[str, str | None] = {}
    si = _lanzar(resultados, "si", [_mensaje("sí")], config, memoria, _Agente(_confirmar(True)), planilla)
    assert planilla.entro.wait(timeout=5)
    archivo = _lanzar(resultados, "archivo", [_archivo()], config, memoria, _Agente(), planilla)
    archivo.join(timeout=0.3)
    esperaba = archivo.is_alive()
    planilla.soltar.set()
    for hilo in (si, archivo):
        hilo.join(timeout=5)
    assert esperaba and not si.is_alive() and not archivo.is_alive()
    return resultados


def test_un_archivo_durante_la_escritura_espera_y_no_toca_la_fila(
    memoria: Memoria, config: ConfigNegocio
) -> None:
    """R33: el archivo espera a que la fila se escriba y cae en R32: la fila sale sin él."""
    planilla = _Planilla(esperar=True)

    resultados = _archivo_durante_la_escritura(memoria, config, planilla)

    assert resultados == {"si": MENSAJE_PEDIDO_CONFIRMADO, "archivo": acuse_de_archivos_despues_de_confirmar(1)}
    assert [fila["archivos"] for fila in planilla.filas] == [""]


def test_un_archivo_durante_una_escritura_que_falla_vuelve_con_el_pedido(
    memoria: Memoria, config: ConfigNegocio
) -> None:
    """R33, R2: si la escritura falla, el archivo se suma al pedido que volvió a pendiente."""
    planilla = _Planilla(ErrorPlanilla("no se pudo"), esperar=True)

    resultados = _archivo_durante_la_escritura(memoria, config, planilla)

    pedido = _pedido(memoria)
    assert len(pedido.archivos) == 1 and planilla.filas == []
    assert resultados == {"si": MENSAJE_ERROR_AL_GUARDAR, "archivo": acuse_de_archivos(1, pedido, config)}


def test_el_archivo_61_no_entra_y_se_deriva(memoria: Memoria, config: ConfigNegocio) -> None:
    """R35: con 60 archivos en el pedido, el 61 no se suma y se deriva."""
    _lote(CONV, [_archivo() for _ in range(60)], config, memoria, _Agente(), _Planilla())

    texto = _lote(CONV, [_archivo()], config, memoria, _Agente(), _Planilla())

    assert texto == _derivado("fuera_de_alcance", config)
    assert len(_pedido(memoria).archivos) == 60


# R47 y R38 · derivación

SIN_STOCK = Decision("derivar_a_asesor", DerivarAAsesor(motivo="sin_stock"))
REGISTRADA = "Ana Registrada"
# R45: un contacto que ya pasó por el alta, sin pregunta viva (R42), con su nombre registrado
CONOCIDO = {
    "primer_contacto": "2026-09-01T10:00:00-03:00", "nombre_preguntado": False, "nombre_cliente": REGISTRADA,
}


def test_derivar_deja_la_nota_y_pasa_la_charla_a_una_persona(memoria: Memoria, config: ConfigNegocio) -> None:
    """R47, R38: con el resumen a la vista, derivar contesta con la frase de horario, deja la nota con el
    pedido como lo entendió el bot y pide el pase; el pedido pendiente se descarta."""
    pedido = _pendiente(memoria, config)

    resultado = procesar_lote(CONV, [_mensaje("¿hay en lona?")], config, memoria, _Agente(SIN_STOCK))

    nota = nota_de_derivacion("sin_stock", pedido, NOMBRE, config)
    assert resultado == Resultado(_derivado("sin_stock", config), nota, derivar=True)
    assert _charla(memoria).pedido is None


def test_fuera_de_horario_la_derivacion_dice_cuando_reabre(memoria: Memoria, config: ConfigNegocio) -> None:
    """R38: a las 20 del martes no promete que contesta ya: dice que reabre el miércoles a las 9."""
    cerrado = datetime(2026, 10, 6, 20, 0, tzinfo=HORA_DE_PRUEBA.tzinfo)
    config.fijar_ahora(cerrado)

    texto = _lote(CONV, [_mensaje("¿hay en lona?")], config, memoria, _Agente(SIN_STOCK))

    assert texto == _derivado("sin_stock", config, cerrado)
    assert texto is not None and texto.endswith("cuando volvamos a abrir: mañana desde las 09:00.")


def test_la_nota_lleva_el_nombre_registrado(memoria: Memoria, config: ConfigNegocio) -> None:
    """R47, R45: sin pedido, la nota lleva el nombre registrado del contacto, no el del perfil."""
    lote = [_mensaje("quiero hablar con alguien", atributos=CONOCIDO)]

    resultado = procesar_lote(CONV, lote, config, memoria, _Agente(SIN_STOCK), actualizar_contacto=_Contactos())

    assert resultado.nota == nota_de_derivacion("sin_stock", None, REGISTRADA, config)


def test_la_nota_nunca_lleva_el_telefono(memoria: Memoria, config: ConfigNegocio) -> None:
    """R47, R13, R43: sin nombre registrado ni pedido, el perfil que es el teléfono no va a la nota."""
    lote = [_mensaje("quiero hablar con alguien", nombre=TELEFONO)]

    resultado = procesar_lote(CONV, lote, config, memoria, _Agente(SIN_STOCK))

    assert resultado.nota == nota_de_derivacion("sin_stock", None, None, config)
    assert TELEFONO not in resultado.nota


def test_despues_de_derivar_un_si_no_confirma(memoria: Memoria, config: ConfigNegocio) -> None:
    """R47, R1: si la charla vuelve al bot, un "sí" no confirma el resumen que ya vio una persona."""
    planilla = _Planilla()
    _pendiente(memoria, config)
    _turno(memoria, config, SIN_STOCK, "¿hay en lona?")

    assert _turno(memoria, config, _confirmar(True), "sí", planilla) == MENSAJE_NO_ENTENDIDO
    assert planilla.filas == []


def test_lo_creado_antes_de_la_derivacion_no_se_contesta(
    memoria: Memoria, config: ConfigNegocio, caplog: pytest.LogCaptureFixture
) -> None:
    """R47, R52: el segundo mensaje llegó en pending mientras el primero estaba en la API y corre después del
    pase: se marca procesado, pero no se anota, no se contesta, no acusa su archivo ni deriva otra vez."""
    caplog.set_level(logging.INFO, logger="app.turno")
    _turno(memoria, config, SIN_STOCK, "¿hay en lona?")
    agente = _Agente(HORARIOS)
    antes = _mensaje("¿y en vinilo?", adjuntos=[_adjunto()], creado=HORA_DE_PRUEBA - timedelta(seconds=1))
    justo = _mensaje("¿hola?", creado=HORA_DE_PRUEBA)  # anterior o igual

    resultado = procesar_lote(CONV, [antes, justo], config, memoria, agente, _Planilla())

    assert resultado == Resultado(None)
    assert agente.llamadas == [] and _charla(memoria).pedido is None
    assert _historial(memoria) == ["¿hay en lona?", "[derivado_a_asesor: sin_stock]"]
    assert procesar_lote(CONV, [antes], config, memoria, agente) == Resultado(None)  # R23: quedó marcado
    assert "turno: anterior a la derivación alias=" in caplog.text and "vinilo" not in caplog.text


@pytest.mark.parametrize("creado", [HORA_DE_PRUEBA + timedelta(seconds=1), None])
def test_lo_creado_despues_de_la_derivacion_se_procesa(
    memoria: Memoria, config: ConfigNegocio, creado: datetime | None
) -> None:
    """R47: la persona devolvió la conversación a pending; lo que el cliente escribe después, o sin hora, se
    contesta normal."""
    _turno(memoria, config, SIN_STOCK, "¿hay en lona?")

    texto = _lote(CONV, [_mensaje("¿a qué hora abren?", creado=creado)], config, memoria, _Agente(HORARIOS))

    assert texto == respuesta_faq("horarios", config)


def test_un_error_no_deja_nota_ni_pase(memoria: Memoria, config: ConfigNegocio) -> None:
    """R24, R47: un turno que falla contesta el error interno, sin nota ni pase."""
    agente = _Agente(_registrar(producto="sellos"))

    resultado = procesar_lote(CONV, [_mensaje(telefono=None)], config, memoria, agente)

    assert resultado == Resultado(MENSAJE_ERROR_INTERNO)


# R41 a R45 · primer contacto

PIDE_EL_NOMBRE = Decision("pedir_dato_faltante", PedirDatoFaltante(dato="nombre_cliente"))
SIN_NOMBRE = {campo: valor for campo, valor in COMPLETO.items() if campo != "nombre_cliente"}


def _con_registro(
    memoria: Memoria, config: ConfigNegocio, agente: _Agente, contactos: _Contactos, texto: str = "hola",
    atributos: dict[str, Any] | None = None, planilla: _Planilla | None = None,
) -> str | None:
    """Un lote con el registro del contacto en el payload (R45); sin `atributos`, un contacto nuevo."""
    lote = [_mensaje(texto, atributos={} if atributos is None else atributos)]
    return _lote(CONV, lote, config, memoria, agente, planilla or _Planilla(), contactos)


def _alta(preguntado: bool, **nombre: str) -> tuple[int, dict[str, object]]:
    """R45: lo que el turno escribe en el contacto nuevo, con la hora del lote."""
    alta = {"primer_contacto": HORA_DE_PRUEBA.isoformat(), "nombre_preguntado": preguntado}
    return ID_CONTACTO, alta | nombre


def test_un_contacto_nuevo_recibe_la_pregunta_si_el_alta_quedo_escrita(
    memoria: Memoria, config: ConfigNegocio
) -> None:
    """R41, R44: el alta se escribe y la pregunta del nombre va debajo de la respuesta elegida."""
    contactos = _Contactos()

    texto = _con_registro(memoria, config, _Agente(HORARIOS), contactos)

    assert texto == con_pregunta_del_nombre(respuesta_faq("horarios", config))
    assert contactos.escritos == [_alta(True)]


def test_si_el_alta_falla_no_pregunta(memoria: Memoria, config: ConfigNegocio) -> None:
    """R41: sin el alta escrita, el nombre no se pregunta y la respuesta sale igual."""
    contactos = _Contactos(sale=False)

    texto = _con_registro(memoria, config, _Agente(HORARIOS), contactos)

    assert texto == respuesta_faq("horarios", config)
    assert contactos.escritos == [_alta(True)]
    assert _charla(memoria).atributos == {}


def test_con_un_error_del_turno_ni_registra_ni_pregunta(memoria: Memoria, config: ConfigNegocio) -> None:
    """R41: si el turno tuvo un error, ni se pregunta ni se registra."""
    contactos = _Contactos()

    texto = _con_registro(memoria, config, _Agente(ErrorApi("credencial")), contactos)

    assert texto == MENSAJE_ERROR_INTERNO
    assert contactos.escritos == []


def test_la_repregunta_del_nombre_es_la_pregunta(memoria: Memoria, config: ConfigNegocio) -> None:
    """R42: si el bot ya repregunta el nombre, el alta queda con la pregunta hecha y no se suma otra."""
    contactos = _Contactos()

    texto = _con_registro(memoria, config, _Agente(PIDE_EL_NOMBRE), contactos)

    assert texto == pregunta_por_dato("nombre_cliente", config)
    assert contactos.escritos == [_alta(True)]


def test_el_segundo_mensaje_con_el_payload_viejo_no_pregunta_otra_vez(
    memoria: Memoria, config: ConfigNegocio
) -> None:
    """R42, R45: el segundo webhook puede traer los atributos de antes del alta; manda lo que la charla ya
    escribió: no hay otra alta ni otra pregunta, y la pregunta viva va al prompt."""
    contactos, agente = _Contactos(), _Agente(HORARIOS, HORARIOS)
    _con_registro(memoria, config, agente, contactos)

    texto = _con_registro(memoria, config, agente, contactos)

    assert texto == respuesta_faq("horarios", config)
    assert contactos.escritos == [_alta(True)]
    assert agente.llamadas[1]["nombre_preguntado"] is True


def test_el_nombre_que_contesta_queda_registrado(memoria: Memoria, config: ConfigNegocio) -> None:
    """R42, R45: con la pregunta viva, el nombre que dice el cliente se escribe en el contacto."""
    contactos, agente = _Contactos(), _Agente(HORARIOS, _registrar(nombre_cliente="Bruno Díaz"))
    _con_registro(memoria, config, agente, contactos)

    _con_registro(memoria, config, agente, contactos, "Bruno Díaz")

    assert contactos.escritos == [_alta(True), (ID_CONTACTO, {"nombre_cliente": "Bruno Díaz"})]


def test_un_contacto_nuevo_que_confirma_queda_con_su_nombre(memoria: Memoria, config: ConfigNegocio) -> None:
    """R45: un contacto nuevo que confirma en este lote queda registrado con el nombre del pedido, sin
    pregunta: el "¡Listo!" no lleva nada debajo."""
    _pendiente(memoria, config)
    contactos, planilla = _Contactos(), _Planilla()

    texto = _con_registro(memoria, config, _Agente(_confirmar(True)), contactos, "sí", planilla=planilla)

    assert texto == MENSAJE_PEDIDO_CONFIRMADO
    assert contactos.escritos == [_alta(False, nombre_cliente=NOMBRE)]
    assert len(planilla.filas) == 1


def test_el_nombre_registrado_se_precarga_en_el_pedido(memoria: Memoria, config: ConfigNegocio) -> None:
    """R43: un pedido nuevo arranca con el nombre registrado, y el bot no lo pregunta."""
    agente = _Agente(_registrar(**SIN_NOMBRE))

    texto = _con_registro(memoria, config, agente, _Contactos(), "3 sellos de goma", CONOCIDO)

    pedido = _pedido(memoria)
    assert pedido.nombre_cliente == REGISTRADA
    assert texto == resumen_pedido(pedido, config)


def test_con_un_nombre_registrado_no_lo_pregunta_lo_completa(memoria: Memoria, config: ConfigNegocio) -> None:
    """R43: si el modelo pide el nombre y hay uno registrado, se completa con ese y sigue el resumen."""
    _turno(memoria, config, _registrar(**SIN_NOMBRE))

    texto = _con_registro(memoria, config, _Agente(PIDE_EL_NOMBRE), _Contactos(), "hola", CONOCIDO)

    pedido = _pedido(memoria)
    assert pedido.nombre_cliente == REGISTRADA
    assert texto == resumen_pedido(pedido, config)


def test_el_nombre_registrado_no_es_un_cambio_sobre_el_confirmado(
    memoria: Memoria, config: ConfigNegocio
) -> None:
    """R43, R6: con el confirmado precargado con el nombre registrado, el reenvío sin nombre no es un cambio,
    y el nombre que ya estaba registrado no se vuelve a escribir."""
    contactos, planilla = _Contactos(), _Planilla()
    agente = _Agente(_registrar(**SIN_NOMBRE), _confirmar(True), _registrar(**SIN_NOMBRE))
    for texto in ("3 sellos de goma", "sí"):
        _con_registro(memoria, config, agente, contactos, texto, CONOCIDO, planilla)

    texto = _con_registro(memoria, config, agente, contactos, "gracias", CONOCIDO, planilla)

    assert texto is None
    assert [fila["nombre_cliente"] for fila in planilla.filas] == [REGISTRADA]
    assert contactos.escritos == []


def test_un_contacto_nuevo_que_deriva_no_recibe_la_pregunta(memoria: Memoria, config: ConfigNegocio) -> None:
    """R41, R47: la pregunta del nombre no va debajo de una derivación (decidido 2026-09-30); sin nombre no hay
    alta, y la pregunta sale en el próximo turno que no deriva."""
    contactos, agente = _Contactos(), _Agente(SIN_STOCK, HORARIOS)

    derivado = _con_registro(memoria, config, agente, contactos, "¿hay en lona?")
    assert (derivado, contactos.escritos) == (_derivado("sin_stock", config), [])

    texto = _con_registro(memoria, config, agente, contactos, "¿a qué hora abren?")
    assert texto == con_pregunta_del_nombre(respuesta_faq("horarios", config))
    assert contactos.escritos == [_alta(True)]


def test_un_contacto_nuevo_con_un_audio_no_recibe_la_pregunta(memoria: Memoria, config: ConfigNegocio) -> None:
    """R41, R36: tampoco va debajo del tipo no soportado; sin nombre, no hay alta."""
    contactos = _Contactos()
    audio = _mensaje("", adjuntos=[_adjunto("audio", "ogg")], atributos={})

    texto = _lote(CONV, [audio], config, memoria, _Agente(), _Planilla(), contactos)

    assert (texto, contactos.escritos) == (MENSAJE_TIPO_NO_SOPORTADO, [])


def test_un_contacto_nuevo_que_dijo_su_nombre_queda_registrado_aunque_no_se_pregunte(
    memoria: Memoria, config: ConfigNegocio
) -> None:
    """R45, R36: un audio y "soy Bruno Díaz": la respuesta es la del audio, y el alta sale con el nombre dicho."""
    contactos = _Contactos()
    lote = [_mensaje("", adjuntos=[_adjunto("audio", "ogg")], atributos={}), _mensaje("soy Bruno Díaz", atributos={})]

    texto = _lote(CONV, lote, config, memoria, _Agente(_registrar(nombre_cliente="Bruno Díaz")), _Planilla(), contactos)

    assert texto == MENSAJE_TIPO_NO_SOPORTADO
    assert contactos.escritos == [_alta(False, nombre_cliente="Bruno Díaz")]
