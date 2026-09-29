import logging
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from app.config import ConfigNegocio
from app.pedidos import CAMPOS, MATERIAL_A_DEFINIR, Pedido, sumar_campos
from tests.conftest import TELEFONO

COMPLETO = {
    "producto": "impresion_digital",
    "material": "papel ilustración 300 g",
    "medidas": "9 x 5 cm",
    "cantidad": 500,
    "fecha_necesita": "2026-10-20",
    "tiene_diseno": "requiere_servicio",
    "nombre_cliente": "Ana Prueba",
}


def _sumar(
    config: ConfigNegocio, campos: dict[str, object], pedido: Pedido | None = None
) -> tuple[Pedido, list[str]]:
    return sumar_campos(pedido or Pedido(telefono=TELEFONO), campos, config, config.ahora())


def test_con_los_siete_campos_esta_completo(config: ConfigNegocio) -> None:
    """SPECS §5: sin campos le faltan los siete; con los siete está completo."""
    assert Pedido(telefono=TELEFONO).faltantes() == list(CAMPOS)
    pedido, descartados = _sumar(config, COMPLETO)
    assert descartados == []
    assert pedido.completo and pedido.faltantes() == []
    assert pedido.fecha_necesita == date(2026, 10, 20)


def test_cada_turno_se_suma_a_lo_que_habia(config: ConfigNegocio) -> None:
    """SPECS §6: lo no dicho no borra nada, un valor nuevo pisa, y el pedido anterior no cambia."""
    primero, _ = _sumar(config, {"producto": "sellos", "cantidad": 2, "tiene_diseno": "no"})
    turno = {"cantidad": 3, "medidas": "4 x 2 cm", "tiene_diseno": None}
    segundo, descartados = _sumar(config, turno, primero)
    assert descartados == []
    assert (segundo.producto, segundo.cantidad, segundo.medidas) == ("sellos", 3, "4 x 2 cm")
    assert segundo.tiene_diseno == "no"
    assert segundo.faltantes() == ["material", "fecha_necesita", "nombre_cliente"]
    assert primero.cantidad == 2 and primero.medidas is None


def test_el_telefono_nunca_sale_del_modelo(config: ConfigNegocio) -> None:
    """R13: un telefono en los argumentos se descarta; queda el del contacto de Chatwoot."""
    pedido, descartados = _sumar(config, {"telefono": "+54 9 11 5555-9999", "cantidad": 10})
    assert pedido.telefono == TELEFONO
    assert pedido.cantidad == 10
    assert descartados == ["telefono"]
    with pytest.raises(ValidationError):
        Pedido(telefono="  ")


@pytest.mark.parametrize("producto", ["tazas", "Impresión digital", "IMPRESION_DIGITAL", ""])
def test_producto_fuera_del_catalogo_se_descarta_solo(config: ConfigNegocio, producto: str) -> None:
    """R14: un producto fuera del catálogo se descarta sin pisar el que había; el resto se suma."""
    pedido = Pedido(telefono=TELEFONO, producto="sellos")
    pedido, descartados = _sumar(config, {"producto": producto, "cantidad": 100}, pedido)
    assert pedido.producto == "sellos"
    assert pedido.cantidad == 100
    assert descartados == ["producto"]


@pytest.mark.parametrize(
    ("fecha", "queda"),
    [
        ("2026-10-05", None),  # ayer
        ("2025-10-20", None),  # el año mal resuelto
        ("2026-10-06", date(2026, 10, 6)),  # hoy
        ("2026-10-20", date(2026, 10, 20)),
    ],
)
def test_fecha_pasada_se_descarta(config: ConfigNegocio, fecha: str, queda: date | None) -> None:
    """R14: una fecha_necesita anterior a hoy (config.ahora()) se descarta; el resto se suma."""
    pedido, descartados = _sumar(config, {"fecha_necesita": fecha, "cantidad": 100})
    assert pedido.fecha_necesita == queda
    assert pedido.cantidad == 100
    assert descartados == ([] if queda else ["fecha_necesita"])


def test_fecha_pasada_no_pisa_la_que_habia(config: ConfigNegocio) -> None:
    """R14: el modelo reenvía la fecha con el año mal resuelto; gana la válida que ya estaba."""
    pedido = Pedido(telefono=TELEFONO, fecha_necesita=date(2026, 10, 20))
    pedido, descartados = _sumar(config, {"fecha_necesita": "2025-10-20"}, pedido)
    assert pedido.fecha_necesita == date(2026, 10, 20)
    assert descartados == ["fecha_necesita"]


def test_hoy_es_el_dia_del_negocio(config: ConfigNegocio) -> None:
    """R37: a las 22 del negocio en UTC ya es mañana; la fecha de hoy no se descarta."""
    ahora_utc = datetime(2026, 10, 7, 1, 0, tzinfo=UTC)  # 6/10 a las 22:00 en Buenos Aires
    campos = {"fecha_necesita": "2026-10-06"}
    pedido, descartados = sumar_campos(Pedido(telefono=TELEFONO), campos, config, ahora_utc)
    assert pedido.fecha_necesita == date(2026, 10, 6)
    assert descartados == []


@pytest.mark.parametrize(
    "nombre", ["👍", "cliente", "  CLIENTE ", "desconocido", "+54 9 11 5555-0000", "J", "...", ""]
)
def test_nombre_que_no_parece_nombre_se_descarta(config: ConfigNegocio, nombre: str) -> None:
    """R14: un emoji, "cliente" o un teléfono no son un nombre: se descarta ese campo solo."""
    pedido, descartados = _sumar(config, {"nombre_cliente": nombre, "cantidad": 100})
    assert pedido.nombre_cliente is None
    assert pedido.cantidad == 100
    assert descartados == ["nombre_cliente"]


@pytest.mark.parametrize("nombre", ["Ana", "Lu", "José Pérez", "Kiosco Ejemplo"])
def test_un_comercio_tambien_es_un_nombre(config: ConfigNegocio, nombre: str) -> None:
    """R14: el filtro de nombres es grueso: deja pasar personas y comercios."""
    pedido, descartados = _sumar(config, {"nombre_cliente": nombre})
    assert pedido.nombre_cliente == nombre
    assert descartados == []


@pytest.mark.parametrize(
    ("material", "queda"),
    [
        ("a definir con el asesor", MATERIAL_A_DEFINIR),
        ("A definir con el asesor.", MATERIAL_A_DEFINIR),
        ("  a definir\n con el ASESOR ", MATERIAL_A_DEFINIR),
        (" vinilo mate ", "vinilo mate"),
    ],
)
def test_material_a_definir_queda_exacto_y_completa(
    config: ConfigNegocio, material: str, queda: str
) -> None:
    """R15: las variantes quedan con el valor exacto y el material cuenta como completo."""
    pedido, descartados = _sumar(config, {"material": material})
    assert pedido.material == queda
    assert "material" not in pedido.faltantes()
    assert descartados == []


@pytest.mark.parametrize(
    ("campo", "valor"),
    [
        ("cantidad", 0),
        ("cantidad", -5),
        ("cantidad", 2.5),
        ("cantidad", "muchas"),
        ("tiene_diseno", "tal vez"),
        ("fecha_necesita", "20/10"),
        ("medidas", "   "),
        ("precio", 1000),
    ],
)
def test_valor_invalido_se_descarta_campo_por_campo(
    config: ConfigNegocio, campo: str, valor: object
) -> None:
    """R14: un campo que no pasa se descarta solo, nunca la llamada entera."""
    pedido, descartados = _sumar(config, {campo: valor, "producto": "rotulacion"})
    assert getattr(pedido, campo, None) is None
    assert pedido.producto == "rotulacion"
    assert descartados == [campo]


def test_ida_y_vuelta_por_json(config: ConfigNegocio) -> None:
    """SQLite guarda el pedido como JSON; vuelve igual, aunque su fecha ya haya pasado."""
    pedido, _ = _sumar(config, COMPLETO)
    vencido = pedido.model_copy(update={"fecha_necesita": date(2026, 10, 1)})
    for guardado in (pedido, vencido):
        assert Pedido.model_validate_json(guardado.model_dump_json()) == guardado
        assert Pedido.model_validate(guardado.model_dump(mode="json")) == guardado
    assert pedido.model_dump(mode="json")["fecha_necesita"] == "2026-10-20"


def test_log_y_errores_sin_el_valor(config: ConfigNegocio, caplog: pytest.LogCaptureFixture) -> None:
    """R52: de un campo descartado se loguea el nombre, nunca el valor; el error tampoco lo lleva."""
    campos = {"nombre_cliente": "👍", "producto": "tazas-secretas", "telefono": "+54 9 11 5555-9999"}
    with caplog.at_level(logging.INFO, logger="app.pedidos"):
        _sumar(config, campos)
    for campo, valor in campos.items():
        assert campo in caplog.text
        assert valor not in caplog.text
    with pytest.raises(ValidationError) as error:
        Pedido(telefono=TELEFONO, nombre_cliente="👍")
    assert "👍" not in str(error.value)
