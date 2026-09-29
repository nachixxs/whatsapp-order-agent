"""Las 5 tools del agente (SPECS §3 y §6): esquemas y descripciones medidas (R17)."""

from typing import Any

from app.config import ConfigNegocio

# SPECS §5. El orden es el de la repregunta: se pide el primero que falte
CAMPOS_DEL_PEDIDO: tuple[str, ...] = (
    "producto", "material", "medidas", "cantidad", "fecha_necesita", "tiene_diseno", "nombre_cliente",
)
ESTADOS_DISENO: tuple[str, ...] = ("si", "no", "requiere_servicio")
TEMAS_CONSULTA: tuple[str, ...] = (
    "direccion", "horarios", "envios", "estacionamiento",
    "presupuestos", "medios_pago", "envio_archivos", "catalogo",
)
MOTIVOS_DERIVACION: tuple[str, ...] = ("sin_stock", "lo_pide_el_cliente", "fuera_de_alcance", "plazo_o_precio")
MATERIAL_A_DEFINIR = "a definir con el asesor"  # R15
MARCADOR_REPREGUNTA_MATERIAL = "[dato_faltante: material]"  # R21


def _nulable(esquema: dict[str, Any], descripcion: str) -> dict[str, Any]:
    # R11: strict exige todos los campos en required; el que no dijo viaja como null
    return {"anyOf": [esquema, {"type": "null"}], "description": descripcion}


def _tool(nombre: str, descripcion: str, propiedades: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": nombre,
        "description": descripcion,
        "input_schema": {
            "type": "object",
            "properties": propiedades,
            "required": list(propiedades),
            "additionalProperties": False,
        },
        "strict": True,  # R11
    }


def _registrar_pedido(config: ConfigNegocio) -> dict[str, Any]:
    texto = {"type": "string"}
    return _tool(
        "registrar_pedido",
        "Guarda los datos de un trabajo que el cliente quiere encargar. "
        "Llamala cada vez que diga algo del pedido, aunque falten datos: "
        "los campos se van acumulando entre mensajes. Pasá solo los que "
        "dijo; no completes los que no dijo. "
        "Todo campo que no dijo, o que no corresponde mandar, va en null.",
        {
            "producto": _nulable(
                {"type": "string", "enum": [producto.id for producto in config.catalogo]},
                "Id de la familia del catálogo que corresponde al trabajo.",
            ),
            "material": _nulable(
                texto,
                "Material en el que se imprime, tal cual lo dijo el "
                "cliente. Por ejemplo 'cartulina 300g' o 'lona'; un "
                "material dicho como preferencia ('lo quiero en vinilo') "
                "va acá. Si hay un pedido en curso sin material y el "
                "cliente pregunta qué materiales hay, dice que no sabe en "
                "qué material lo quiere o que lo decida la imprenta, mandá "
                "exactamente "
                f"'{MATERIAL_A_DEFINIR}'. Si tu último turno fue "
                f"`{MARCADOR_REPREGUNTA_MATERIAL}` —esa "
                "repregunta le ofrece dejarlo a definir con el asesor— y "
                "contesta solo que sí, 'dale' o un 👍, sin nombrar un "
                "material, está aceptando: también mandá exactamente "
                f"'{MATERIAL_A_DEFINIR}'. Si ya había dicho un material, "
                "mandá ese valor solo si pide dejarlo a definir; si "
                "pregunta por otros materiales, no mandes este campo. "
                "Nunca pongas un material que el cliente no dijo. Si "
                "pregunta si tienen un material o si les queda, dice que "
                "falta uno o pide una alternativa porque no tienen o no "
                "les queda, no va acá: es "
                "derivar_a_asesor con sin_stock.",
            ),
            "medidas": _nulable(
                texto, "Medidas de la pieza, tal cual las dijo. Por ejemplo '9x5 cm'."
            ),
            "cantidad": _nulable(
                {"type": "integer"}, "Cuántas unidades quiere. Entero positivo."
            ),
            "fecha_necesita": _nulable(
                texto,
                "Fecha en la que el cliente necesita el trabajo, en "
                "formato YYYY-MM-DD, resuelta contra la fecha de hoy "
                "que figura en el prompt. Es cuándo lo necesita él, no "
                "una fecha de entrega prometida.",
            ),
            "tiene_diseno": _nulable(
                {"type": "string", "enum": list(ESTADOS_DISENO)},
                "'si' si ya tiene el arte hecho, 'no' si no lo tiene "
                "pero lo consigue por su cuenta, 'requiere_servicio' si "
                "quiere que se lo diseñemos nosotros.",
            ),
            "nombre_cliente": _nulable(
                texto,
                "Nombre de quien encarga el trabajo, de persona o de "
                "comercio. Se usa el nombre de perfil de WhatsApp solo "
                "si es un nombre: si es un emoji, un número o un "
                "puñado de símbolos, NO se manda este campo y se "
                "pregunta con pedir_dato_faltante.",
            ),
        },
    )


def _enum(valores: tuple[str, ...], descripcion: str) -> dict[str, Any]:
    return {"type": "string", "enum": list(valores), "description": descripcion}


def definir_tools(config: ConfigNegocio) -> list[dict[str, Any]]:
    """Las 5 tools en orden fijo: otro orden invalida la caché sin error (R18)."""
    return [
        _registrar_pedido(config),
        _tool(
            "pedir_dato_faltante",
            "Llamala cuando el cliente quiere encargar algo pero falta un dato "
            "y en este mensaje no dio ninguno nuevo. Pasá un solo dato: el más "
            "importante de los que faltan. No la uses si ya están todos. Si hay "
            "un pedido en curso sin material y el cliente pregunta qué materiales "
            "hay, dice que no sabe en qué material lo quiere o que lo decida la "
            "imprenta, o si tu último turno fue "
            f"`{MARCADOR_REPREGUNTA_MATERIAL}` y "
            "contesta solo que sí, 'dale' o un 👍, sin nombrar un material, no la "
            f"uses: es `registrar_pedido` con material '{MATERIAL_A_DEFINIR}'.",
            {"dato": _enum(CAMPOS_DEL_PEDIDO, "El dato que hay que pedirle al cliente.")},
        ),
        _tool(
            "consulta_general",
            "Responde una consulta sobre la imprenta. Cubre solo estos temas: "
            "dirección, horarios, envíos a domicilio, estacionamiento, plazo "
            "de los presupuestos, métodos de pago, cómo mandar un archivo, y "
            "qué productos se hacen. Cualquier otra consulta no es esta tool: "
            "es `derivar_a_asesor` con motivo fuera_de_alcance.",
            {"tema": _enum(TEMAS_CONSULTA, "Tema del FAQ sobre el que pregunta el cliente.")},
        ),
        _tool(
            "derivar_a_asesor",
            "Pasa la conversación a un asesor humano. Usala cuando el cliente "
            "lo pida, cuando pregunte precio o plazo de entrega, cuando a la "
            "imprenta le falte stock o material —el cliente pregunta si lo "
            "tienen o dice que no les queda—, o ante cualquier duda que no "
            "puedas resolver con las otras tools. Nunca ofrezcas alternativas "
            "de material por tu cuenta: eso es sin_stock.",
            {
                "motivo": _enum(
                    MOTIVOS_DERIVACION,
                    "sin_stock: no hay material o stock. "
                    "lo_pide_el_cliente: pidió hablar con alguien. "
                    "fuera_de_alcance: una duda que no podés resolver. "
                    "plazo_o_precio: preguntó cuánto sale o para cuándo está.",
                )
            },
        ),
        _tool(
            "confirmar_pedido",
            "Registra la respuesta del cliente al resumen del pedido. "
            "acepta=true solo si acepta el resumen tal cual está. Si acepta "
            "pero pide cambiar algo, o si rechaza, es acepta=false.",
            {
                "acepta": {
                    "type": "boolean",
                    "description": "true si da el OK al resumen tal cual; false si no.",
                }
            },
        ),
    ]
