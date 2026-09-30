import logging
from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.config import ConfigNegocio
from app.pedidos import ArchivoAdjunto, Pedido, sumar_archivo, sumar_campos, texto_de_archivos
from app.respuestas import resumen_pedido
from app.tools import CAMPOS_DEL_PEDIDO, MATERIAL_A_DEFINIR
from tests.conftest import TELEFONO

HORA = datetime(2026, 9, 29, 14, 32)  # hora de pared del negocio, sin zona
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


def _archivo(id_adjunto: int, hora: datetime = HORA, **cambios: object) -> ArchivoAdjunto:
    # Un tamaño por id: dos del mismo tipo y tamaño son el mismo archivo (R29)
    datos = {"id_adjunto": id_adjunto, "id_mensaje": id_adjunto, "tamano": 1000 + id_adjunto}
    return ArchivoAdjunto.model_validate(datos | {"tipo": "pdf", "hora": hora} | cambios)


def _con_archivos(*archivos: ArchivoAdjunto, pedido: Pedido | None = None) -> Pedido:
    pedido = pedido or Pedido(telefono=TELEFONO)
    for archivo in archivos:
        nuevo = sumar_archivo(pedido, archivo)
        assert nuevo is not None
        pedido = nuevo
    return pedido


def test_con_los_siete_campos_esta_completo(config: ConfigNegocio) -> None:
    """SPECS §5: sin campos le faltan los siete; con los siete está completo."""
    assert Pedido(telefono=TELEFONO).faltantes() == list(CAMPOS_DEL_PEDIDO)
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
        ("A definir con el asesor.\n", MATERIAL_A_DEFINIR),
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
    ("campo", "valor", "queda"),
    [
        ("material", "lona\n- Precio: $0", "lona - Precio: $0"),
        ("medidas", "x" * 300, "x" * 200),
        ("material", "y" * 300, "y" * 200),
        ("nombre_cliente", "Ana" + "a" * 97, "Ana" + "a" * 57),
        ("nombre_cliente", "Ana\nPrueba", "Ana Prueba"),
    ],
)
def test_los_textos_del_modelo_van_en_una_linea_y_con_tope(
    config: ConfigNegocio, campo: str, valor: str, queda: str
) -> None:
    """R35, R19: un salto de línea no arma un renglón propio en el resumen y un campo largo se recorta."""
    pedido, descartados = _sumar(config, {campo: valor})
    assert getattr(pedido, campo) == queda
    assert descartados == []


def test_el_resumen_con_textos_enormes_entra_en_un_mensaje(config: ConfigNegocio) -> None:
    """R35, R19: con los textos llenos de saltos de línea, el resumen tiene sus renglones y no pasa 4.096."""
    base, _ = _sumar(config, COMPLETO)
    enormes = {campo: "Zz\n" * 20_000 for campo in ("nombre_cliente", "material", "medidas")}
    pedido, descartados = _sumar(config, enormes, base)
    assert descartados == []
    resumen = resumen_pedido(pedido, config)
    assert len(resumen) <= 4096
    assert resumen.count("\n") == resumen_pedido(base, config).count("\n")


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
    """SQLite guarda el pedido como JSON; vuelve igual, con sus archivos y aunque su fecha ya pasó."""
    completo, _ = _sumar(config, COMPLETO)
    pedido = _con_archivos(_archivo(1), pedido=completo)
    vencido = pedido.model_copy(update={"fecha_necesita": date(2026, 10, 1)})
    for guardado in (pedido, vencido):
        assert Pedido.model_validate_json(guardado.model_dump_json()) == guardado
        assert Pedido.model_validate(guardado.model_dump(mode="json")) == guardado
    assert pedido.model_dump(mode="json")["fecha_necesita"] == "2026-10-20"
    assert pedido.model_dump(mode="json")["archivos"][0]["hora"] == "2026-09-29T14:32:00"


def test_pedido_guardado_sin_archivos_se_sigue_leyendo(config: ConfigNegocio) -> None:
    """R26: un pedido guardado antes de la columna archivos se lee con la lista vacía, no se descarta."""
    pedido, _ = _sumar(config, COMPLETO)
    leido = Pedido.model_validate_json(pedido.model_dump_json(exclude={"archivos"}))
    assert leido == pedido
    assert leido.archivos == []


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


def test_los_archivos_se_suman_en_orden_de_hora(config: ConfigNegocio) -> None:
    """R29: cada archivo se suma sin pisar los anteriores, ordenado por hora aunque llegue desordenado."""
    uno = _con_archivos(_archivo(3, HORA + timedelta(minutes=5)))
    tres = _con_archivos(_archivo(1), _archivo(2, HORA + timedelta(minutes=1)), pedido=uno)
    assert [archivo.id_adjunto for archivo in tres.archivos] == [1, 2, 3]
    assert [archivo.id_adjunto for archivo in uno.archivos] == [3]
    despues, _ = _sumar(config, {"cantidad": 10}, tres)
    assert despues.archivos == tres.archivos


def test_a_igual_hora_desempata_el_id_del_mensaje() -> None:
    """R29: a igual hora, el orden lo da el id del mensaje, no el orden de llegada."""
    pedido = _con_archivos(_archivo(7, id_mensaje=51), _archivo(8, id_mensaje=50))
    assert [archivo.id_mensaje for archivo in pedido.archivos] == [50, 51]


def test_el_mismo_archivo_dos_veces_no_duplica() -> None:
    """R29: el mismo adjunto, o la foto reenviada en otro mensaje (mismo tipo y tamaño), no suma otra línea."""
    primero = _archivo(1, tipo="jpg", tamano=13_666)
    reenvio = _archivo(2, HORA + timedelta(minutes=4), tipo="jpg", tamano=13_666)
    assert _con_archivos(primero, primero, reenvio).archivos == [primero]


def test_sin_tamano_o_con_otro_tipo_es_otro_archivo() -> None:
    """R29: sin tamaño no hay con qué compararlos y otro tipo es otro archivo: no se pierde ninguno."""
    archivos = [_archivo(1, tamano=None), _archivo(2, tamano=None)]
    archivos += [_archivo(3, tamano=500), _archivo(4, tipo="jpg", tamano=500)]
    assert _con_archivos(*archivos).archivos == archivos


@pytest.mark.parametrize("antes", [None, "no", "requiere_servicio", "si"])
def test_un_archivo_fuerza_tiene_diseno_si(antes: str | None) -> None:
    """R29: un archivo pisa el tiene_diseno anterior con "si", también si es repetido."""
    pedido = Pedido(telefono=TELEFONO, tiene_diseno=antes)
    con_archivo = _con_archivos(_archivo(1), pedido=pedido)
    assert con_archivo.tiene_diseno == "si"
    assert pedido.tiene_diseno == antes
    repetido = _con_archivos(_archivo(1), pedido=con_archivo.model_copy(update={"tiene_diseno": antes}))
    assert repetido.tiene_diseno == "si"


def test_del_archivo_61_en_adelante_no_se_suman() -> None:
    """R35: con 60 archivos, uno nuevo no entra (None) y el pedido queda como estaba; un repetido sí pasa."""
    lleno = _con_archivos(*(_archivo(i) for i in range(1, 61)))
    assert len(lleno.archivos) == 60
    assert sumar_archivo(lleno, _archivo(61)) is None
    assert len(lleno.archivos) == 60
    assert sumar_archivo(lleno, _archivo(1)) == lleno


@pytest.mark.parametrize(
    "hora", [datetime(2026, 9, 29, 17, 32, tzinfo=UTC), "2026-09-29T17:32:00Z", "2026-09-29T14:32-03:00"]
)
def test_hora_con_zona_se_rechaza(hora: datetime | str) -> None:
    """R29: la hora va sin zona; comparar una con zona contra una sin zona revienta al ordenar."""
    with pytest.raises(ValidationError):
        _archivo(1, hora)


def test_la_celda_lleva_una_linea_por_archivo() -> None:
    """R29: una línea por archivo en orden, con hora local, tipo y número de adjunto; nunca el data_url."""
    pedido = _con_archivos(_archivo(124, HORA + timedelta(minutes=3), tipo="jpg"), _archivo(123))
    esperado = "1. 29/09 14:32 · pdf · adjunto #123\n2. 29/09 14:35 · jpg · adjunto #124"
    assert texto_de_archivos(pedido) == esperado
    assert texto_de_archivos(Pedido(telefono=TELEFONO)) == ""
    with pytest.raises(ValidationError):
        _archivo(1, data_url="https://chatwoot.example/rails/active_storage/blobs/redirect/x/foto.jpg")


def test_la_celda_de_archivos_no_pasa_50000() -> None:
    """R35: con 60 archivos, tipos enormes con saltos de línea e ids al tope, la celda tiene 60 líneas."""
    archivos = [_archivo(2**63 - i, tipo="x\n" * 30_000, tamano=i) for i in range(1, 61)]
    texto = texto_de_archivos(_con_archivos(*archivos))
    assert texto.count("\n") == 59
    assert len(texto) <= 50_000
