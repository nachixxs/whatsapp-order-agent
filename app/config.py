"""Config del negocio con Pydantic, el reloj del negocio (R37) y el horario con feriados (R39)."""

import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo, available_timezones

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

RAIZ = Path(__file__).resolve().parent.parent
RUTA_POR_DEFECTO = RAIZ / "config" / "negocio.json"
# En el orden de weekday(): 0 es lunes
DIAS = ("lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo")
DIAS_DE_BUSQUEDA = 31  # R39: hasta dónde busca la próxima apertura
AVISO_FERIADOS = timedelta(days=60)  # R39

logger = logging.getLogger(__name__)

Texto = Annotated[str, Field(min_length=1)]


class _Base(BaseModel):
    # R54: un campo mal escrito rompe la carga. Sin el valor en el error (CODESTYLE)
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Franja(_Base):
    abre: time
    cierra: time

    @model_validator(mode="after")
    def _abre_antes_de_cerrar(self) -> "Franja":
        if self.abre.tzinfo is not None or self.cierra.tzinfo is not None:
            raise ValueError("la franja va en hora local, sin zona")
        if self.abre >= self.cierra:  # sin franjas que crucen la medianoche
            raise ValueError("la franja cierra antes de abrir (medianoche se escribe 23:59)")
        return self


class Horario(_Base):
    """Sin defaults: un día cerrado se escribe con la lista vacía."""

    lunes: list[Franja]
    martes: list[Franja]
    miercoles: list[Franja]
    jueves: list[Franja]
    viernes: list[Franja]
    sabado: list[Franja]
    domingo: list[Franja]

    @model_validator(mode="after")
    def _abre_algun_dia(self) -> "Horario":
        if not any(self.del_dia(dia) for dia in range(len(DIAS))):
            raise ValueError("el horario no abre ningún día")
        return self

    def del_dia(self, dia: int) -> list[Franja]:
        return getattr(self, DIAS[dia])


class Producto(_Base):
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]  # va al enum de registrar_pedido
    familia: Texto
    ejemplos: list[Texto] = Field(min_length=1)


class ConfigNegocio(_Base):
    nombre: Texto
    direccion: Texto
    zona_horaria: str  # R37: obligatoria y sin default
    horario: Horario
    feriados: list[date]  # R39: obligatoria y ordenada
    mail_archivos: Texto
    hace_envios: bool
    tiene_estacionamiento: bool
    plazo_presupuesto_horas: int = Field(gt=0)
    medios_pago: list[Texto] = Field(min_length=1)
    catalogo: list[Producto] = Field(min_length=1)
    _ahora_fijo: datetime | None = PrivateAttr(default=None)

    @field_validator("zona_horaria")
    @classmethod
    def _zona_valida(cls, zona: str) -> str:
        # No ZoneInfo(zona): en Windows acepta "utc", que en el contenedor Linux no existe
        if zona not in available_timezones():
            raise ValueError("zona_horaria no está en la base tz")
        return zona

    @field_validator("feriados")
    @classmethod
    def _feriados_ordenados(cls, feriados: list[date]) -> list[date]:
        # R39: un repetido o un desorden suele ser el typo de otra fecha
        if any(anterior >= siguiente for anterior, siguiente in zip(feriados, feriados[1:])):
            raise ValueError("feriados desordenados o repetidos")
        return feriados

    @field_validator("catalogo")
    @classmethod
    def _ids_unicos(cls, catalogo: list[Producto]) -> list[Producto]:
        ids = [producto.id for producto in catalogo]
        if len(set(ids)) != len(ids):
            raise ValueError("ids repetidos en el catálogo")
        return catalogo

    @property
    def zona(self) -> ZoneInfo:
        return ZoneInfo(self.zona_horaria)

    def ahora(self) -> datetime:
        """R37: el único reloj de app/. Con zona, la del negocio."""
        return (self._ahora_fijo or datetime.now(self.zona)).astimezone(self.zona)

    def fijar_ahora(self, instante: datetime) -> None:
        """Para tests: desde acá, ahora() devuelve este instante."""
        self._ahora_fijo = self._en_zona(instante)

    def esta_abierto(self, ahora: datetime) -> bool:
        local = self._en_zona(ahora)
        if local.date() in self.feriados:  # R39: un feriado está cerrado todo el día
            return False
        hora = local.time()
        franjas = self.horario.del_dia(local.weekday())
        return any(franja.abre <= hora < franja.cierra for franja in franjas)

    def proxima_apertura(self, ahora: datetime) -> datetime | None:
        """R38, R39: la primera apertura después de ahora, nunca en feriado; None si no hay en 31 días."""
        local = self._en_zona(ahora)
        for adelanto in range(DIAS_DE_BUSQUEDA + 1):  # hoy y los 31 días que siguen
            dia = local.date() + timedelta(days=adelanto)
            if dia in self.feriados:
                continue
            franjas = self.horario.del_dia(dia.weekday())
            aperturas = [datetime.combine(dia, franja.abre, self.zona) for franja in franjas]
            if siguientes := [apertura for apertura in aperturas if apertura > local]:
                return min(siguientes)  # min: las franjas del día pueden venir en cualquier orden
        return None

    def feriados_por_vencer(self, ahora: datetime) -> bool:
        """R39: no hay feriados cargados o el último está a menos de 60 días."""
        return not self.feriados or self.feriados[-1] - self._en_zona(ahora).date() < AVISO_FERIADOS

    def _en_zona(self, instante: datetime) -> datetime:
        if instante.tzinfo is None:  # R37: una hora sin zona se leería como la del servidor
            raise ValueError("hora sin zona: usar config.ahora()")
        return instante.astimezone(self.zona)


def cargar_config(ruta: Path = RUTA_POR_DEFECTO) -> ConfigNegocio:
    if not ruta.is_file():
        raise FileNotFoundError(f"Falta la config del negocio: {ruta}")
    config = ConfigNegocio.model_validate_json(ruta.read_text(encoding="utf-8"))  # R40
    if config.feriados_por_vencer(config.ahora()):  # R39
        logger.warning("config: cargar feriados nuevos, el último está a menos de 60 días o no hay")
    return config
