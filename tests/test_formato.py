import importlib
import re

import pytest

from app import formato
from app.formato import (
    DIAS,
    ILEGIBLE,
    MESES,
    alias_conversacion,
    en_una_linea,
    para_log,
    solo_digitos,
)

TELEFONO = "+54 9 11 5555-0000"
ALIAS = re.compile(r"[0-9a-f]{12}")


class StrRoto:
    def __str__(self) -> str:
        raise RuntimeError("no se puede mostrar")


@pytest.mark.parametrize(
    "separador", ["\n", "\r", "\r\n", "\x0b", "\x0c", "\x1c", "\x85", " ", " "]
)
def test_para_log_ningun_salto_parte_la_linea(separador: str) -> None:
    """R52: un salto de línea, ASCII o Unicode, no parte la línea del log."""
    resultado = para_log(f"tool{separador}INFO falso")
    assert resultado == "tool" + " " * len(separador) + "INFO falso"
    assert len(resultado.splitlines()) == 1


def test_para_log_tab_y_nulo_son_espacios() -> None:
    """R52: un tab o un byte nulo se reemplazan por un espacio."""
    assert para_log("a\tb\x00c") == "a b c"


def test_para_log_neutraliza_secuencia_ansi() -> None:
    """R52: una secuencia ANSI llega sin el ESC: no colorea ni mueve la terminal."""
    assert para_log("\x1b[31mrojo\x1b[0m") == " [31mrojo [0m"


def test_para_log_neutraliza_override_bidi() -> None:
    """R52: un carácter de formato (override bidi) no reordena la línea."""
    assert para_log("abc‮def") == "abc def"


def test_para_log_surrogate_suelto() -> None:
    """R52: un surrogate suelto se reemplaza y el resultado se codifica en UTF-8."""
    resultado = para_log("a\ud83db")
    assert resultado.encode("utf-8") == b"a b"


def test_para_log_texto_comun_intacto() -> None:
    """R52: un texto sin controles, con tildes y eñe, pasa igual."""
    assert para_log("registrar_pedido: diseño válido") == "registrar_pedido: diseño válido"


def test_para_log_64_justos_no_se_cortan() -> None:
    """R52: un texto de 64 caracteres justos no lleva marca de corte."""
    assert para_log("x" * 64) == "x" * 64


@pytest.mark.parametrize("largo", [65, 10_000])
def test_para_log_corta_a_64(largo: int) -> None:
    """R52: un texto largo se corta a 64 caracteres en total, con la marca del corte."""
    resultado = para_log("x" * largo)
    assert resultado == "x" * 63 + "…"
    assert len(resultado) == 64


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [(None, "None"), (42, "42"), (b"a\nb", "b'a\\nb'"), (["x"], "['x']")],
)
def test_para_log_entradas_no_str(valor: object, esperado: str) -> None:
    """R52: None, números, bytes y objetos se convierten a str sin lanzar."""
    assert para_log(valor) == esperado


def test_para_log_str_que_lanza() -> None:
    """R52: un objeto cuyo __str__ lanza no tumba el log."""
    assert para_log(StrRoto()) == ILEGIBLE


def test_para_log_entero_que_str_rechaza() -> None:
    """R52: un entero de más de 4.300 dígitos, que str() rechaza, no tumba el log."""
    assert para_log(10**5000) == ILEGIBLE


def test_alias_estable_en_el_proceso() -> None:
    """R52: la misma conversación da siempre el mismo alias dentro del proceso."""
    assert alias_conversacion(TELEFONO) == alias_conversacion(TELEFONO)


@pytest.mark.parametrize("identificador", [TELEFONO, "7", "x" * 500])
def test_alias_no_expone_el_input(identificador: str) -> None:
    """R52: el alias son 12 hex, largo fijo sea cual sea el input, sin el teléfono adentro."""
    alias = alias_conversacion(identificador)
    assert ALIAS.fullmatch(alias)
    assert solo_digitos(TELEFONO) not in alias


def test_alias_distingue_conversaciones() -> None:
    """R52: dos teléfonos que difieren en un dígito dan alias distintos."""
    assert alias_conversacion(TELEFONO) != alias_conversacion("+54 9 11 5555-0001")


def test_alias_cambia_con_la_sal_de_otro_proceso() -> None:
    """R52: la sal es aleatoria por proceso: al recargar el módulo, el teléfono da otro alias."""
    antes = alias_conversacion(TELEFONO)
    importlib.reload(formato)
    assert formato.alias_conversacion(TELEFONO) != antes


@pytest.mark.parametrize("identificador", [None, 12345, b"\x00", "a\ud800b"])
def test_alias_entradas_no_str(identificador: object) -> None:
    """R52: el alias no lanza con None, números, bytes o un surrogate suelto."""
    assert ALIAS.fullmatch(alias_conversacion(identificador))


def test_alias_str_que_lanza() -> None:
    """R52: un identificador cuyo __str__ lanza no tumba el log."""
    assert alias_conversacion(StrRoto()) == ILEGIBLE


def test_en_una_linea_saltos_windows_tabs_y_espacios() -> None:
    """R35: saltos de Windows, tabs y espacios repetidos quedan en un solo espacio."""
    texto = "500 tarjetas\r\n\r\npapel\tilustración   300 g"
    assert en_una_linea(texto) == "500 tarjetas papel ilustración 300 g"


def test_en_una_linea_recorta_bordes() -> None:
    """R35: los espacios y saltos de los bordes se recortan."""
    assert en_una_linea(" \r\n\t hola \n ") == "hola"


def test_en_una_linea_solo_espacios_queda_vacio() -> None:
    """R35: un texto hecho solo de espacios y saltos queda vacío."""
    assert en_una_linea(" \r\n\t ") == ""


def test_en_una_linea_separador_unicode() -> None:
    """R35: un separador de línea Unicode también pasa a espacio."""
    assert en_una_linea("línea uno línea dos") == "línea uno línea dos"


@pytest.mark.parametrize(
    ("telefono", "esperado"),
    [(TELEFONO, "5491155550000"), ("(011) 5555.0000", "01155550000"), ("", "")],
)
def test_solo_digitos_descarta_signos(telefono: str, esperado: str) -> None:
    """R46: el +, los espacios, guiones, paréntesis y puntos se descartan."""
    assert solo_digitos(telefono) == esperado


def test_solo_digitos_solo_ascii() -> None:
    """R46: un superíndice o un dígito de otro alfabeto no entra al teléfono."""
    assert solo_digitos("+54 9 11 5555-000²٣") == "549115555000"


def test_dias_y_meses_en_castellano() -> None:
    """R40: días en el orden de weekday() y meses de enero a diciembre, con tildes y sin locale."""
    assert len(DIAS) == 7 and len(MESES) == 12
    assert (DIAS[0], DIAS[2], DIAS[5], DIAS[6]) == ("lunes", "miércoles", "sábado", "domingo")
    assert (MESES[0], MESES[8], MESES[11]) == ("enero", "septiembre", "diciembre")
