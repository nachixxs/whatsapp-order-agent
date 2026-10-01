from datetime import datetime, timedelta

import pytest

from app.contactos import (
    Plan,
    Registro,
    atributos_del_alta,
    atributos_del_nombre,
    leer_registro,
    nombre_limpio,
    plan_primer_contacto,
    pregunta_viva,
)
from app.pedidos import TOPE_NOMBRE
from tests.conftest import HORA_DE_PRUEBA

AHORA = HORA_DE_PRUEBA
PERFIL = "Perfil Inventado"
NADA = Plan(None, False)
NUEVO = Registro(None, False, None)
PREGUNTADO = Registro(AHORA - timedelta(hours=1), True, None)  # la pregunta del alta sigue viva


def _plan(registro: Registro | None, **cambios: object) -> Plan:
    entradas: dict[str, object] = {
        "error": False, "respuesta_vacia": False, "nombre_dicho": None, "nombre_perfil": PERFIL,
        "repregunta_del_nombre": False, "nombre_confirmado": None, "sin_pregunta": False,
    }
    return plan_primer_contacto(registro, AHORA, **(entradas | cambios))  # type: ignore[arg-type]


# leer_registro


@pytest.mark.parametrize("atributos", [None, [], "primer_contacto", 7])
def test_sin_diccionario_es_sin_dato(atributos: object) -> None:
    """R41: sin custom_attributes no hay dato: ni se pregunta ni se registra."""
    assert leer_registro(atributos) is None
    assert _plan(leer_registro(atributos)) == NADA


def test_diccionario_sin_primer_contacto_es_nuevo() -> None:
    """R45: un diccionario sin primer_contacto es un contacto nuevo."""
    assert leer_registro({"otro_atributo": "x"}) == NUEVO


def test_el_alta_se_lee_de_vuelta() -> None:
    """R45: lo que escribe el alta se lee igual del webhook, con la fecha y su zona."""
    atributos = atributos_del_alta(AHORA, preguntado=True, nombre="Ana Prueba")

    assert atributos == {
        "primer_contacto": "2026-10-06T10:00:00-03:00", "nombre_preguntado": True, "nombre_cliente": "Ana Prueba",
    }
    assert leer_registro(atributos) == Registro(AHORA, True, "Ana Prueba")


def test_lo_que_la_charla_escribio_le_gana_al_payload() -> None:
    """R42, R45: un payload armado antes del alta no hace que el contacto vuelva a ser nuevo."""
    escritos = atributos_del_alta(AHORA, preguntado=True) | atributos_del_nombre("Ana Prueba")

    assert leer_registro({}, escritos) == Registro(AHORA, True, "Ana Prueba")
    assert leer_registro({"nombre_cliente": "Viejo"}, {"nombre_cliente": "Ana Prueba"}) == Registro(
        None, False, "Ana Prueba"
    )
    assert leer_registro(None, escritos) is None  # R41: sin payload sigue siendo sin dato


@pytest.mark.parametrize("fecha", ["ayer", "2026-13-01T10:00:00-03:00", "", 1759755600, None])
def test_fecha_ilegible_cuenta_como_ausente(fecha: object) -> None:
    """R45: una fecha que no se puede leer cuenta como ausente: el contacto es nuevo."""
    registro = leer_registro({"primer_contacto": fecha, "nombre_preguntado": True})
    assert registro is not None and registro.primer_contacto is None


def test_fecha_sin_zona_cuenta_como_ausente() -> None:
    """R45, R42: sin zona no se sabe con qué reloj se escribió; la ventana no se mide."""
    registro = leer_registro({"primer_contacto": "2026-10-06T09:00:00", "nombre_preguntado": True})
    assert registro is not None and registro.primer_contacto is None
    assert not pregunta_viva(registro, AHORA)


@pytest.mark.parametrize("valor", ["true", "Sí", 1, None])
def test_nombre_preguntado_que_no_es_booleano_cuenta_como_no(valor: object) -> None:
    """R45: un nombre_preguntado como texto o número es ilegible: la pregunta no está viva."""
    registro = leer_registro({"primer_contacto": AHORA.isoformat(), "nombre_preguntado": valor})
    assert registro == Registro(AHORA, False, None)
    assert not pregunta_viva(registro, AHORA)


@pytest.mark.parametrize("nombre", [123, ["Ana"], "", "   \n "])
def test_nombre_cliente_ilegible_o_vacio_es_none(nombre: object) -> None:
    """R45: un nombre que no es texto, o vacío, cuenta como ausente."""
    registro = leer_registro({"primer_contacto": AHORA.isoformat(), "nombre_cliente": nombre})
    assert registro is not None and registro.nombre_cliente is None


def test_nombre_con_saltos_de_linea_y_largo_se_limpia() -> None:
    """R44: el nombre queda en una línea y con el tope del pedido, limpio una sola vez al leer."""
    registro = leer_registro({"nombre_cliente": "  Ana\nPrueba\r\n" + "x" * 100})

    assert registro is not None and registro.nombre_cliente == ("Ana Prueba " + "x" * 100)[:TOPE_NOMBRE]
    assert nombre_limpio("Ana   Prueba \n") == "Ana Prueba"


# atributos y ventana


def test_el_alta_sin_zona_no_se_escribe() -> None:
    """R37, R45: el alta lleva la hora con zona; sin ella, lanza en vez de escribir otro reloj."""
    with pytest.raises(ValueError):
        atributos_del_alta(datetime(2026, 10, 6, 10, 0), preguntado=True)


def test_el_alta_sin_nombre_no_toca_nombre_cliente() -> None:
    """R45: el alta sin nombre no escribe nombre_cliente (el update de Chatwoot mezcla, no pisa)."""
    assert atributos_del_alta(AHORA, preguntado=False) == {
        "primer_contacto": "2026-10-06T10:00:00-03:00", "nombre_preguntado": False,
    }


@pytest.mark.parametrize(
    ("hace", "viva"),
    [(timedelta(hours=6) - timedelta(seconds=1), True), (timedelta(hours=6), True),
     (timedelta(hours=6, seconds=1), False)],
)
def test_la_ventana_de_la_pregunta_es_el_ttl(hace: timedelta, viva: bool) -> None:
    """R42, R22: la pregunta vale 6 horas desde el alta; después un nombre suelto ya no es respuesta."""
    assert pregunta_viva(Registro(AHORA - hace, True, None), AHORA) is viva


def test_sin_pregunta_en_el_alta_no_hay_ventana() -> None:
    """R42: si el alta no preguntó el nombre, no hay pregunta viva aunque sea reciente."""
    assert not pregunta_viva(Registro(AHORA, False, None), AHORA)
    assert not pregunta_viva(NUEVO, AHORA)
    assert not pregunta_viva(None, AHORA)


# el plan: contacto nuevo


def test_turno_con_error_ni_registra_ni_pregunta() -> None:
    """R41: si el turno tuvo un error, el contacto nuevo sigue nuevo y no se le pregunta."""
    assert _plan(NUEVO, error=True) == NADA


def test_respuesta_vacia_ni_registra_ni_pregunta() -> None:
    """R44: la pregunta nunca va sobre una respuesta vacía, y sin pregunta no hay alta."""
    assert _plan(NUEVO, respuesta_vacia=True) == NADA


def test_nuevo_registra_y_pregunta_si_se_escribe() -> None:
    """R41: el alta con nombre_preguntado y la pregunta, que va solo si la escritura sale."""
    assert _plan(NUEVO) == Plan(atributos_del_alta(AHORA, preguntado=True), True)


def test_nuevo_que_dijo_su_nombre_se_registra_sin_pregunta() -> None:
    """R43: dijo un nombre que no es el del perfil: alta con ese nombre, sin preguntar."""
    assert _plan(NUEVO, nombre_dicho="Ana Prueba") == Plan(
        atributos_del_alta(AHORA, preguntado=False, nombre="Ana Prueba"), False
    )


def test_nuevo_con_el_nombre_del_perfil_igual_se_pregunta() -> None:
    """R43: el nombre del perfil copiado (aunque cambie la mayúscula) no cuenta como dicho."""
    assert _plan(NUEVO, nombre_dicho="perfil inventado") == Plan(atributos_del_alta(AHORA, preguntado=True), True)


def test_nuevo_sin_perfil_el_nombre_dicho_cuenta() -> None:
    """R43: sin perfil (None) cualquier nombre dicho es el del cliente."""
    plan = _plan(NUEVO, nombre_dicho="Ana Prueba", nombre_perfil=None)
    assert plan.escribir == atributos_del_alta(AHORA, preguntado=False, nombre="Ana Prueba")


def test_nuevo_con_la_repregunta_no_se_agrega_otra() -> None:
    """R42: la repregunta del nombre cuenta como la pregunta: el alta la marca y no se suma otra."""
    assert _plan(NUEVO, repregunta_del_nombre=True) == Plan(atributos_del_alta(AHORA, preguntado=True), False)


def test_nuevo_que_confirma_se_registra_con_el_nombre_del_pedido() -> None:
    """R45: el contacto nuevo que confirma en este lote queda con el nombre del pedido, sin la pregunta."""
    assert _plan(NUEVO, nombre_confirmado=PERFIL) == Plan(
        atributos_del_alta(AHORA, preguntado=False, nombre=PERFIL), False
    )


def test_nuevo_bajo_una_derivacion_no_se_registra_ni_se_pregunta() -> None:
    """R41, R42: bajo una derivación o un tipo no soportado no va la pregunta (decidido 2026-09-30), y sin
    nombre tampoco va el alta: con nombre_preguntado en falso y sin nombre, no se preguntaría nunca."""
    assert _plan(NUEVO, sin_pregunta=True) == NADA


def test_nuevo_bajo_una_derivacion_con_su_nombre_se_registra() -> None:
    """R43, R45: si dijo su nombre, o confirmó un pedido con nombre, el alta sale igual, sin la pregunta."""
    con_nombre = Plan(atributos_del_alta(AHORA, preguntado=False, nombre="Ana Prueba"), False)
    assert _plan(NUEVO, sin_pregunta=True, nombre_dicho="Ana Prueba") == con_nombre
    assert _plan(NUEVO, sin_pregunta=True, nombre_confirmado="Ana Prueba") == con_nombre


def test_conocido_bajo_una_derivacion_igual_registra_el_nombre() -> None:
    """R42: la derivación no cambia lo del contacto ya registrado: el nombre que contesta se escribe."""
    assert _plan(PREGUNTADO, sin_pregunta=True, nombre_dicho="Ana Prueba") == Plan(
        atributos_del_nombre("Ana Prueba"), False
    )


# el plan: contacto conocido


def test_conocido_nunca_pregunta() -> None:
    """R42: el contacto ya registrado no recibe la pregunta, ni con la repregunta ni sin nombre."""
    assert _plan(PREGUNTADO) == NADA
    assert _plan(Registro(AHORA - timedelta(days=30), False, None)) == NADA


def test_nombre_dicho_con_la_pregunta_viva_se_escribe() -> None:
    """R42: con la pregunta viva, el nombre que dice el cliente se escribe, sin preguntar."""
    assert _plan(PREGUNTADO, nombre_dicho="Ana Prueba") == Plan(atributos_del_nombre("Ana Prueba"), False)


def test_nombre_dicho_con_la_pregunta_vencida_no_se_escribe() -> None:
    """R42: pasadas las 6 horas, un nombre suelto ya no es respuesta."""
    vencida = Registro(AHORA - timedelta(hours=6, seconds=1), True, None)
    assert _plan(vencida, nombre_dicho="Ana Prueba") == NADA


def test_nombre_dicho_sin_pregunta_en_el_alta_no_se_escribe() -> None:
    """R42: si el alta no preguntó (ya lo había dicho), otro nombre suelto no pisa el registrado."""
    registro = Registro(AHORA - timedelta(minutes=5), False, "Ana Prueba")
    assert _plan(registro, nombre_dicho="Otra Persona") == NADA


def test_el_nombre_del_perfil_con_la_pregunta_viva_no_se_escribe() -> None:
    """R43: el nombre del perfil no cuenta como respuesta a la pregunta."""
    assert _plan(PREGUNTADO, nombre_dicho=PERFIL) == NADA


def test_el_nombre_dicho_igual_al_registrado_no_se_reescribe() -> None:
    """R42: el mismo nombre que ya estaba no se vuelve a escribir."""
    registro = Registro(AHORA - timedelta(hours=1), True, "Ana Prueba")
    assert _plan(registro, nombre_dicho="Ana Prueba") == NADA


def test_el_pedido_confirmado_copia_su_nombre() -> None:
    """R45: el nombre del pedido confirmado se copia a nombre_cliente si es distinto del registrado."""
    registro = Registro(AHORA - timedelta(days=3), False, "Ana Prueba")
    assert _plan(registro, nombre_confirmado="Ferretería Ejemplo") == Plan(
        atributos_del_nombre("Ferretería Ejemplo"), False
    )
    assert _plan(registro, nombre_confirmado="Ana Prueba") == NADA


def test_el_pedido_confirmado_sin_nombre_registrado_copia_aunque_sea_el_perfil() -> None:
    """R45, R43: sin nombre registrado no hay nada que pisar: se copia el del pedido, aunque sea el del perfil."""
    registro = Registro(AHORA - timedelta(days=3), False, None)
    assert _plan(registro, nombre_confirmado=PERFIL) == Plan(atributos_del_nombre(PERFIL), False)


def test_el_pedido_confirmado_con_el_perfil_no_pisa_el_registrado() -> None:
    """R43: el nombre del perfil no pisa uno registrado, aunque llegue en el pedido confirmado."""
    registro = Registro(AHORA - timedelta(days=3), False, "Ana Prueba")
    assert _plan(registro, nombre_confirmado=PERFIL) == NADA


def test_con_la_pregunta_viva_gana_el_nombre_dicho() -> None:
    """R42, R45: si en el lote dijo su nombre con la pregunta viva y confirmó, se escribe el dicho."""
    plan = _plan(PREGUNTADO, nombre_dicho="Ana Prueba", nombre_confirmado="Ferretería Ejemplo")
    assert plan == Plan(atributos_del_nombre("Ana Prueba"), False)


def test_conocido_con_error_no_escribe_el_nombre() -> None:
    """R41: un turno con error no escribe ni el nombre dicho ni el del pedido."""
    assert _plan(PREGUNTADO, error=True, nombre_dicho="Ana Prueba", nombre_confirmado="Ana Prueba") == NADA
