import inspect
import re
from datetime import date, time

import pytest

from app import respuestas
from app.config import ConfigNegocio, Franja, Horario
from app.pedidos import Pedido
from app.respuestas import (
    AVISO_MATERIAL_A_DEFINIR,
    MENSAJE_ERROR_INTERNO,
    MENSAJE_NO_ENTENDIDO,
    MENSAJE_PEDIDO_RECHAZADO,
    fecha_en_palabras,
    pregunta_por_dato,
    respuesta_faq,
    resumen_pedido,
    texto_derivacion,
)
from app.tools import (
    CAMPOS_DEL_PEDIDO,
    ESTADOS_DISENO,
    MATERIAL_A_DEFINIR,
    MOTIVOS_DERIVACION,
    TEMAS_CONSULTA,
)
from tests.conftest import TELEFONO

# Lo que delataría un precio, un plazo de entrega, una seña o una promesa de inmediatez.
# "precio" y "plazo" no están: aparecen para decir que los da el asesor (ver el test de abajo).
# "hábiles" tampoco: el plazo del presupuesto es un dato de la config (R19 lo permite en el FAQ).
# Con \b: "diseñamos" contiene "seña"
PROHIBIDAS = (
    r"\$", r"\bseñas?\b", r"\banticipo", r"\bdemora", r"\bdías hábiles\b", r"\bentreg", r"\bcuesta\b",
    r"\ben breve\b", r"\benseguida\b", r"¡listo",
)


def _pedido(**cambios: object) -> Pedido:
    campos: dict[str, object] = {
        "telefono": TELEFONO,
        "nombre_cliente": "Ana Prueba",
        "producto": "impresion_digital",
        "material": "cartulina 300g",
        "medidas": "9x5 cm",
        "cantidad": 100,
        "tiene_diseno": "si",
        "fecha_necesita": date(2026, 10, 14),
    }
    return Pedido.model_validate(campos | cambios)


def _con(config: ConfigNegocio, **cambios: object) -> ConfigNegocio:
    return ConfigNegocio.model_validate(config.model_dump() | cambios)


def _todos_los_textos(config: ConfigNegocio) -> list[str]:
    variantes = [config, _con(config, hace_envios=True, tiene_estacionamiento=True)]
    return [
        MENSAJE_NO_ENTENDIDO, MENSAJE_ERROR_INTERNO, MENSAJE_PEDIDO_RECHAZADO, AVISO_MATERIAL_A_DEFINIR,
        *(respuesta_faq(tema, c) for tema in TEMAS_CONSULTA for c in variantes),
        *(pregunta_por_dato(dato, config) for dato in CAMPOS_DEL_PEDIDO),
        *(resumen_pedido(_pedido(tiene_diseno=estado), config) for estado in ESTADOS_DISENO),
        resumen_pedido(_pedido(material=MATERIAL_A_DEFINIR), config),
        *(texto_derivacion(motivo, config) for motivo in MOTIVOS_DERIVACION),
    ]


# FAQ


@pytest.mark.parametrize("tema", TEMAS_CONSULTA)
def test_cada_tema_del_enum_tiene_su_texto(tema: str, config: ConfigNegocio) -> None:
    """R19: cada tema de consulta_general tiene un texto propio, no el de no entendido."""
    assert respuesta_faq(tema, config) != MENSAJE_NO_ENTENDIDO


def test_los_temas_tienen_textos_distintos(config: ConfigNegocio) -> None:
    textos = {respuesta_faq(tema, config) for tema in TEMAS_CONSULTA}
    assert len(textos) == len(TEMAS_CONSULTA)


def test_tema_fuera_del_enum_es_no_entendido(config: ConfigNegocio) -> None:
    assert respuesta_faq("precios", config) == MENSAJE_NO_ENTENDIDO


def test_direccion_sale_de_la_config(config: ConfigNegocio) -> None:
    assert respuesta_faq("direccion", config) == "Estamos en Calle Falsa 123. Te esperamos."
    otra = _con(config, direccion="Avenida Siempreviva 742")
    assert respuesta_faq("direccion", otra) == "Estamos en Avenida Siempreviva 742. Te esperamos."


def test_horarios_agrupa_los_dias_y_nombra_los_cerrados_y_feriados(config: ConfigNegocio) -> None:
    """R39: la respuesta de horarios nombra los feriados."""
    assert respuesta_faq("horarios", config) == (
        "Atendemos lunes a viernes de 09:00 a 18:00; sábado de 09:00 a 13:00. "
        "Domingo, feriados y días no laborables cerramos."
    )


def test_horarios_con_dos_franjas_y_dos_dias_cerrados(config: ConfigNegocio) -> None:
    partido = [Franja(abre=time(9), cierra=time(13)), Franja(abre=time(15), cierra=time(19))]
    horario = Horario(
        lunes=partido, martes=partido, miercoles=[Franja(abre=time(9), cierra=time(13))],
        jueves=partido, viernes=partido, sabado=[], domingo=[],
    )
    otra = _con(config, horario=horario.model_dump(), feriados=[])
    assert respuesta_faq("horarios", otra) == (
        "Atendemos lunes a martes de 09:00 a 13:00 y 15:00 a 19:00; miércoles de 09:00 a 13:00; "
        "jueves a viernes de 09:00 a 13:00 y 15:00 a 19:00. Sábado o domingo cerramos."
    )


def test_horarios_abierto_toda_la_semana_igual_nombra_los_feriados(config: ConfigNegocio) -> None:
    """R39: un negocio que abre los siete días cierra igual sus feriados."""
    franja = [{"abre": "10:00", "cierra": "20:00"}]
    otra = _con(config, horario={dia: franja for dia in Horario.model_fields})
    assert respuesta_faq("horarios", otra) == (
        "Atendemos lunes a domingo de 10:00 a 20:00. Feriados y días no laborables cerramos."
    )


def test_envios_y_estacionamiento_segun_la_config(config: ConfigNegocio) -> None:
    assert respuesta_faq("envios", config) == (
        "No hacemos envíos: el trabajo se retira por el local, en Calle Falsa 123."
    )
    assert respuesta_faq("estacionamiento", config) == "No tenemos estacionamiento propio."
    otra = _con(config, hace_envios=True, tiene_estacionamiento=True)
    assert respuesta_faq("envios", otra) == "Sí, hacemos envíos a domicilio. Consultanos por tu zona."
    assert respuesta_faq("estacionamiento", otra) == "Sí, tenés lugar para estacionar cuando venís."


def test_presupuestos_pagos_y_archivos_salen_de_la_config(config: ConfigNegocio) -> None:
    assert respuesta_faq("presupuestos", config) == (
        "Los presupuestos te los pasamos dentro de las 24 horas hábiles."
    )
    assert respuesta_faq("medios_pago", config) == "Podés pagar con efectivo o transferencia."
    assert respuesta_faq("envio_archivos", config) == (
        "Podés mandarme el archivo por acá, como documento, o mandarlo por mail a "
        "archivos@imprenta.example. Lo que te quede más cómodo."
    )
    otra = _con(config, plazo_presupuesto_horas=48, medios_pago=["efectivo", "débito", "crédito"])
    assert "48 horas hábiles" in respuesta_faq("presupuestos", otra)
    assert respuesta_faq("medios_pago", otra) == "Podés pagar con efectivo, débito o crédito."


def test_catalogo_una_linea_por_familia(config: ConfigNegocio) -> None:
    lineas = respuesta_faq("catalogo", config).splitlines()
    assert lineas[0] == "Esto es lo que hacemos:"
    assert lineas[1] == "- Impresión digital: tarjetas, folletos o volantes."
    assert len(lineas) == 1 + len(config.catalogo)


# Repreguntas


@pytest.mark.parametrize("dato", CAMPOS_DEL_PEDIDO)
def test_cada_dato_tiene_su_repregunta(dato: str, config: ConfigNegocio) -> None:
    """R19: cada campo de CAMPOS_DEL_PEDIDO tiene su repregunta armada por Python."""
    texto = pregunta_por_dato(dato, config)
    assert texto != MENSAJE_NO_ENTENDIDO
    assert "?" in texto


def test_las_repreguntas_son_distintas(config: ConfigNegocio) -> None:
    textos = {pregunta_por_dato(dato, config) for dato in CAMPOS_DEL_PEDIDO}
    assert len(textos) == len(CAMPOS_DEL_PEDIDO)


def test_repregunta_del_producto_lista_el_catalogo(config: ConfigNegocio) -> None:
    assert pregunta_por_dato("producto", config) == (
        "¿Qué necesitás? Hacemos Impresión digital, Gran formato, Rotulación, Sellos o Acabados."
    )


def test_repregunta_del_material_ofrece_dejarlo_a_definir(config: ConfigNegocio) -> None:
    """R15: la repregunta del material ofrece la salida 'a definir con el asesor'."""
    assert MATERIAL_A_DEFINIR in pregunta_por_dato("material", config)


def test_dato_fuera_del_enum_es_no_entendido(config: ConfigNegocio) -> None:
    """R13: el teléfono no es un dato que se repregunte."""
    assert pregunta_por_dato("telefono", config) == MENSAJE_NO_ENTENDIDO


# Resumen


def test_resumen_de_un_pedido_completo(config: ConfigNegocio) -> None:
    """R19: el resumen campo por campo, sin precio, plazo de entrega ni seña."""
    assert resumen_pedido(_pedido(), config) == (
        "Te leo el pedido, Ana Prueba:\n"
        "- Trabajo: Impresión digital\n"
        "- Material: cartulina 300g\n"
        "- Medidas: 9x5 cm\n"
        "- Cantidad: 100\n"
        "- Diseño: ya lo tenés\n"
        "- Lo necesitás para el miércoles 14 de octubre\n"
        "¿Está todo bien? Confirmame y se lo paso a un asesor, que te va a pasar el precio y el plazo."
    )


def test_resumen_con_material_a_definir(config: ConfigNegocio) -> None:
    """R15: el material a definir se muestra como tal, sin nombrar ningún material."""
    resumen = resumen_pedido(_pedido(material="A definir con el asesor."), config)
    assert "- Material: a definir con el asesor\n" in resumen


@pytest.mark.parametrize(
    ("estado", "linea"),
    [("si", "ya lo tenés"), ("no", "todavía no lo tenés"), ("requiere_servicio", "lo diseñamos nosotros")],
)
def test_resumen_con_cada_estado_de_diseno(estado: str, linea: str, config: ConfigNegocio) -> None:
    assert f"- Diseño: {linea}\n" in resumen_pedido(_pedido(tiene_diseno=estado), config)


def test_resumen_de_un_pedido_incompleto_lanza(config: ConfigNegocio) -> None:
    """§7: el resumen se muestra solo con el pedido completo; nunca un 'Material: None'."""
    with pytest.raises(ValueError):
        resumen_pedido(_pedido(material=None), config)


@pytest.mark.parametrize(
    ("fecha", "texto"),
    [
        (date(2026, 10, 10), "sábado 10 de octubre"),
        (date(2026, 10, 14), "miércoles 14 de octubre"),
        (date(2027, 1, 4), "lunes 4 de enero"),
        (date(2026, 12, 31), "jueves 31 de diciembre"),
    ],
)
def test_fecha_en_castellano(fecha: date, texto: str) -> None:
    """R40: días y meses en castellano y con tilde, sin año."""
    assert fecha_en_palabras(fecha) == texto


def test_fechas_sin_locale() -> None:
    """R40: días y meses del diccionario de formato, nunca de locale ni de strftime."""
    fuente = inspect.getsource(respuestas)
    assert "locale" not in fuente
    assert not re.search(r"%[aAbBc]", fuente)


# Textos fijos y derivación


def test_rechazo_vuelve_a_recolectar_sin_listo() -> None:
    """R2: rechazar el resumen nunca suena a pedido guardado."""
    assert "listo" not in MENSAJE_PEDIDO_RECHAZADO.casefold()
    assert "cambiar" in MENSAJE_PEDIDO_RECHAZADO


def test_aviso_de_material_no_nombra_materiales() -> None:
    """R15: el aviso dice por qué queda a definir, sin ofrecer ningún material."""
    assert AVISO_MATERIAL_A_DEFINIR == (
        "El material lo confirma un asesor cuando cotiza, así que lo dejamos a definir."
    )


@pytest.mark.parametrize("motivo", MOTIVOS_DERIVACION)
def test_cada_motivo_tiene_su_texto_con_asesor(motivo: str, config: ConfigNegocio) -> None:
    """R19: cada motivo de derivar_a_asesor tiene su texto y nombra al asesor."""
    assert "asesor" in texto_derivacion(motivo, config)


def test_los_motivos_tienen_textos_distintos(config: ConfigNegocio) -> None:
    textos = {texto_derivacion(motivo, config) for motivo in MOTIVOS_DERIVACION}
    assert len(textos) == len(MOTIVOS_DERIVACION)


def test_sin_stock_no_ofrece_alternativas(config: ConfigNegocio) -> None:
    """R15: con sin_stock nunca se ofrecen alternativas, ni de material ni del catálogo."""
    texto = texto_derivacion("sin_stock", config).casefold()
    for ofrecimiento in ("otro", "otra", "alternativ", "en su lugar", "en cambio", "podemos", "tenemos", "?"):
        assert ofrecimiento not in texto
    for producto in config.catalogo:
        for nombre in (producto.familia, *producto.ejemplos):
            assert nombre.casefold() not in texto


# Todos los textos


def test_ningun_texto_da_precio_plazo_ni_sena(config: ConfigNegocio) -> None:
    """R19: ningún texto da precio, plazo de entrega ni seña, ni promete inmediatez (R38)."""
    for texto in _todos_los_textos(config):
        for prohibida in PROHIBIDAS:
            assert not re.search(prohibida, texto.casefold()), (prohibida, texto)


@pytest.mark.parametrize(
    "malo",
    ["Sale $5000", "La seña es del 50 %", "Dejás un anticipo", "Demora 3 días", "En 5 días hábiles",
     "Te lo entregamos el jueves", "Cuesta poco", "Te contestan en breve", "Enseguida te llaman",
     "¡Listo! Ya está"],
)
def test_la_lista_de_prohibidas_detecta_cada_caso(malo: str) -> None:
    """R19: cada patrón de PROHIBIDAS atrapa el texto que quiere impedir."""
    assert any(re.search(patron, malo.casefold()) for patron in PROHIBIDAS)


def test_precio_y_plazo_solo_para_decir_que_los_da_el_asesor(config: ConfigNegocio) -> None:
    """R19: 'precio' o 'plazo' aparecen solo en un texto que los deja en manos del asesor."""
    for texto in _todos_los_textos(config):
        if "precio" in texto.casefold() or "plazo" in texto.casefold():
            assert "asesor" in texto, texto


def test_todos_los_textos_tutean(config: ConfigNegocio) -> None:
    """R19: tuteo siempre, nunca 'usted'."""
    for texto in _todos_los_textos(config):
        assert not re.search(r"\busted\b", texto, re.IGNORECASE), texto
