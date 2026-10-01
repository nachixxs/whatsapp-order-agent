"""Primer contacto y nombre: el registro en los atributos del contacto de Chatwoot, sin red (SPECS §13)."""

from dataclasses import dataclass
from datetime import datetime

from app.chatwoot import Contacto
from app.formato import en_una_linea, solo_digitos
from app.memoria import TTL_CHARLA
from app.pedidos import TOPE_NOMBRE


@dataclass(frozen=True)
class Registro:  # R45: los atributos del contacto, con sus mismos nombres
    primer_contacto: datetime | None  # None: contacto nuevo
    nombre_preguntado: bool
    nombre_cliente: str | None


@dataclass(frozen=True)
class Plan:
    escribir: dict[str, object] | None  # None: no se escribe nada
    preguntar: bool  # R41: la pregunta va solo si la escritura salió


def nombre_limpio(valor: object) -> str | None:
    if not isinstance(valor, str):
        return None
    return en_una_linea(valor)[:TOPE_NOMBRE].rstrip() or None  # R44: el mismo tope que el del pedido


def nombre_del_perfil(contacto: Contacto) -> str | None:
    """R43: sin perfil, Chatwoot le pone el teléfono de nombre; el teléfono nunca va a la API (R13)."""
    nombre, telefono = nombre_limpio(contacto.nombre), solo_digitos(contacto.telefono or "")
    return None if nombre and telefono and solo_digitos(nombre) == telefono else nombre


def _fecha(valor: object) -> datetime | None:
    try:
        fecha = datetime.fromisoformat(valor)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return fecha if fecha.utcoffset() is not None else None  # R42: sin zona, la ventana no se puede medir


def leer_registro(atributos: object, escritos: dict[str, object] | None = None) -> Registro | None:
    """R41, R45: nunca lanza. None es sin dato; un valor ilegible cuenta como ausente. Lo que la charla ya
    escribió (`escritos`) le gana al payload, que pudo armarse antes del alta (R42)."""
    if not isinstance(atributos, dict):
        return None
    atributos = atributos | (escritos or {})
    return Registro(
        primer_contacto=_fecha(atributos.get("primer_contacto")),
        nombre_preguntado=atributos.get("nombre_preguntado") is True,
        nombre_cliente=nombre_limpio(atributos.get("nombre_cliente")),
    )


def atributos_del_alta(ahora: datetime, *, preguntado: bool, nombre: str | None = None) -> dict[str, object]:
    if ahora.utcoffset() is None:  # R37: una hora sin zona se leería como la del servidor
        raise ValueError("hora sin zona: usar config.ahora()")
    alta = {"primer_contacto": ahora.isoformat(), "nombre_preguntado": preguntado}
    return alta | (atributos_del_nombre(nombre) if nombre else {})


def atributos_del_nombre(nombre: str) -> dict[str, object]:
    return {"nombre_cliente": nombre}


def pregunta_viva(registro: Registro | None, ahora: datetime) -> bool:
    """R42: la pregunta del alta vale lo que dura la charla (R22)."""
    if registro is None or registro.primer_contacto is None:
        return False
    return registro.nombre_preguntado and ahora - registro.primer_contacto <= TTL_CHARLA


def _mismo(nombre: str | None, otro: str | None) -> bool:
    return nombre is not None and otro is not None and nombre.casefold() == otro.casefold()


def plan_primer_contacto(
    registro: Registro | None, ahora: datetime, *, error: bool, respuesta_vacia: bool, nombre_dicho: str | None,
    nombre_perfil: str | None, repregunta_del_nombre: bool, nombre_confirmado: str | None,
) -> Plan:
    """R41 a R45. Los nombres llegan limpios (nombre_limpio, R44); el del perfil no es uno dicho (R43)."""
    if registro is None or error or respuesta_vacia:  # R41, R44
        return Plan(None, False)
    dicho = None if _mismo(nombre_dicho, nombre_perfil) else nombre_dicho
    if registro.primer_contacto is None:  # R42: la repregunta del nombre ya es la pregunta
        if dicho or nombre_confirmado:  # R45: el que dijo, o el del pedido que confirmó en este lote
            return Plan(atributos_del_alta(ahora, preguntado=False, nombre=dicho or nombre_confirmado), False)
        return Plan(atributos_del_alta(ahora, preguntado=True), not repregunta_del_nombre)
    nombre = dicho if pregunta_viva(registro, ahora) else None  # R42
    # R45: el del pedido confirmado; R43: el del perfil no pisa uno registrado
    if not nombre and not (registro.nombre_cliente and _mismo(nombre_confirmado, nombre_perfil)):
        nombre = nombre_confirmado
    if not nombre or _mismo(nombre, registro.nombre_cliente):
        return Plan(None, False)
    return Plan(atributos_del_nombre(nombre), False)
