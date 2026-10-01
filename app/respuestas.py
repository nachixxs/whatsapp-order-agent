"""Los textos que lee el cliente, armados por Python desde la config (R19)."""

import re
import unicodedata
from collections.abc import Sequence
from datetime import date, datetime

from app.config import ConfigNegocio, Franja
from app.formato import DIAS, MESES, en_una_linea
from app.pedidos import Pedido

# R11: el modelo no devolvió una tool, o una que no se sabe aplicar. Se repregunta, no se improvisa
MENSAJE_NO_ENTENDIDO = "Perdón, no te entendí bien. ¿Me lo contás de nuevo?"
# R12: API caída o timeout. No dice qué se rompió
MENSAJE_ERROR_INTERNO = "Uf, se me complicó procesar tu mensaje. ¿Me lo escribís de nuevo en un rato?"
# R8: acepta=false vuelve a recolectar sin perder lo que ya dio
MENSAJE_PEDIDO_RECHAZADO = "Dale, no lo guardo así. Decime qué querés cambiar y lo corrijo."
# R15: va delante de la repregunta o del resumen del turno que dejó el material a definir.
# No nombra ningún material
AVISO_MATERIAL_A_DEFINIR = "El material lo confirma un asesor cuando cotiza, así que lo dejamos a definir."
# R2: sale solo con la fila escrita. Es el único texto con "¡Listo!"
MENSAJE_PEDIDO_CONFIRMADO = (
    "¡Listo! Ya guardamos tus datos. Un asesor se contactará para confirmar "
    "el pago y la entrega."
)
# R2: el pedido sigue pendiente y el mensaje siguiente reintenta la escritura
MENSAJE_ERROR_AL_GUARDAR = (
    "Tengo todos tus datos pero no los pude guardar bien. Escribime de nuevo en "
    "unos minutos así no se pierde nada."
)
# R6. R38: detrás va con_frase_de_horario
MENSAJE_CAMBIO_DURANTE_LA_CONFIRMACION = (
    "Ese pedido ya lo estaba confirmando, así que un cambio lo tiene que ver un asesor con vos."
)
# R36: audio, sticker o ubicación
MENSAJE_TIPO_NO_SOPORTADO = (
    "Por ahora solo puedo leer mensajes de texto. Si querés, escribime lo que "
    "necesitás y seguimos, o mandame el diseño como documento."
)

_PREGUNTAS: dict[str, str] = {
    # R15: la salida para el que no sabe es la que el prompt espera tras [dato_faltante: material]
    "material": "¿En qué material lo querés? Si no sabés, lo dejamos a definir con el asesor.",
    "medidas": "¿Qué medidas necesitás?",
    "cantidad": "¿Cuántas unidades son?",
    "fecha_necesita": "¿Para qué fecha lo necesitás?",
    "tiene_diseno": "¿Tenés el diseño hecho o querés que te lo hagamos nosotros?",
    "nombre_cliente": "¿A nombre de quién te lo anoto?",
}

_DISENO_EN_EL_RESUMEN: dict[str, str] = {
    "si": "ya lo tenés",
    "no": "todavía no lo tenés",
    "requiere_servicio": "lo diseñamos nosotros",
}
_DISENO_PARA_EL_ASESOR = {"si": "lo tiene", "no": "no lo tiene", "requiere_servicio": "lo diseñamos nosotros"}

# R15: sin_stock no ofrece alternativas. R19: plazo_o_precio no arriesga ningún número
_MOTIVO_EN_TEXTO: dict[str, str] = {
    "sin_stock": "Eso lo tiene que ver un asesor con vos.",
    "lo_pide_el_cliente": "Dale, te paso con un asesor.",
    "fuera_de_alcance": "Esto prefiero que te lo conteste un asesor.",
    "plazo_o_precio": "El precio y el plazo te los confirma un asesor, no te los puedo dar yo.",
}
# R47: lo lee el asesor. R6: el cambio durante la confirmación también deriva
_MOTIVO_PARA_EL_ASESOR: dict[str, str] = {
    "sin_stock": "Pregunta por stock de un material",
    "lo_pide_el_cliente": "Pidió hablar con una persona",
    "fuera_de_alcance": "Una consulta que el bot no resuelve",
    "plazo_o_precio": "Pregunta por precio o plazo",
    "cambio_sobre_pedido_confirmado": (
        "Pidió un cambio o una cancelación sobre un pedido que se estaba confirmando"
    ),
}
SIN_NOMBRE = "sin nombre registrado"
PREGUNTA_DEL_NOMBRE = "¿Cómo es tu nombre? Así te agendamos."  # R44
_SINTAXIS_DE_CHATWOOT = re.compile(r"[\[\]()<>]|mention://", re.IGNORECASE)  # R47: links y menciones; los C* salen aparte


def listar(items: Sequence[str], conector: str = "o") -> str:
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} {conector} {items[-1]}"


def franjas(franjas: list[Franja]) -> str:
    return listar([f"{franja.abre:%H:%M} a {franja.cierra:%H:%M}" for franja in franjas], "y")


def fecha_en_palabras(fecha: date) -> str:
    """R40: 'miércoles 14 de octubre', con los diccionarios de formato y sin año."""
    return f"{DIAS[fecha.weekday()]} {fecha.day} de {MESES[fecha.month - 1]}"


def _dias_agrupados(config: ConfigNegocio) -> list[str]:
    # Días seguidos con el mismo horario van juntos: siete renglones iguales no se leen en un WhatsApp
    grupos: list[tuple[int, int, str]] = []
    for dia in range(7):
        texto = franjas(config.horario.del_dia(dia))
        if not texto:
            continue
        if grupos and grupos[-1][2] == texto and grupos[-1][1] == dia - 1:
            grupos[-1] = (grupos[-1][0], dia, texto)
        else:
            grupos.append((dia, dia, texto))
    return [
        f"{DIAS[desde]} de {texto}" if desde == hasta else f"{DIAS[desde]} a {DIAS[hasta]} de {texto}"
        for desde, hasta, texto in grupos
    ]


def _horarios(config: ConfigNegocio) -> str:
    texto = f"Atendemos {'; '.join(_dias_agrupados(config))}."
    cerrados = [DIAS[dia] for dia in range(7) if not config.horario.del_dia(dia)]
    cerramos = [listar(cerrados)] if cerrados else []
    # R39: se nombran si hay alguno cargado, aunque el local abra los siete días
    if config.feriados:
        cerramos.append("feriados y días no laborables")
    if cerramos:
        texto += f" {', '.join(cerramos).capitalize()} cerramos."
    return texto


def _familias(config: ConfigNegocio) -> str:
    return listar([producto.familia for producto in config.catalogo])


def respuesta_faq(tema: str, config: ConfigNegocio) -> str:
    """SPECS §6: un texto por tema del enum de consulta_general, con los datos de la config."""
    if tema == "direccion":
        return f"Estamos en {config.direccion}. Te esperamos."
    if tema == "horarios":
        return _horarios(config)
    if tema == "envios":
        if config.hace_envios:
            return "Sí, hacemos envíos a domicilio. Consultanos por tu zona."
        return f"No hacemos envíos: el trabajo se retira por el local, en {config.direccion}."
    if tema == "estacionamiento":
        if config.tiene_estacionamiento:
            return "Sí, tenés lugar para estacionar cuando venís."
        return "No tenemos estacionamiento propio."
    if tema == "presupuestos":
        return f"Los presupuestos te los pasamos dentro de las {config.plazo_presupuesto_horas} horas hábiles."
    if tema == "medios_pago":
        return f"Podés pagar con {listar(config.medios_pago)}."
    if tema == "envio_archivos":
        return (
            "Podés mandarme el archivo por acá, como documento, o mandarlo por "
            f"mail a {config.mail_archivos}. Lo que te quede más cómodo."
        )
    if tema == "catalogo":
        lineas = [f"- {p.familia}: {listar(p.ejemplos)}." for p in config.catalogo]
        return "Esto es lo que hacemos:\n" + "\n".join(lineas)
    return MENSAJE_NO_ENTENDIDO


def pregunta_por_dato(dato: str, config: ConfigNegocio) -> str:
    """SPECS §6: la repregunta por un campo de CAMPOS_DEL_PEDIDO; el modelo solo elige cuál."""
    if dato == "producto":
        return f"¿Qué necesitás? Hacemos {_familias(config)}."
    return _PREGUNTAS.get(dato, MENSAJE_NO_ENTENDIDO)


def aviso_de_descartados(
    descartados: Sequence[str], pedido: Pedido, config: ConfigNegocio
) -> str | None:
    """R14: con los descartados de sumar_campos, el texto que va en lugar de la repregunta o del resumen.

    None: sigue el flujo normal. El nombre descartado no se avisa: el pedido queda sin él y se repregunta.
    """
    if "producto" in descartados:
        return f"Eso no lo tengo en la lista. Hacemos {_familias(config)}. ¿Cuál de esos necesitás?"
    # Si ya tenía una fecha válida, el reenvío mal resuelto se ignora y la charla sigue
    if "fecha_necesita" in descartados and pedido.fecha_necesita is None:
        return "Esa fecha ya pasó, así que algo entendí mal. ¿Para qué día lo necesitás?"
    return None


def con_aviso_de_material(texto: str, *, es_resumen: bool) -> str:
    """R15: el aviso del turno que dejó el material a definir, delante de la repregunta o del resumen."""
    separador = "\n\n" if es_resumen else " "
    return f"{AVISO_MATERIAL_A_DEFINIR}{separador}{texto}"


def resumen_pedido(pedido: Pedido, config: ConfigNegocio) -> str:
    """§7: el resumen para confirmar. fecha_necesita es cuándo lo necesita él, no una entrega (R19)."""
    if not pedido.completo or pedido.fecha_necesita is None or pedido.tiene_diseno is None:
        raise ValueError("el resumen es de un pedido completo")
    familias = {producto.id: producto.familia for producto in config.catalogo}
    return "\n".join([
        f"Te leo el pedido, {pedido.nombre_cliente}:",
        f"- Trabajo: {familias.get(pedido.producto or '', pedido.producto)}",
        f"- Material: {pedido.material}",
        f"- Medidas: {pedido.medidas}",
        f"- Cantidad: {pedido.cantidad}",
        f"- Diseño: {_DISENO_EN_EL_RESUMEN[pedido.tiene_diseno]}",
        f"- Lo necesitás para el {fecha_en_palabras(pedido.fecha_necesita)}",
        "¿Está todo bien? Confirmame y se lo paso a un asesor, que te va a "
        "pasar el precio y el plazo.",
    ])


def texto_derivacion(motivo: str, config: ConfigNegocio) -> str:
    """SPECS §6: el texto al pasar la charla a un asesor, por motivo. R38: detrás va con_frase_de_horario."""
    return _MOTIVO_EN_TEXTO[motivo]


def con_frase_de_horario(texto: str, config: ConfigNegocio, ahora: datetime) -> str:
    """R38: en horario, que le contesta un asesor; fuera, cuándo reabre. Nunca "en breve" ni "enseguida"."""
    if config.esta_abierto(ahora):
        return f"{texto} Ya le paso tu consulta así te contesta por acá."
    base = f"{texto} Ya le dejo tu consulta, y te va a contestar cuando volvamos a abrir"
    apertura = config.proxima_apertura(ahora)
    if apertura is None:  # R39: ninguna apertura en 31 días; la oración cierra sin fecha
        return f"{base}."
    dias = (apertura.date() - ahora.astimezone(config.zona).date()).days
    dia = {0: "hoy", 1: "mañana"}.get(dias, f"el {fecha_en_palabras(apertura.date())}")
    return f"{base}: {dia} desde las {apertura:%H:%M}."


def _para_nota(valor: str | None) -> str:
    texto = _SINTAXIS_DE_CHATWOOT.sub("", "".join(c for c in valor or "" if unicodedata.category(c)[0] != "C"))
    return texto if texto == (valor or "") else _para_nota(texto)  # sacar puede armar otro "mention://"


def nota_de_derivacion(motivo: str, pedido: Pedido | None, nombre: str | None, config: ConfigNegocio) -> str:
    """R47: la nota interna para el asesor. R6: el pedido con las etiquetas del resumen, sin vacíos."""
    nombre = _para_nota(en_una_linea(nombre or "")) or SIN_NOMBRE
    lineas = [f"Derivación del bot: {_MOTIVO_PARA_EL_ASESOR[motivo]}.", f"Nombre: {nombre}"]
    if pedido is None:
        return "\n".join(lineas)
    familias = {producto.id: producto.familia for producto in config.catalogo}
    datos = [
        ("- A nombre de:", _para_nota(pedido.nombre_cliente)),
        ("- Trabajo:", familias.get(pedido.producto or "", pedido.producto)),
        ("- Material:", _para_nota(pedido.material)),
        ("- Medidas:", _para_nota(pedido.medidas)),
        ("- Cantidad:", pedido.cantidad),
        ("- Diseño:", pedido.tiene_diseno and _DISENO_PARA_EL_ASESOR[pedido.tiene_diseno]),
        ("- Lo necesita para el", pedido.fecha_necesita and fecha_en_palabras(pedido.fecha_necesita)),
    ]
    if detalle := [f"{etiqueta} {valor}" for etiqueta, valor in datos if valor]:
        lineas += ["Así entendió el bot el pedido:", *detalle]
    return "\n".join(lineas)


def nota_de_archivos(lineas: str) -> str:
    """R32, R33: los archivos que la persona suma al pedido; `lineas` como en la celda (texto_de_archivos)."""
    cantidad = sum(bool(linea.strip()) for linea in lineas.splitlines())
    if cantidad < 1:
        raise ValueError("una nota de archivos es por al menos un archivo")
    encabezado = "Archivo que llegó" if cantidad == 1 else "Archivos que llegaron"
    return f"{encabezado} después de confirmar el pedido.\n" + "\n".join(map(_para_nota, lineas.splitlines()))


def con_pregunta_del_nombre(texto: str) -> str:
    """R44: la pregunta va debajo de la respuesta elegida, nunca sola."""
    if not texto.strip():
        raise ValueError("la pregunta del nombre va debajo de una respuesta")
    return f"{texto}\n\n{PREGUNTA_DEL_NOMBRE}"


def acuse_de_archivos(recibidos: int, pedido: Pedido, config: ConfigNegocio) -> str:
    """R30: un acuse por ráfaga, con los recibidos. R31: si el pedido quedó completo, sigue el resumen."""
    if recibidos < 1:
        raise ValueError("un acuse es por al menos un archivo")
    if recibidos == 1:
        acuse = "¡Recibí tu archivo! Ya queda guardado con tu pedido y un asesor lo va a revisar."
    else:
        acuse = (
            f"¡Recibí tus {recibidos} archivos! Ya quedan guardados con tu pedido y "
            "un asesor los va a revisar."
        )
    if pedido.completo:
        return f"{acuse}\n\n{resumen_pedido(pedido, config)}"
    return f"{acuse} ¿Seguimos con los datos?"


def acuse_de_archivos_despues_de_confirmar(recibidos: int) -> str:
    """R32: el archivo no toca la fila, así que no dice "guardado": lo suma una persona."""
    if recibidos < 1:
        raise ValueError("un acuse es por al menos un archivo")
    if recibidos == 1:
        return (
            "¡Recibí tu archivo! Tu pedido ya estaba confirmado, así que se lo "
            "paso al asesor para que lo sume."
        )
    return (
        f"¡Recibí tus {recibidos} archivos! Tu pedido ya estaba confirmado, así "
        "que se los paso al asesor para que los sume."
    )
