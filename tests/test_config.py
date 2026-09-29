import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.config import RAIZ, ConfigNegocio, cargar_config

RUTA_EJEMPLO = RAIZ / "config" / "negocio.ejemplo.json"
BUENOS_AIRES = ZoneInfo("America/Argentina/Buenos_Aires")
RELOJES_PROHIBIDOS = ("datetime.now(", "datetime.utcnow(", "datetime.today(", "date.today(")


def _datos() -> dict:
    return json.loads(RUTA_EJEMPLO.read_text(encoding="utf-8"))


def _en_ba(dia: int, hora: int, minuto: int = 0) -> datetime:
    return datetime(2026, 10, dia, hora, minuto, tzinfo=BUENOS_AIRES)


@pytest.fixture
def config() -> ConfigNegocio:
    return cargar_config(RUTA_EJEMPLO)


def test_carga_el_ejemplo(config: ConfigNegocio) -> None:
    """R40: el JSON se lee en utf-8 (las tildes llegan enteras) y trae el catálogo de SPECS §4."""
    assert config.nombre == "Imprenta Ejemplo"
    assert config.zona_horaria == "America/Argentina/Buenos_Aires"
    ids = [producto.id for producto in config.catalogo]
    assert ids == ["impresion_digital", "gran_formato", "rotulacion", "sellos", "acabados"]
    assert config.catalogo[0].familia == "Impresión digital"
    assert config.horario.domingo == []


def test_falta_el_archivo_dice_cual(tmp_path: Path) -> None:
    ruta = tmp_path / "negocio.json"
    with pytest.raises(FileNotFoundError, match="negocio.json"):
        cargar_config(ruta)


@pytest.mark.parametrize("donde", [(), ("horario",), ("horario", "lunes", 0), ("catalogo", 0)])
def test_campo_extra_rompe_la_carga(donde: tuple) -> None:
    """R54: un campo mal escrito rompe la carga, también adentro del horario y del catálogo."""
    datos = _datos()
    destino = datos
    for clave in donde:
        destino = destino[clave]
    destino["campo_mal_escrito"] = 1
    with pytest.raises(ValidationError) as error:
        ConfigNegocio.model_validate(datos)
    assert [e["type"] for e in error.value.errors()] == ["extra_forbidden"]


def _rompe_en(campo: str, datos: dict) -> None:
    with pytest.raises(ValidationError) as error:
        ConfigNegocio.model_validate(datos)
    assert {e["loc"][0] for e in error.value.errors()} == {campo}


@pytest.mark.parametrize("campo", ["zona_horaria", "feriados"])
def test_zona_o_feriados_ausentes_rompen_la_carga(campo: str) -> None:
    """R37, R39: zona_horaria y feriados son obligatorias, sin default."""
    datos = _datos()
    del datos[campo]
    _rompe_en(campo, datos)


@pytest.mark.parametrize("zona", ["UTC-3", "utc", "America/Buenos_Aires_", "../etc/passwd", ""])
def test_zona_invalida_rompe_la_carga(zona: str) -> None:
    """R37: la zona se valida al cargar ("utc" en minúscula pasa en Windows y no en Linux)."""
    _rompe_en("zona_horaria", _datos() | {"zona_horaria": zona})


@pytest.mark.parametrize(
    "feriados", [["2026-12-25", "2026-12-08"], ["2026-12-08", "2026-12-08"], ["no-es-fecha"]]
)
def test_feriados_desordenados_o_invalidos_rompen_la_carga(feriados: list[str]) -> None:
    """R39: la lista de feriados es de fechas ISO, ordenada y sin repetidos."""
    _rompe_en("feriados", _datos() | {"feriados": feriados})


@pytest.mark.parametrize(
    ("campo", "valor"),
    [
        ("horario", {"lunes": [{"abre": "18:00", "cierra": "09:00"}]}),
        ("horario", {"lunes": [{"abre": "09:00-03:00", "cierra": "18:00"}]}),
        ("catalogo", [{"id": "sellos", "familia": "A", "ejemplos": ["a"]}] * 2),
        ("catalogo", [{"id": "Sellos de goma", "familia": "A", "ejemplos": ["a"]}]),
    ],
)
def test_config_inconsistente_rompe_la_carga(campo: str, valor: object) -> None:
    """R54: franja invertida o con zona, id repetido o que no sirve de enum: rompe al cargar."""
    datos = _datos()
    datos[campo] = datos[campo] | valor if isinstance(valor, dict) else valor
    _rompe_en(campo, datos)


@pytest.mark.parametrize(
    ("instante", "abierto"),
    [
        (_en_ba(5, 10), True),  # lunes
        (_en_ba(5, 8, 59), False),
        (_en_ba(5, 18), False),  # el cierre ya es cerrado
        (_en_ba(10, 12, 59), True),  # sábado
        (_en_ba(10, 13), False),
        (_en_ba(11, 10), False),  # domingo
        (_en_ba(12, 10), False),  # lunes feriado
        (datetime(2026, 10, 5, 20, 0, tzinfo=UTC), True),  # 17:00 en el local
    ],
)
def test_esta_abierto(config: ConfigNegocio, instante: datetime, abierto: bool) -> None:
    """R37, R39: horario por día en la zona del negocio; un feriado cierra todo el día."""
    assert config.esta_abierto(instante) is abierto


def test_hora_sin_zona_se_rechaza(config: ConfigNegocio) -> None:
    """R37: una hora sin zona se leería como la del servidor."""
    with pytest.raises(ValueError):
        config.esta_abierto(datetime(2026, 10, 5, 10, 0))
    with pytest.raises(ValueError):
        config.fijar_ahora(datetime(2026, 10, 5, 10, 0))


def test_ahora_en_la_zona_del_negocio(config: ConfigNegocio) -> None:
    """R37: ahora() es la hora real, con la zona de la config."""
    ahora = config.ahora()
    assert str(ahora.tzinfo) == "America/Argentina/Buenos_Aires"
    assert ahora.utcoffset() == timedelta(hours=-3)
    assert abs(ahora - datetime.now(UTC)) < timedelta(seconds=5)


def test_ahora_se_fija_en_tests(config: ConfigNegocio) -> None:
    """R37: fijar_ahora congela el reloj de esa config (y no de otra), en la zona del negocio."""
    config.fijar_ahora(datetime(2026, 10, 5, 13, 0, tzinfo=UTC))
    assert config.ahora() == _en_ba(5, 10)
    assert config.ahora().hour == 10
    otra = cargar_config(RUTA_EJEMPLO)
    assert abs(otra.ahora() - datetime.now(UTC)) < timedelta(seconds=5)


def test_ningun_reloj_fuera_de_config() -> None:
    """R37: datetime.now(), date.today() y parecidos solo aparecen en app/config.py."""
    archivos = sorted((RAIZ / "app").rglob("*.py"))
    assert RAIZ / "app" / "config.py" in archivos
    culpables = [
        f"{archivo.relative_to(RAIZ)}: {reloj}"
        for archivo in archivos
        if archivo != RAIZ / "app" / "config.py"
        for reloj in RELOJES_PROHIBIDOS
        if reloj in archivo.read_text(encoding="utf-8")
    ]
    assert culpables == []
