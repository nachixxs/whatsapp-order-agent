from datetime import date, datetime, timedelta

import pytest
from pydantic import BaseModel

from app.config import ConfigNegocio
from app.prompt import NOMBRE_SIN_PERFIL, bloque_dinamico, bloque_estatico, bloques_de_sistema
from app.tools import MATERIAL_A_DEFINIR


class PedidoFalso(BaseModel):
    """Doble del Pedido de app/pedidos.py: solo lo que lee el prompt."""

    producto: str | None = None
    material: str | None = None
    medidas: str | None = None
    cantidad: int | None = None
    fecha_necesita: date | None = None
    tiene_diseno: str | None = None
    nombre_cliente: str | None = None
    archivos: list[str] = []


COMPLETO = PedidoFalso(
    producto="sellos",
    material="goma",
    medidas="3x2 cm",
    cantidad=3,
    fecha_necesita=date(2026, 10, 9),
    tiene_diseno="si",
    nombre_cliente="Ana Prueba",
)


def _dinamico(config: ConfigNegocio, **cambios: object) -> str:
    argumentos: dict[str, object] = {"nombre_perfil": "Ana Prueba", "pedido": None} | cambios
    return bloque_dinamico(config, config.ahora(), **argumentos)  # type: ignore[arg-type]


def test_el_estatico_sale_igual_byte_a_byte(config: ConfigNegocio) -> None:
    """R18: el bloque estático no cambia entre requests aunque cambien la hora, el cliente y el pedido."""
    primero = bloques_de_sistema(config, config.ahora(), nombre_perfil="Ana Prueba", pedido=None)
    config.fijar_ahora(config.ahora() + timedelta(days=6, hours=9))  # otro día, un feriado, de noche
    segundo = bloques_de_sistema(
        config, config.ahora(), nombre_perfil=None, pedido=COMPLETO, nombre_preguntado=True, confirmado=COMPLETO
    )
    assert primero[0] == segundo[0]
    assert primero[0]["text"].encode("utf-8") == segundo[0]["text"].encode("utf-8")
    assert primero[1]["text"] != segundo[1]["text"]


def test_el_estatico_no_trae_nada_del_turno(config: ConfigNegocio) -> None:
    """R18: el estático no depende de la hora: fecha, cliente y pedido van solo en el dinámico."""
    estatico = bloque_estatico(config)
    assert "2026" not in estatico
    assert "Hoy es" not in estatico
    assert "PEDIDO EN CURSO" not in estatico.replace("seguí lo que dice PEDIDO EN CURSO", "")


def test_cache_control_solo_en_el_estatico(config: ConfigNegocio) -> None:
    """R18: un solo breakpoint de caché, en el bloque estático; el dinámico va después."""
    estatico, dinamico = bloques_de_sistema(config, config.ahora(), nombre_perfil=None, pedido=None)
    assert estatico["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in dinamico
    assert estatico["type"] == dinamico["type"] == "text"
    assert dinamico["text"].startswith("\n\nHoy es ")


def test_el_dinamico_cambia_con_la_fecha(config: ConfigNegocio) -> None:
    """R18: la fecha se arma en cada request con el reloj del negocio (R37)."""
    martes = _dinamico(config)
    assert martes.startswith("\n\nHoy es martes 6 de octubre de 2026 (2026-10-06).")
    config.fijar_ahora(config.ahora() + timedelta(days=1))
    assert _dinamico(config).startswith("\n\nHoy es miércoles 7 de octubre de 2026 (2026-10-07).")


def test_la_fecha_es_la_del_negocio_aunque_llegue_en_utc(config: ConfigNegocio) -> None:
    """R37: un instante en UTC se lee en la zona del negocio (01:00 UTC del 7 es el 6 en Buenos Aires)."""
    utc = datetime.fromisoformat("2026-10-07T01:00:00+00:00")
    texto = bloque_dinamico(config, utc, nombre_perfil=None, pedido=None)
    assert "(2026-10-06)" in texto


def test_hora_sin_zona_se_rechaza(config: ConfigNegocio) -> None:
    """R37: una hora sin zona se leería como la del servidor."""
    with pytest.raises(ValueError):
        bloque_dinamico(config, datetime(2026, 10, 6, 10), nombre_perfil=None, pedido=None)


def test_la_linea_de_cerrado_va_solo_el_feriado(config: ConfigNegocio) -> None:
    """R39: "hoy el local está cerrado" entra al prompt solo el día del feriado."""
    cerrado = "Hoy el local está cerrado todo el día"
    assert cerrado not in _dinamico(config)
    config.fijar_ahora(config.ahora().replace(day=12))  # 12 de octubre, feriado en la config
    assert cerrado in _dinamico(config)
    config.fijar_ahora(config.ahora().replace(day=13))
    assert cerrado not in _dinamico(config)


def test_el_prompt_pide_exactamente_una_tool(config: ConfigNegocio) -> None:
    """R11: cada mensaje termina en exactamente una de las cinco tools, nunca en texto."""
    estatico = bloque_estatico(config)
    assert "Cada mensaje del cliente termina en exactamente una llamada a una de las cinco tools" in estatico
    assert "nunca en texto" in estatico
    assert "y a una sola" in estatico


def test_el_estatico_sale_de_la_config(config: ConfigNegocio) -> None:
    """R17: los datos del negocio salen de la config, no del texto copiado."""
    estatico = bloque_estatico(config)
    assert estatico.startswith("Sos el asistente de WhatsApp de Imprenta Ejemplo, una imprenta.")
    assert "- Dirección: Calle Falsa 123" in estatico
    assert "- sábado: 09:00 a 13:00\n- domingo: cerrado" in estatico
    assert "- Envíos a domicilio: no, se retira por el local" in estatico
    assert "- Estacionamiento: no" in estatico
    assert "- Presupuestos: dentro de las 24 horas hábiles" in estatico
    assert "- Métodos de pago: efectivo o transferencia" in estatico
    assert "- id `gran_formato`: Gran formato — lonas, banners o planos" in estatico
    for producto in config.catalogo:
        assert f"- id `{producto.id}`: {producto.familia}" in estatico


def test_el_estatico_cambia_si_cambia_la_config(config: ConfigNegocio) -> None:
    """R17: otra config da otro prompt; el texto no tiene datos del negocio fijos."""
    otra = config.model_copy(update={"nombre": "Otra Imprenta", "hace_envios": True})
    estatico = bloque_estatico(otra)
    assert "de Otra Imprenta, una imprenta" in estatico
    assert "- Envíos a domicilio: sí" in estatico


def test_las_clausulas_van_en_el_orden_medido(config: ConfigNegocio) -> None:
    """R17: el orden de las cláusulas es parte del artefacto medido."""
    estatico = bloque_estatico(config)
    secciones = ["DATOS DE LA IMPRENTA", "CATÁLOGO", "QUÉ HACER CON CADA MENSAJE", "REGLAS QUE NO SE NEGOCIAN",
                 "EL DISEÑO TIENE TRES ESTADOS, NO DOS"]
    posiciones = [estatico.index(f"\n{seccion}\n") for seccion in secciones]
    assert posiciones == sorted(posiciones)
    reglas = [estatico.index(f"\n{numero}. ") for numero in range(1, 8)]
    assert reglas == sorted(reglas)


def test_las_reglas_duras_estan_en_el_prompt(config: ConfigNegocio) -> None:
    """R19: nunca precio, plazo, seña ni alternativas; R38: nunca "en breve"."""
    estatico = bloque_estatico(config)
    assert "**Nunca des un precio ni un presupuesto**" in estatico
    assert "**Nunca prometas una fecha de entrega**" in estatico
    assert "**Nunca definas la seña ni el anticipo.**" in estatico
    assert "no ofrezcas alternativas.** Ni una." in estatico
    assert '"ya", "enseguida" o "en breve"' in estatico
    assert "Tuteá siempre." in estatico


def test_el_material_a_definir_y_su_marcador(config: ConfigNegocio) -> None:
    """R15 y R21: la regla 6 usa el valor exacto y el marcador de la repregunta del material."""
    estatico = bloque_estatico(config)
    assert f'`material` igual a "{MATERIAL_A_DEFINIR}"' in estatico
    assert "Si tu último turno fue `[dato_faltante: material]`" in estatico


def test_sin_pedido(config: ConfigNegocio) -> None:
    """R18: sin pedido, el dinámico lo dice; un pedido sin campos cuenta como ninguno."""
    assert _dinamico(config).endswith("PEDIDO EN CURSO\nNo hay ningún pedido en curso.")
    assert _dinamico(config, pedido=PedidoFalso()).endswith("PEDIDO EN CURSO\nNo hay ningún pedido en curso.")


def test_pedido_a_medias_lista_lo_que_falta(config: ConfigNegocio) -> None:
    """R18: el pedido en curso va con lo que dio y lo que falta, en el orden de la repregunta."""
    pedido = PedidoFalso(producto="sellos", cantidad=3, archivos=["a"])
    texto = _dinamico(config, pedido=pedido)
    assert "Datos que el cliente ya dio:\n- producto: sellos\n- cantidad: 3\n" in texto
    assert "Todavía falta: material, medidas, fecha_necesita, tiene_diseno, nombre_cliente." in texto
    assert texto.endswith("Ya mandó el archivo del diseño por WhatsApp.")
    assert "confirmar_pedido` con acepta=true" not in texto


def test_pedido_completo_pide_confirmar(config: ConfigNegocio) -> None:
    """R16: frente al resumen, un 👍 confirma y un "gracias" pelado no; lo dice el prompt."""
    completo = COMPLETO.model_copy(update={"archivos": ["a", "b"]})
    texto = _dinamico(config, pedido=completo)
    assert "- fecha_necesita: 2026-10-09" in texto
    assert "Están todos los datos." in texto
    assert 'Un "sí", un "dale", un "listo, gracias" o un 👍 solo son un sí' in texto
    assert 'Un "gracias" pelado, sin nada más, no confirma' in texto
    assert texto.endswith("Ya mandó 2 archivos del diseño por WhatsApp.")


def test_pedido_confirmado_solo_si_lo_hay(config: ConfigNegocio) -> None:
    """R7: el párrafo PEDIDO YA CONFIRMADO entra al dinámico solo con un pedido confirmado."""
    assert "PEDIDO YA CONFIRMADO" not in _dinamico(config)
    texto = _dinamico(config, confirmado=COMPLETO)
    assert "PEDIDO YA CONFIRMADO\nEste cliente ya confirmó un pedido, que quedó tomado y anotado:\n- producto: sellos" in texto
    assert texto.index("PEDIDO YA CONFIRMADO") < texto.index("PEDIDO EN CURSO")


def test_primer_contacto_solo_con_la_pregunta_viva(config: ConfigNegocio) -> None:
    """SPECS §13: el párrafo PRIMER CONTACTO va solo con la pregunta del nombre viva, antes del pedido."""
    assert "PRIMER CONTACTO" not in _dinamico(config)
    texto = _dinamico(config, nombre_preguntado=True, confirmado=COMPLETO)
    assert texto.index("PRIMER CONTACTO") < texto.index("PEDIDO YA CONFIRMADO") < texto.index("PEDIDO EN CURSO")


def test_sin_perfil_usa_el_literal(config: ConfigNegocio) -> None:
    """R43 (lado del prompt): sin nombre de perfil, el prompt muestra el literal que le dice que no es un nombre."""
    texto = _dinamico(config, nombre_perfil=None)
    assert f'El nombre de perfil de WhatsApp de quien escribe es "{NOMBRE_SIN_PERFIL}".' in texto
    assert f'o el literal "{NOMBRE_SIN_PERFIL}"' in texto
    assert 'quien escribe es "Ana Prueba".' in _dinamico(config)
