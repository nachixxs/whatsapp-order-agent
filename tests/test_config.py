import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.config import DIAS, RAIZ, ConfigNegocio, cargar_config

RUTA_EJEMPLO = RAIZ / "config" / "negocio.ejemplo.json"
BUENOS_AIRES = ZoneInfo("America/Argentina/Buenos_Aires")
RELOJES_PROHIBIDOS = ("datetime.now(", "datetime.utcnow(", "datetime.today(", "date.today(")
MANANA_Y_TARDE = [{"abre": "09:00", "cierra": "13:00"}, {"abre": "14:00", "cierra": "18:00"}]


def _datos() -> dict:
    return json.loads(RUTA_EJEMPLO.read_text(encoding="utf-8"))


def _en_ba(dia: int, hora: int, minuto: int = 0, mes: int = 10) -> datetime:
    return datetime(2026, mes, dia, hora, minuto, tzinfo=BUENOS_AIRES)


@pytest.fixture
def config() -> ConfigNegocio:
    return cargar_config(RUTA_EJEMPLO)


@pytest.fixture
def partido() -> ConfigNegocio:
    """Lunes a viernes de 9 a 13 y de 14 a 18, fin de semana cerrado, feriados del ejemplo."""
    horario = {dia: MANANA_Y_TARDE for dia in DIAS[:5]} | {"sabado": [], "domingo": []}
    return ConfigNegocio.model_validate(_datos() | {"horario": horario})


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
        ("horario", {"lunes": [{"abre": "18:00", "cierra": "00:00"}]}),  # medianoche es 23:59
        ("horario", {dia: [] for dia in DIAS}),
        ("catalogo", [{"id": "sellos", "familia": "A", "ejemplos": ["a"]}] * 2),
        ("catalogo", [{"id": "Sellos de goma", "familia": "A", "ejemplos": ["a"]}]),
    ],
)
def test_config_inconsistente_rompe_la_carga(campo: str, valor: object) -> None:
    """R54: franja invertida o con zona, ningún día abierto, id repetido o que no sirve de enum."""
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


@pytest.mark.parametrize(("dia", "abierto"), [(7, True), (8, False), (9, True)])
def test_feriado_cierra_solo_ese_dia(config: ConfigNegocio, dia: int, abierto: bool) -> None:
    """R39: el martes 8 de diciembre, feriado, cierra; el lunes antes y el miércoles después abren."""
    assert config.esta_abierto(_en_ba(dia, 10, mes=12)) is abierto


@pytest.mark.parametrize(
    ("instante", "apertura"),
    [
        (_en_ba(6, 8), _en_ba(6, 9)),  # martes antes de abrir
        (_en_ba(6, 13, 30), _en_ba(6, 14)),  # entre la franja de la mañana y la de la tarde
        (_en_ba(6, 10), _en_ba(6, 14)),  # abierto: la que sigue, nunca ahora
        (_en_ba(6, 18), _en_ba(7, 9)),  # en el cierre
        (_en_ba(2, 20), _en_ba(5, 9)),  # viernes a la noche, sábado y domingo cerrados
        (_en_ba(7, 19, mes=12), _en_ba(9, 9, mes=12)),  # lunes antes del martes 8, feriado
        (_en_ba(12, 8), _en_ba(13, 9)),  # el lunes 12, feriado, antes de la hora de abrir
        (datetime(2026, 10, 6, 11, 0, tzinfo=UTC), _en_ba(6, 9)),  # 08:00 en el local
    ],
)
def test_proxima_apertura(partido: ConfigNegocio, instante: datetime, apertura: datetime) -> None:
    """R38, R39: la próxima apertura, en la zona del negocio, salta el fin de semana y los feriados."""
    resultado = partido.proxima_apertura(instante)
    assert resultado == apertura
    assert str(resultado.tzinfo) == "America/Argentina/Buenos_Aires"


@pytest.mark.parametrize(("feriados", "apertura"), [([], _en_ba(5, 9, mes=11)), (["2026-11-05"], None)])
def test_proxima_apertura_busca_hasta_31_dias(feriados: list[str], apertura: datetime | None) -> None:
    """R39: desde el lunes 5/10, el jueves 5/11 (a 31 días) se encuentra; el viernes 6/11 (a 32) ya no."""
    octubre = [f"2026-10-{dia:02d}" for dia in (8, 9, 15, 16, 22, 23, 29, 30)]  # sus jueves y viernes
    franja = [{"abre": "09:00", "cierra": "18:00"}]
    horario = {dia: [] for dia in DIAS} | {"jueves": franja, "viernes": franja}
    config = ConfigNegocio.model_validate(_datos() | {"horario": horario, "feriados": octubre + feriados})
    assert config.proxima_apertura(_en_ba(5, 20)) == apertura


@pytest.mark.parametrize(
    ("instante", "por_vencer"),
    [
        (datetime(2027, 10, 26, 10, 0, tzinfo=BUENOS_AIRES), False),  # el 25/12/2027 está a 60 días
        (datetime(2027, 10, 27, 2, 0, tzinfo=UTC), False),  # todavía es el 26 en el local
        (datetime(2027, 10, 27, 10, 0, tzinfo=BUENOS_AIRES), True),
        (datetime(2028, 1, 2, 10, 0, tzinfo=BUENOS_AIRES), True),  # el último ya pasó
    ],
)
def test_feriados_por_vencer(config: ConfigNegocio, instante: datetime, por_vencer: bool) -> None:
    """R39: avisa si el último feriado cargado está a menos de 60 días, contados en la zona del negocio."""
    assert config.feriados_por_vencer(instante) is por_vencer


@pytest.mark.parametrize(("dias_al_ultimo", "avisos"), [(None, 1), (30, 1), (365, 0)])
def test_aviso_de_feriados_al_cargar(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, dias_al_ultimo: int | None, avisos: int
) -> None:
    """R39: al cargar sale un WARNING si no hay feriados o el último está a menos de 60 días."""
    # cargar_config usa el reloj real: las fechas se arman relativas a hoy
    hoy = datetime.now(BUENOS_AIRES).date()
    feriados = [] if dias_al_ultimo is None else [str(hoy + timedelta(days=dias_al_ultimo))]
    ruta = tmp_path / "negocio.json"
    ruta.write_text(json.dumps(_datos() | {"feriados": feriados}), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="app.config"):
        cargar_config(ruta)
    assert [r.levelname for r in caplog.records if r.name == "app.config"] == ["WARNING"] * avisos


@pytest.mark.parametrize("metodo", ["esta_abierto", "fijar_ahora", "proxima_apertura", "feriados_por_vencer"])
def test_hora_sin_zona_se_rechaza(config: ConfigNegocio, metodo: str) -> None:
    """R37: una hora sin zona se leería como la del servidor."""
    with pytest.raises(ValueError):
        getattr(config, metodo)(datetime(2026, 10, 5, 10, 0))


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
