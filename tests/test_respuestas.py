import inspect
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import respuestas
from app.config import ConfigNegocio, Franja, Horario
from app.pedidos import ArchivoAdjunto, Pedido, texto_de_archivos
from app.respuestas import (
    AVISO_MATERIAL_A_DEFINIR,
    MENSAJE_CAMBIO_DURANTE_LA_CONFIRMACION,
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
    fecha_en_palabras,
    nota_de_archivos,
    nota_de_derivacion,
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
from tests.conftest import HORA_DE_PRUEBA, TELEFONO

# Lo que delataría un precio, un plazo de entrega, una seña o una promesa de inmediatez.
# "precio" y "plazo" no están: aparecen para decir que los da el asesor (ver el test de abajo).
# "hábiles" tampoco: el plazo del presupuesto es un dato de la config (R19 lo permite en el FAQ).
# Con \b: "diseñamos" contiene "seña". "ya" suelto tampoco: "ya lo tenés" o "Ya le paso tu consulta"
# no prometen nada; lo prohibido es que una persona contesta ya (R38)
_CONTACTO = r"(contest|contact|atiend|llam|escrib|respond)"
PROHIBIDAS = (
    r"\$", r"\bseñas?\b", r"\banticipo", r"\bdemora", r"\bdías hábiles\b", r"\bentreg", r"\bcuesta\b",
    r"\ben breve\b", r"\benseguida\b", r"¡listo", rf"\bya te {_CONTACTO}", rf"\b{_CONTACTO}\w* ya\b",
)
ZONA = ZoneInfo("America/Argentina/Buenos_Aires")
MOTIVOS_DE_LA_NOTA = (*MOTIVOS_DERIVACION, "cambio_sobre_pedido_confirmado")  # R6


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


def _sin_aperturas(config: ConfigNegocio) -> ConfigNegocio:
    """Feriados que tapan hoy y los 31 días que siguen a HORA_DE_PRUEBA: proxima_apertura da None."""
    return _con(config, feriados=[HORA_DE_PRUEBA.date() + timedelta(days=dia) for dia in range(32)])


def _con_horario(config: ConfigNegocio) -> list[str]:
    """La frase de horario detrás de cada texto que deriva: en horario, cerrado y sin apertura a la vista."""
    cerrado = HORA_DE_PRUEBA.replace(hour=20)
    textos = [*(texto_derivacion(motivo, config) for motivo in MOTIVOS_DERIVACION),
              MENSAJE_CAMBIO_DURANTE_LA_CONFIRMACION]
    return [
        con_frase_de_horario(texto, c, ahora)
        for texto in textos
        for c, ahora in ((config, HORA_DE_PRUEBA), (config, cerrado), (_sin_aperturas(config), cerrado))
    ]


def _todos_los_textos(config: ConfigNegocio) -> list[str]:
    """Todos menos MENSAJE_PEDIDO_CONFIRMADO, que tiene su propio test contra PROHIBIDAS."""
    variantes = [config, _con(config, hace_envios=True, tiene_estacionamiento=True)]
    return [
        *_con_horario(config),
        con_pregunta_del_nombre(respuesta_faq("direccion", config)),
        MENSAJE_NO_ENTENDIDO, MENSAJE_ERROR_INTERNO, MENSAJE_PEDIDO_RECHAZADO, AVISO_MATERIAL_A_DEFINIR,
        MENSAJE_ERROR_AL_GUARDAR, MENSAJE_CAMBIO_DURANTE_LA_CONFIRMACION, MENSAJE_TIPO_NO_SOPORTADO,
        *(acuse_de_archivos(n, p, config) for n in (1, 3) for p in (_pedido(), _pedido(medidas=None))),
        *(acuse_de_archivos_despues_de_confirmar(n) for n in (1, 3)),
        *(respuesta_faq(tema, c) for tema in TEMAS_CONSULTA for c in variantes),
        *(pregunta_por_dato(dato, config) for dato in CAMPOS_DEL_PEDIDO),
        *(resumen_pedido(_pedido(tiene_diseno=estado), config) for estado in ESTADOS_DISENO),
        resumen_pedido(_pedido(material=MATERIAL_A_DEFINIR), config),
        *(texto_derivacion(motivo, config) for motivo in MOTIVOS_DERIVACION),
        *filter(None, (
            aviso_de_descartados([campo], _pedido(**{campo: None}), config)
            for campo in CAMPOS_DEL_PEDIDO
        )),
        con_aviso_de_material(pregunta_por_dato("medidas", config), es_resumen=False),
        con_aviso_de_material(resumen_pedido(_pedido(material=MATERIAL_A_DEFINIR), config), es_resumen=True),
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


# Campos descartados y aviso de material


def test_producto_fuera_del_catalogo_lista_las_familias(config: ConfigNegocio) -> None:
    """R14: el producto descartado se avisa con el catálogo y se repregunta en el mismo texto."""
    assert aviso_de_descartados(["producto"], _pedido(producto=None), config) == (
        "Eso no lo tengo en la lista. Hacemos Impresión digital, Gran formato, Rotulación, Sellos o "
        "Acabados. ¿Cuál de esos necesitás?"
    )


def test_producto_fuera_del_catalogo_se_avisa_aunque_el_pedido_tenga_otro(config: ConfigNegocio) -> None:
    """R14: el cliente pidió algo que no está; callarlo dejaría el pedido con el producto anterior."""
    assert aviso_de_descartados(["producto"], _pedido(), config) is not None


def test_fecha_pasada_se_repregunta(config: ConfigNegocio) -> None:
    """R14: la fecha pasada se descarta y se repregunta."""
    assert aviso_de_descartados(["fecha_necesita"], _pedido(fecha_necesita=None), config) == (
        "Esa fecha ya pasó, así que algo entendí mal. ¿Para qué día lo necesitás?"
    )


def test_fecha_pasada_con_una_valida_de_antes_sigue_la_charla(config: ConfigNegocio) -> None:
    """R14: si el pedido conserva una fecha válida, el reenvío vencido no se repregunta."""
    assert aviso_de_descartados(["fecha_necesita"], _pedido(), config) is None


def test_producto_gana_sobre_la_fecha(config: ConfigNegocio) -> None:
    """R14: con dos descartados sale un solo aviso, el del producto."""
    descartados = ["fecha_necesita", "producto"]
    pedido = _pedido(producto=None, fecha_necesita=None)
    assert aviso_de_descartados(descartados, pedido, config) == aviso_de_descartados(
        ["producto"], pedido, config
    )


@pytest.mark.parametrize("descartados", [[], ["nombre_cliente"], ["cantidad"], ["telefono"]])
def test_sin_aviso_sigue_el_flujo_normal(descartados: list[str], config: ConfigNegocio) -> None:
    """R14: el nombre descartado no se avisa; el pedido queda sin él y la repregunta normal lo pide."""
    assert aviso_de_descartados(descartados, _pedido(nombre_cliente=None), config) is None


def test_aviso_de_material_antes_de_la_repregunta_con_un_espacio() -> None:
    """R15: el aviso va delante de la repregunta, separado por un espacio."""
    assert con_aviso_de_material("¿Qué medidas necesitás?", es_resumen=False) == (
        "El material lo confirma un asesor cuando cotiza, así que lo dejamos a definir. "
        "¿Qué medidas necesitás?"
    )


def test_aviso_de_material_antes_del_resumen_con_una_linea_en_blanco(config: ConfigNegocio) -> None:
    """R15: el aviso va delante del resumen, separado por una línea en blanco."""
    resumen = resumen_pedido(_pedido(material=MATERIAL_A_DEFINIR), config)
    assert con_aviso_de_material(resumen, es_resumen=True) == f"{AVISO_MATERIAL_A_DEFINIR}\n\n{resumen}"


def test_los_avisos_de_descartados_no_ofrecen_alternativas_de_material(config: ConfigNegocio) -> None:
    """R15: ningún aviso de R14 nombra un material ni ofrece uno en su lugar."""
    for campo in ("producto", "fecha_necesita"):
        texto = aviso_de_descartados([campo], _pedido(**{campo: None}), config) or ""
        for ofrecimiento in ("material", "otro", "otra", "alternativ", "en su lugar", "en cambio"):
            assert ofrecimiento not in texto.casefold()


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


# Confirmación y archivos


def test_confirmacion_literal() -> None:
    """R2: el único texto con "¡Listo!", literal del bot anterior."""
    assert MENSAJE_PEDIDO_CONFIRMADO == (
        "¡Listo! Ya guardamos tus datos. Un asesor se contactará para confirmar el pago y la entrega."
    )


def test_confirmacion_nombra_la_entrega_solo_para_dejarla_al_asesor() -> None:
    """R19: de PROHIBIDAS, la confirmación solo tiene "¡listo" y "entreg", y la entrega la ve el asesor (R38)."""
    texto = MENSAJE_PEDIDO_CONFIRMADO.casefold()
    assert [patron for patron in PROHIBIDAS if re.search(patron, texto)] == [r"\bentreg", r"¡listo"]
    assert "un asesor se contactará para confirmar el pago y la entrega" in texto


def test_error_al_guardar_no_confirma_e_invita_a_reintentar() -> None:
    """R2: con la planilla caída el cliente lee que no se guardó y que vuelva a escribir."""
    assert MENSAJE_ERROR_AL_GUARDAR == (
        "Tengo todos tus datos pero no los pude guardar bien. Escribime de nuevo en unos minutos así no "
        "se pierde nada."
    )


def test_acuse_de_un_archivo_con_el_pedido_incompleto(config: ConfigNegocio) -> None:
    """R30: un archivo, un acuse que sigue con los datos que faltan."""
    assert acuse_de_archivos(1, _pedido(medidas=None), config) == (
        "¡Recibí tu archivo! Ya queda guardado con tu pedido y un asesor lo va a revisar. "
        "¿Seguimos con los datos?"
    )


def test_acuse_de_una_rafaga_es_uno_con_la_cantidad(config: ConfigNegocio) -> None:
    """R30: tres archivos seguidos, un solo acuse que dice cuántos llegaron."""
    texto = acuse_de_archivos(3, _pedido(medidas=None), config)
    assert texto == (
        "¡Recibí tus 3 archivos! Ya quedan guardados con tu pedido y un asesor los va a revisar. "
        "¿Seguimos con los datos?"
    )


def test_acuse_con_el_pedido_completo_sigue_con_el_resumen(config: ConfigNegocio) -> None:
    """R31: si el archivo completó el pedido, al acuse lo sigue el resumen, no "¿seguimos?"."""
    pedido = _pedido()
    assert acuse_de_archivos(1, pedido, config) == (
        "¡Recibí tu archivo! Ya queda guardado con tu pedido y un asesor lo va a revisar.\n\n"
        f"{resumen_pedido(pedido, config)}"
    )


def test_acuse_sin_archivos_lanza(config: ConfigNegocio) -> None:
    """R30: un acuse es por al menos un archivo; nunca "Recibí tus 0 archivos"."""
    with pytest.raises(ValueError):
        acuse_de_archivos(0, _pedido(), config)
    with pytest.raises(ValueError):
        acuse_de_archivos_despues_de_confirmar(0)


@pytest.mark.parametrize(
    ("recibidos", "texto"),
    [
        (1, "¡Recibí tu archivo! Tu pedido ya estaba confirmado, así que se lo paso al asesor para que lo sume."),
        (3, "¡Recibí tus 3 archivos! Tu pedido ya estaba confirmado, así que se los paso al asesor para que "
            "los sume."),
    ],
)
def test_acuse_despues_de_confirmar_no_dice_guardado(recibidos: int, texto: str) -> None:
    """R32: el archivo no toca la fila: el acuse no dice "guardado" y se lo pasa al asesor."""
    assert acuse_de_archivos_despues_de_confirmar(recibidos) == texto


def test_cambio_durante_la_confirmacion() -> None:
    """R6: un cambio sobre el pedido que se estaba confirmando lo ve un asesor."""
    assert MENSAJE_CAMBIO_DURANTE_LA_CONFIRMACION == (
        "Ese pedido ya lo estaba confirmando, así que un cambio lo tiene que ver un asesor con vos."
    )


def test_tipo_no_soportado_tiene_respuesta_fija() -> None:
    """R36: audio, sticker o ubicación reciben una respuesta fija que ofrece escribir o mandar documento."""
    assert MENSAJE_TIPO_NO_SOPORTADO == (
        "Por ahora solo puedo leer mensajes de texto. Si querés, escribime lo que necesitás y seguimos, "
        "o mandame el diseño como documento."
    )


# Frase de horario


_REABRE = " Ya le dejo tu consulta, y te va a contestar cuando volvamos a abrir"


def test_en_horario_le_contesta_un_asesor_por_aca(config: ConfigNegocio) -> None:
    """R38: en horario, detrás del texto va que un asesor le contesta por acá; sin "en breve"."""
    config.fijar_ahora(HORA_DE_PRUEBA)
    assert con_frase_de_horario(MENSAJE_CAMBIO_DURANTE_LA_CONFIRMACION, config, config.ahora()) == (
        "Ese pedido ya lo estaba confirmando, así que un cambio lo tiene que ver un asesor con vos. "
        "Ya le paso tu consulta así te contesta por acá."
    )


@pytest.mark.parametrize(
    ("ahora", "cuando"),
    [
        (datetime(2026, 10, 6, 7, 30, tzinfo=ZONA), "hoy desde las 09:00"),
        (datetime(2026, 10, 6, 18, 0, tzinfo=ZONA), "mañana desde las 09:00"),
        (datetime(2026, 10, 3, 14, 0, tzinfo=ZONA), "el lunes 5 de octubre desde las 09:00"),
    ],
    ids=["antes-de-abrir", "al-cerrar", "sabado-a-la-tarde"],
)
def test_fuera_de_horario_dice_cuando_reabre(ahora: datetime, cuando: str, config: ConfigNegocio) -> None:
    """R38: fuera de horario dice cuándo reabre, con la config: hoy, mañana o el día con la fecha."""
    texto = texto_derivacion("lo_pide_el_cliente", config)
    assert con_frase_de_horario(texto, config, ahora) == f"{texto}{_REABRE}: {cuando}."


@pytest.mark.parametrize(
    ("ahora", "cuando"),
    [
        (datetime(2026, 10, 11, 10, 0, tzinfo=ZONA), "el martes 13 de octubre desde las 09:00"),
        (datetime(2026, 10, 12, 10, 0, tzinfo=ZONA), "mañana desde las 09:00"),
    ],
    ids=["vispera", "el-feriado"],
)
def test_la_reapertura_saltea_el_feriado(ahora: datetime, cuando: str, config: ConfigNegocio) -> None:
    """R38, R39: el lunes 12 de octubre es feriado; la reapertura de la víspera cae el martes."""
    texto = texto_derivacion("sin_stock", config)
    assert con_frase_de_horario(texto, config, ahora) == f"{texto}{_REABRE}: {cuando}."


def test_hoy_y_manana_son_los_de_la_zona_del_negocio(config: ConfigNegocio) -> None:
    """R37, R38: el martes 22:00 en el negocio ya es miércoles en UTC; la reapertura es "mañana", no "hoy"."""
    ahora = datetime(2026, 10, 7, 1, 0, tzinfo=ZoneInfo("UTC"))
    texto = texto_derivacion("fuera_de_alcance", config)
    assert con_frase_de_horario(texto, config, ahora) == f"{texto}{_REABRE}: mañana desde las 09:00."


def test_sin_apertura_a_la_vista_cierra_sin_fecha(config: ConfigNegocio) -> None:
    """R38, R39: si proxima_apertura da None, la oración cierra sin fecha y sin dos puntos colgando."""
    texto = texto_derivacion("plazo_o_precio", config)
    ahora = HORA_DE_PRUEBA.replace(hour=20)
    assert con_frase_de_horario(texto, _sin_aperturas(config), ahora) == f"{texto}{_REABRE}."


def test_frase_de_horario_sin_zona_lanza(config: ConfigNegocio) -> None:
    """R37: una hora sin zona se leería como la del servidor."""
    with pytest.raises(ValueError):
        con_frase_de_horario("Hola.", config, datetime(2026, 10, 6, 10, 0))


# Notas al asesor


def test_nota_de_derivacion_con_pedido_y_nombre(config: ConfigNegocio) -> None:
    """R47: nombre, motivo en palabras y el pedido con las etiquetas del resumen."""
    assert nota_de_derivacion("lo_pide_el_cliente", _pedido(), "Ana", config) == (
        "Derivación del bot: Pidió hablar con una persona.\n"
        "Nombre: Ana\n"
        "Así entendió el bot el pedido:\n"
        "- A nombre de: Ana Prueba\n"
        "- Trabajo: Impresión digital\n"
        "- Material: cartulina 300g\n"
        "- Medidas: 9x5 cm\n"
        "- Cantidad: 100\n"
        "- Diseño: lo tiene\n"
        "- Lo necesita para el miércoles 14 de octubre"
    )


@pytest.mark.parametrize("nombre", [None, "", "  \n "])
def test_nota_sin_nombre_lo_marca(nombre: str | None, config: ConfigNegocio) -> None:
    """R47: sin nombre registrado, la nota lo dice; nunca un "Nombre: " vacío."""
    nota = nota_de_derivacion("sin_stock", None, nombre, config)
    assert nota.splitlines()[1] == "Nombre: sin nombre registrado"


def test_nota_sin_pedido_no_lleva_bloque_de_pedido(config: ConfigNegocio) -> None:
    """R47: sin pedido, solo el motivo y el nombre."""
    assert nota_de_derivacion("plazo_o_precio", None, "Ana", config) == (
        "Derivación del bot: Pregunta por precio o plazo.\nNombre: Ana"
    )
    vacio = Pedido(telefono=TELEFONO)
    assert nota_de_derivacion("plazo_o_precio", vacio, "Ana", config) == (
        "Derivación del bot: Pregunta por precio o plazo.\nNombre: Ana"
    )


def test_nota_con_pedido_a_medias_no_muestra_campos_vacios(config: ConfigNegocio) -> None:
    """R47: solo los campos que el bot entendió, sin "None" ni etiquetas vacías."""
    pedido = Pedido(telefono=TELEFONO, producto="gran_formato", cantidad=50, material=MATERIAL_A_DEFINIR)
    assert nota_de_derivacion("fuera_de_alcance", pedido, None, config).splitlines()[2:] == [
        "Así entendió el bot el pedido:",
        "- Trabajo: Gran formato",
        "- Material: a definir con el asesor",
        "- Cantidad: 50",
    ]


@pytest.mark.parametrize("estado", ESTADOS_DISENO)
def test_nota_sin_claves_internas(estado: str, config: ConfigNegocio) -> None:
    """R6, R47: la nota nunca muestra nombres internos, ids del catálogo, valores del enum ni el teléfono."""
    nota = nota_de_derivacion("cambio_sobre_pedido_confirmado", _pedido(tiene_diseno=estado), "Ana", config)
    internos = (*CAMPOS_DEL_PEDIDO, "requiere_servicio", "telefono", "impresion_digital", "None", TELEFONO)
    for interno in internos:
        assert interno not in nota, interno


def test_cada_motivo_tiene_su_frase_en_la_nota(config: ConfigNegocio) -> None:
    """R6, R47: los motivos de derivar y el cambio durante la confirmación, cada uno con su frase."""
    notas = [nota_de_derivacion(motivo, None, None, config) for motivo in MOTIVOS_DE_LA_NOTA]
    primeras = {nota.splitlines()[0] for nota in notas}
    assert len(primeras) == len(MOTIVOS_DE_LA_NOTA)
    assert all("_" not in linea for linea in primeras)


def test_nota_de_derivacion_limpia_lo_que_escribio_el_cliente(config: ConfigNegocio) -> None:
    """R47: sin bidi ni invisibles, ni links ni menciones que Chatwoot interprete en la nota privada."""
    pedido = _pedido(
        nombre_cliente="Ana‮abanA", material="lona [x](https://example.com)", medidas="MENTION://team/1/x 2x1"
    )
    nota = nota_de_derivacion("lo_pide_el_cliente", pedido, "Ana​‮abanA\x07", config)
    assert nota.splitlines()[1] == "Nombre: AnaabanA"
    assert "- A nombre de: AnaabanA" in nota
    assert "- Material: lona xhttps://example.com" in nota
    assert "- Medidas: team/1/x 2x1" in nota


@pytest.mark.parametrize("valor", ["mention://", "menmention://tion://", "mentioMention://n://","[]()<>‮"])
def test_nota_de_derivacion_no_rearma_una_mencion_al_limpiar(valor: str, config: ConfigNegocio) -> None:
    """R47: sacar un "mention://" no deja otro armado con lo que quedó a los costados."""
    nota = nota_de_derivacion("sin_stock", _pedido(medidas=f"9x5 {valor}"), valor, config)
    assert "mention://" not in nota.casefold()
    assert not set("[]()<>‮") & set(nota)
    assert nota.splitlines()[1] == "Nombre: sin nombre registrado"


def _archivo(id_adjunto: int, minuto: int, tipo: str = "pdf") -> ArchivoAdjunto:
    return ArchivoAdjunto(
        id_adjunto=id_adjunto, id_mensaje=1, tipo=tipo, tamano=None, hora=datetime(2026, 10, 6, 10, minuto)
    )


def test_nota_de_un_archivo_despues_de_confirmar() -> None:
    """R32: la nota lleva la línea de la celda para que la persona sume el archivo al pedido."""
    lineas = texto_de_archivos(Pedido(telefono=TELEFONO, archivos=[_archivo(123, 5)]))
    assert nota_de_archivos(lineas) == (
        "Archivo que llegó después de confirmar el pedido.\n1. 06/10 10:05 · pdf · adjunto #123"
    )


def test_nota_de_varios_archivos_en_plural() -> None:
    """R32, R33: con varios, en plural y una línea por archivo, en el orden de la celda."""
    lineas = texto_de_archivos(Pedido(telefono=TELEFONO, archivos=[_archivo(123, 5), _archivo(124, 7)]))
    assert nota_de_archivos(lineas) == (
        "Archivos que llegaron después de confirmar el pedido.\n"
        "1. 06/10 10:05 · pdf · adjunto #123\n2. 06/10 10:07 · pdf · adjunto #124"
    )


def test_nota_de_archivos_limpia_la_extension_que_puso_el_cliente() -> None:
    """R47: la extensión viene del nombre del archivo; sin mención ni link, y una línea por archivo."""
    archivos = [_archivo(123, 5, "Mention://team/1/x"), _archivo(124, 7, "[p](‮)pdf")]
    assert nota_de_archivos(texto_de_archivos(Pedido(telefono=TELEFONO, archivos=archivos))) == (
        "Archivos que llegaron después de confirmar el pedido.\n"
        "1. 06/10 10:05 · team/1/x · adjunto #123\n2. 06/10 10:07 · ppdf · adjunto #124"
    )


def test_nota_de_archivos_sin_archivos_lanza() -> None:
    """R32: una nota de archivos es por al menos uno."""
    with pytest.raises(ValueError):
        nota_de_archivos("")


def test_las_notas_no_tienen_palabras_prohibidas(config: ConfigNegocio) -> None:
    """R19, R38: las notas tampoco prometen inmediatez ni ponen precio, plazo ni seña."""
    notas = [
        *(nota_de_derivacion(motivo, pedido, nombre, config)
          for motivo in MOTIVOS_DE_LA_NOTA for pedido in (None, _pedido()) for nombre in (None, "Ana")),
        nota_de_archivos(texto_de_archivos(Pedido(telefono=TELEFONO, archivos=[_archivo(1, 0)]))),
    ]
    for nota in notas:
        for prohibida in PROHIBIDAS:
            assert not re.search(prohibida, nota.casefold()), (prohibida, nota)


# Pregunta del nombre


def test_pregunta_del_nombre_debajo_con_el_separador(config: ConfigNegocio) -> None:
    """R44: la pregunta va debajo de la respuesta elegida, separada por una línea en blanco."""
    assert con_pregunta_del_nombre(respuesta_faq("direccion", config)) == (
        "Estamos en Calle Falsa 123. Te esperamos.\n\n¿Cómo es tu nombre? Así te agendamos."
    )


@pytest.mark.parametrize("vacio", ["", "  \n "])
def test_pregunta_del_nombre_nunca_va_sola(vacio: str) -> None:
    """R44: nunca sobre una respuesta vacía."""
    with pytest.raises(ValueError):
        con_pregunta_del_nombre(vacio)


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
     "¡Listo! Ya está", "Ya te contesta un asesor", "Te llaman ya"],
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
