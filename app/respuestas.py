"""Los textos que lee el cliente, armados por Python desde la config (R19)."""

from collections.abc import Sequence
from datetime import date

from app.config import ConfigNegocio, Franja
from app.formato import DIAS, MESES
from app.pedidos import Pedido

# R11: el modelo no devolvió una tool, o una que no se sabe aplicar. Se repregunta, no se improvisa
MENSAJE_NO_ENTENDIDO = "Perdón, no te entendí bien. ¿Me lo contás de nuevo?"
# R12: API caída o timeout. No dice qué se rompió
MENSAJE_ERROR_INTERNO = (
    "Uf, se me complicó procesar tu mensaje. ¿Me lo escribís de nuevo en un rato?"
)
# R8: acepta=false vuelve a recolectar sin perder lo que ya dio
MENSAJE_PEDIDO_RECHAZADO = (
    "Dale, no lo guardo así. Decime qué querés cambiar y lo corrijo."
)
# R15: va delante de la repregunta o del resumen del turno que dejó el material a definir.
# No nombra ningún material
AVISO_MATERIAL_A_DEFINIR = (
    "El material lo confirma un asesor cuando cotiza, así que lo dejamos a definir."
)

_PREGUNTAS: dict[str, str] = {
    # R15: la salida para el que no sabe es la que el prompt espera tras [dato_faltante: material]
    "material": (
        "¿En qué material lo querés? Si no sabés, lo dejamos a definir con "
        "el asesor."
    ),
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

# R15: sin_stock no ofrece alternativas. R19: plazo_o_precio no arriesga ningún número
_MOTIVO_EN_TEXTO: dict[str, str] = {
    "sin_stock": "Eso lo tiene que ver un asesor con vos.",
    "lo_pide_el_cliente": "Dale, te paso con un asesor.",
    "fuera_de_alcance": "Esto prefiero que te lo conteste un asesor.",
    "plazo_o_precio": (
        "El precio y el plazo te los confirma un asesor, no te los puedo dar yo."
    ),
}


def _listar(items: Sequence[str], conector: str = "o") -> str:
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} {conector} {items[-1]}"


def _franjas(franjas: list[Franja]) -> str:
    return _listar([f"{franja.abre:%H:%M} a {franja.cierra:%H:%M}" for franja in franjas], "y")


def fecha_en_palabras(fecha: date) -> str:
    """R40: 'miércoles 14 de octubre', con los diccionarios de formato y sin año."""
    return f"{DIAS[fecha.weekday()]} {fecha.day} de {MESES[fecha.month - 1]}"


def _dias_agrupados(config: ConfigNegocio) -> list[str]:
    # Días seguidos con el mismo horario van juntos: siete renglones iguales no se leen en un WhatsApp
    grupos: list[tuple[int, int, str]] = []
    for dia in range(7):
        texto = _franjas(config.horario.del_dia(dia))
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
    cerramos = [_listar(cerrados)] if cerrados else []
    # R39: se nombran si hay alguno cargado, aunque el local abra los siete días
    if config.feriados:
        cerramos.append("feriados y días no laborables")
    if cerramos:
        texto += f" {', '.join(cerramos).capitalize()} cerramos."
    return texto


def _familias(config: ConfigNegocio) -> str:
    return _listar([producto.familia for producto in config.catalogo])


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
        return (
            f"Los presupuestos te los pasamos dentro de las "
            f"{config.plazo_presupuesto_horas} horas hábiles."
        )
    if tema == "medios_pago":
        return f"Podés pagar con {_listar(config.medios_pago)}."
    if tema == "envio_archivos":
        return (
            "Podés mandarme el archivo por acá, como documento, o mandarlo por "
            f"mail a {config.mail_archivos}. Lo que te quede más cómodo."
        )
    if tema == "catalogo":
        lineas = [f"- {p.familia}: {_listar(p.ejemplos)}." for p in config.catalogo]
        return "Esto es lo que hacemos:\n" + "\n".join(lineas)
    return MENSAJE_NO_ENTENDIDO


def pregunta_por_dato(dato: str, config: ConfigNegocio) -> str:
    """SPECS §6: la repregunta por un campo de CAMPOS_DEL_PEDIDO; el modelo solo elige cuál."""
    if dato == "producto":
        return f"¿Qué necesitás? Hacemos {_familias(config)}."
    return _PREGUNTAS.get(dato, MENSAJE_NO_ENTENDIDO)


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
        # R29: la línea de archivos va acá, entre el diseño y la fecha (tarea 3.3)
        f"- Lo necesitás para el {fecha_en_palabras(pedido.fecha_necesita)}",
        "¿Está todo bien? Confirmame y se lo paso a un asesor, que te va a "
        "pasar el precio y el plazo.",
    ])


def texto_derivacion(motivo: str, config: ConfigNegocio) -> str:
    """SPECS §6: el texto al pasar la charla a un asesor, por motivo."""
    # R38 (tarea 4.4): después del motivo va la frase de horario, "te contacta un asesor" en
    # horario o cuándo reabre fuera, con la config y `ahora`. Nunca "en breve"
    return _MOTIVO_EN_TEXTO[motivo]
