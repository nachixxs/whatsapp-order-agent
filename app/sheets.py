"""Escritura de un pedido como fila de la pestaña Pedidos de Google Sheets."""

import logging
import os
from collections.abc import Mapping
from typing import Any

import gspread

from app.formato import para_log

logger = logging.getLogger(__name__)

# R9: el orden exacto de SPECS §5; append_row escribe por posición
COLUMNAS: tuple[str, ...] = (
    "fecha_ingreso", "nombre_cliente", "telefono", "producto", "material", "medidas",
    "cantidad", "tiene_diseno", "archivos", "fecha_necesita", "anticipo",
)
PESTANA = "Pedidos"
ALCANCES = ["https://www.googleapis.com/auth/spreadsheets"]
# R10: (conexión, lectura). gspread lo pasa como timeout= a cada request, y google-auth al refresh del token
TOPE = (5, 180)
LARGO_MAXIMO_DE_UNA_RUTA = 260
FALTAN_CREDENCIALES = "faltan GOOGLE_CREDENCIALES o SHEET_ID en el entorno"
CREDENCIAL_PEGADA = "GOOGLE_CREDENCIALES tiene que ser la ruta al JSON, no su contenido"
NO_SE_ESCRIBIO = "no se pudo escribir la fila en la planilla"
RANGO_DESCONOCIDO = "desconocido"


class ErrorPlanilla(Exception):
    """La fila no quedó escrita. Mensaje fijo: ni valores del pedido ni la credencial (R52, R53)."""


def _es_contenido(ruta: str) -> bool:
    # R53: el error de "no existe el archivo" citaría el contenido pegado
    return ruta.startswith("{") or "PRIVATE KEY" in ruta or "\n" in ruta or len(ruta) > LARGO_MAXIMO_DE_UNA_RUTA


def _abrir_hoja() -> gspread.Worksheet:
    """R53: la credencial se lee recién acá, en la primera escritura."""
    ruta = os.environ.get("GOOGLE_CREDENCIALES", "").strip()
    sheet_id = os.environ.get("SHEET_ID", "").strip()
    if not ruta or not sheet_id:
        raise ErrorPlanilla(FALTAN_CREDENCIALES) from None
    if _es_contenido(ruta):
        raise ErrorPlanilla(CREDENCIAL_PEGADA) from None
    for nombre in ("google", "urllib3"):  # R52: en DEBUG vuelcan los requests (la fila, la URL con el id)
        if logging.getLogger(nombre).getEffectiveLevel() < logging.INFO:
            logging.getLogger(nombre).setLevel(logging.INFO)
    # R10: HTTPClient no reintenta; BackOffHTTPClient reintenta 429 y 5xx, y eso deja una segunda fila
    cliente = gspread.service_account(filename=ruta, scopes=ALCANCES, http_client=gspread.HTTPClient)
    cliente.set_timeout(TOPE)
    return cliente.open_by_key(sheet_id).worksheet(PESTANA)


def _rango(respuesta: Any) -> str:
    try:
        return para_log(respuesta["updates"]["updatedRange"])
    except Exception:  # R3: la fila ya está escrita; una respuesta con otra forma no la desdice
        return RANGO_DESCONOCIDO


class Planilla:
    def __init__(self, hoja: Any = None) -> None:
        self._hoja = hoja  # sin hoja, se abre en la primera escritura (R53)

    def escribir_fila(self, valores: Mapping[str, str]) -> None:
        """ErrorPlanilla si la fila no quedó escrita (R2); ValueError si la llamada está mal armada."""
        if set(valores) != set(COLUMNAS[:-1]):
            raise ValueError("escribir_fila espera las columnas de COLUMNAS menos anticipo")
        if not valores["fecha_ingreso"].strip():
            raise ValueError("fecha_ingreso vacía: la pone quien llama, con config.ahora() (R9)")
        fila = [valores[columna] for columna in COLUMNAS[:-1]] + [""]  # R9: anticipo siempre vacío
        try:
            if self._hoja is None:
                self._hoja = _abrir_hoja()
            respuesta = self._hoja.append_row(fila, value_input_option="RAW")  # R9; R10: una sola vez
        except ErrorPlanilla as error:
            logger.error("planilla: %s", error)  # mensaje fijo
            raise
        except Exception as error:  # R2: cualquier falla de la credencial, la red o la API
            tipo = type(error).__name__
        else:
            logger.info("planilla: fila escrita rango=%s", _rango(respuesta))
            return
        logger.error("planilla: fallo al escribir tipo=%s", tipo)  # R52: el tipo, nunca el mensaje
        raise ErrorPlanilla(NO_SE_ESCRIBIO) from None  # R53: afuera del except, sin __context__
