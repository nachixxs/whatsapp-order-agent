"""El pedido: sus campos, sus archivos (R29), las validaciones (R14, R15) y lo que le falta."""

import logging
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, NaiveDatetime, ValidationInfo, field_validator

from app.config import ConfigNegocio, Texto
from app.formato import en_una_linea, para_log
from app.tools import CAMPOS_DEL_PEDIDO, MATERIAL_A_DEFINIR

logger = logging.getLogger(__name__)

NO_SON_NOMBRES = frozenset({"cliente", "usuario", "desconocido", "sin nombre", "no especificado"})
# R35: con los siete campos al tope, el resumen queda lejos de los 4.096 de WhatsApp
TOPE_NOMBRE = 60
TOPE_CAMPO = 200
# R35: con 60 archivos y el tipo al tope, la celda queda lejos de los 50.000 caracteres
TOPE_ARCHIVOS = 60
TOPE_TIPO = 20

TieneDiseno = Literal["si", "no", "requiere_servicio"]


class ArchivoAdjunto(BaseModel):
    """Un adjunto de Chatwoot. El bot no lo baja y nunca guarda su data_url (R29)."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    id_adjunto: int  # attachments[].id
    id_mensaje: int
    tipo: str  # la extensión o el file_type, para mostrar
    tamano: int | None
    hora: NaiveDatetime  # R29: hora de pared del negocio; con y sin zona no se pueden ordenar

    @field_validator("tipo", mode="before")
    @classmethod
    def _tipo_en_una_linea(cls, tipo: object) -> object:
        # R35: un salto de línea armaría otra línea de archivo en la celda
        return en_una_linea(tipo)[:TOPE_TIPO] if isinstance(tipo, str) else tipo


class Pedido(BaseModel):
    """Las columnas de la planilla (SPECS §5) que no completa Python al escribir la fila."""

    # R52: un error de validación no lleva el valor (R26 lo loguea al leer de SQLite)
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True
    )

    nombre_cliente: Texto | None = None
    telefono: Texto  # R13: del contacto de Chatwoot, nunca del modelo
    producto: Texto | None = None
    material: Texto | None = None
    medidas: Texto | None = None
    cantidad: Annotated[int, Field(gt=0)] | None = None
    tiene_diseno: TieneDiseno | None = None
    archivos: list[ArchivoAdjunto] = []  # R29: en orden; solo los suma sumar_archivo
    fecha_necesita: date | None = None

    @field_validator("nombre_cliente", "producto", "material", "medidas", mode="before")
    @classmethod
    def _en_una_linea(cls, valor: object, info: ValidationInfo) -> object:
        # R19: un salto de línea del modelo sería un renglón propio en el resumen, en la voz del bot
        tope = TOPE_NOMBRE if info.field_name == "nombre_cliente" else TOPE_CAMPO
        return en_una_linea(valor)[:tope] if isinstance(valor, str) else valor

    @field_validator("material")
    @classmethod
    def _material_a_definir(cls, material: str | None) -> str | None:
        # R15: "A definir con el asesor." y sus variantes se guardan con el valor exacto
        if material and en_una_linea(material).rstrip(".").casefold() == MATERIAL_A_DEFINIR:
            return MATERIAL_A_DEFINIR
        return material

    @field_validator("nombre_cliente")
    @classmethod
    def _parece_nombre(cls, nombre: str | None) -> str | None:
        # R14: un emoji, un teléfono o "cliente" no son un nombre
        letras = sum(c.isalpha() for c in nombre or "")
        if nombre is not None and (letras < 2 or nombre.casefold() in NO_SON_NOMBRES):
            raise ValueError("no parece un nombre")
        return nombre

    def faltantes(self) -> list[str]:
        # Los que llena el modelo; con los siete está completo
        return [campo for campo in CAMPOS_DEL_PEDIDO if getattr(self, campo) is None]

    @property
    def completo(self) -> bool:
        return not self.faltantes()


def sumar_campos(
    pedido: Pedido, campos: dict[str, object], config: ConfigNegocio, ahora: datetime
) -> tuple[Pedido, list[str]]:
    """R14: suma campo por campo. Devuelve un pedido nuevo y los campos descartados."""
    hoy = ahora.astimezone(config.zona).date()  # R37: el día del negocio, no el del servidor
    catalogo = {producto.id for producto in config.catalogo}
    descartados = []
    for campo, valor in campos.items():
        if valor is None:  # no dicho: no borra lo que ya estaba
            continue
        try:
            pedido = _con_campo(pedido, campo, valor, catalogo, hoy)
        except ValueError:  # la ValidationError de Pydantic también es un ValueError
            logger.info("pedido: campo descartado %s", para_log(campo))  # R52: nunca el valor
            descartados.append(campo)
    return pedido, descartados


def _con_campo(
    pedido: Pedido, campo: str, valor: object, catalogo: set[str], hoy: date
) -> Pedido:
    if campo not in CAMPOS_DEL_PEDIDO:  # R13: el teléfono no lo llena el modelo
        raise ValueError("campo que no llena el modelo")
    nuevo = Pedido.model_validate(pedido.model_dump() | {campo: valor})
    if campo == "producto" and nuevo.producto not in catalogo:
        raise ValueError("producto fuera del catálogo")
    # Solo al sumar: un pedido guardado ayer se sigue leyendo aunque su fecha ya pasó (R26)
    if campo == "fecha_necesita" and nuevo.fecha_necesita and nuevo.fecha_necesita < hoy:
        raise ValueError("fecha pasada")
    return nuevo


def sumar_archivo(pedido: Pedido, archivo: ArchivoAdjunto) -> Pedido | None:
    """R29: un pedido nuevo con el archivo sumado y el diseño en "si". None si ya tiene 60 (R35)."""
    archivos = pedido.archivos
    if not any(_es_el_mismo(previo, archivo) for previo in archivos):
        if len(archivos) >= TOPE_ARCHIVOS:
            return None
        archivos = sorted([*archivos, archivo], key=lambda a: (a.hora, a.id_mensaje))
    return pedido.model_copy(update={"archivos": archivos, "tiene_diseno": "si"})


def _es_el_mismo(previo: ArchivoAdjunto, archivo: ArchivoAdjunto) -> bool:
    # R29: la foto reenviada llega con otro id; sin checksum ni nombre, la delatan tipo y tamaño
    igual = (previo.tipo, previo.tamano) == (archivo.tipo, archivo.tamano)
    return previo.id_adjunto == archivo.id_adjunto or (archivo.tamano is not None and igual)


def texto_de_archivos(pedido: Pedido) -> str:
    """La celda `archivos`: una línea por archivo, sin link ni data_url (R29)."""
    return "\n".join(
        f"{orden}. {archivo.hora:%d/%m %H:%M} · {archivo.tipo} · adjunto #{archivo.id_adjunto}"
        for orden, archivo in enumerate(pedido.archivos, start=1)
    )
