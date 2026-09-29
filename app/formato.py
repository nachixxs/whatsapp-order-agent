"""Utilidades de texto puras: logs sin datos del cliente, textos en una línea y teléfonos."""

import hashlib
import secrets
import unicodedata

TOPE_LOG = 64
ILEGIBLE = "<ilegible>"
_SAL = secrets.token_bytes(16)  # R52: alias con sal por proceso


def _visible(caracter: str) -> str:
    categoria = unicodedata.category(caracter)
    # R52: controles, formato (bidi), surrogates sueltos y separadores de línea Unicode
    return " " if categoria[0] == "C" or categoria in ("Zl", "Zp") else caracter


def para_log(valor: object) -> str:
    try:
        texto = "".join(_visible(c) for c in str(valor)[: TOPE_LOG + 1])
    except Exception:  # R52: loguear nunca lanza, ni con un __str__ roto
        return ILEGIBLE
    return texto if len(texto) <= TOPE_LOG else texto[: TOPE_LOG - 1] + "…"


def alias_conversacion(identificador: object) -> str:
    """12 hex estables dentro del proceso; sin la sal no se vuelve al id."""
    try:
        datos = str(identificador).encode("utf-8", "surrogatepass")
        return hashlib.blake2b(datos, key=_SAL, digest_size=6).hexdigest()
    except Exception:  # R52: loguear nunca lanza, ni con un __str__ roto
        return ILEGIBLE


def en_una_linea(texto: str) -> str:
    return " ".join(texto.split())


def solo_digitos(telefono: str) -> str:
    # isdigit() también acepta "²" y dígitos de otros alfabetos
    return "".join(c for c in telefono if c in "0123456789")
