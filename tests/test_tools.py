import json
from typing import Any, Iterator

from app.config import ConfigNegocio
from app.tools import (
    CAMPOS_DEL_PEDIDO,
    MATERIAL_A_DEFINIR,
    MOTIVOS_DERIVACION,
    TEMAS_CONSULTA,
    definir_tools,
)

NOMBRES = ["registrar_pedido", "pedir_dato_faltante", "consulta_general", "derivar_a_asesor", "confirmar_pedido"]


def _por_nombre(config: ConfigNegocio) -> dict[str, dict[str, Any]]:
    return {tool["name"]: tool for tool in definir_tools(config)}


def _claves(esquema: object) -> Iterator[str]:
    if isinstance(esquema, dict):
        for clave, valor in esquema.items():
            yield clave
            yield from _claves(valor)
    elif isinstance(esquema, list):
        for valor in esquema:
            yield from _claves(valor)


def _enum_de(propiedad: dict[str, Any]) -> list[object]:
    return next(opcion["enum"] for opcion in propiedad["anyOf"] if "enum" in opcion)


def test_ninguna_tool_recibe_telefono(config: ConfigNegocio) -> None:
    """R13: telefono no figura en ningún esquema, ni como propiedad ni en el texto."""
    tools = definir_tools(config)
    assert "telefono" not in set(_claves(tools))
    assert "telefono" not in json.dumps(tools)


def test_cada_tool_es_strict_y_cerrada(config: ConfigNegocio) -> None:
    """R11: strict en las 5 tools, additionalProperties false y todos los campos en required."""
    for tool in definir_tools(config):
        esquema = tool["input_schema"]
        assert tool["strict"] is True, tool["name"]
        assert esquema["additionalProperties"] is False, tool["name"]
        assert esquema["required"] == list(esquema["properties"]), tool["name"]


def test_los_campos_de_registrar_pedido_admiten_null(config: ConfigNegocio) -> None:
    """R11: con strict todo va en required, así que cada campo opcional de SPECS §6 acepta null."""
    propiedades = _por_nombre(config)["registrar_pedido"]["input_schema"]["properties"]
    assert list(propiedades) == list(CAMPOS_DEL_PEDIDO)
    for campo, propiedad in propiedades.items():
        assert {"type": "null"} in propiedad["anyOf"], campo


def test_el_orden_de_las_tools_es_fijo(config: ConfigNegocio) -> None:
    """R18: mismas tools en el mismo orden y byte a byte en cada request."""
    assert [tool["name"] for tool in definir_tools(config)] == NOMBRES
    assert json.dumps(definir_tools(config)) == json.dumps(definir_tools(config))


def test_el_enum_de_productos_sale_del_catalogo(config: ConfigNegocio) -> None:
    """SPECS §4: el enum de producto son los id del catálogo de la config, y la sigue si cambia."""
    producto = _por_nombre(config)["registrar_pedido"]["input_schema"]["properties"]["producto"]
    assert _enum_de(producto) == [item.id for item in config.catalogo]

    recortada = config.model_copy(update={"catalogo": config.catalogo[:2]})
    producto = _por_nombre(recortada)["registrar_pedido"]["input_schema"]["properties"]["producto"]
    assert _enum_de(producto) == ["impresion_digital", "gran_formato"]


def test_tiene_diseno_tiene_tres_estados(config: ConfigNegocio) -> None:
    """SPECS §5: tiene_diseno es si, no o requiere_servicio, no un booleano."""
    propiedad = _por_nombre(config)["registrar_pedido"]["input_schema"]["properties"]["tiene_diseno"]
    assert _enum_de(propiedad) == ["si", "no", "requiere_servicio"]


def test_vocabularios_cerrados_de_specs(config: ConfigNegocio) -> None:
    """SPECS §6: el modelo elige de enums cerrados; los datos, temas y motivos son los del SPECS."""
    tools = _por_nombre(config)
    assert tools["pedir_dato_faltante"]["input_schema"]["properties"]["dato"]["enum"] == list(CAMPOS_DEL_PEDIDO)
    assert tools["consulta_general"]["input_schema"]["properties"]["tema"]["enum"] == list(TEMAS_CONSULTA)
    assert TEMAS_CONSULTA == (
        "direccion", "horarios", "envios", "estacionamiento",
        "presupuestos", "medios_pago", "envio_archivos", "catalogo",
    )
    assert tools["derivar_a_asesor"]["input_schema"]["properties"]["motivo"]["enum"] == list(MOTIVOS_DERIVACION)
    assert MOTIVOS_DERIVACION == ("sin_stock", "lo_pide_el_cliente", "fuera_de_alcance", "plazo_o_precio")
    assert tools["confirmar_pedido"]["input_schema"]["properties"]["acepta"]["type"] == "boolean"


def test_material_a_definir_esta_en_las_descripciones(config: ConfigNegocio) -> None:
    """R15: registrar_pedido y pedir_dato_faltante nombran el valor exacto del material a definir."""
    tools = _por_nombre(config)
    material = tools["registrar_pedido"]["input_schema"]["properties"]["material"]["description"]
    assert f"'{MATERIAL_A_DEFINIR}'" in material
    assert "[dato_faltante: material]" in material
    assert f"'{MATERIAL_A_DEFINIR}'" in tools["pedir_dato_faltante"]["description"]


def test_sin_stock_nunca_ofrece_alternativas(config: ConfigNegocio) -> None:
    """R15: la falta de stock va a derivar_a_asesor(sin_stock), sin alternativas."""
    derivar = _por_nombre(config)["derivar_a_asesor"]["description"]
    assert "Nunca ofrezcas alternativas" in derivar
    assert "sin_stock" in derivar
