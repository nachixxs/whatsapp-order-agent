import logging
from pathlib import Path
from typing import Any

import gspread
import pytest

from app.sheets import (
    COLUMNAS,
    CREDENCIAL_PEGADA,
    FALTAN_CREDENCIALES,
    NO_SE_ESCRIBIO,
    RANGO_DESCONOCIDO,
    ErrorPlanilla,
    Planilla,
)
from tests.conftest import TELEFONO

CENTINELA = "Centinela-7Q"  # un valor que nunca puede aparecer en un log ni en un error
FORMULA = '=IMPORTXML("https://ejemplo.invalid/", "//a")'
VALORES = {
    "fecha_ingreso": "2026-10-06 10:00",
    "nombre_cliente": CENTINELA,
    "telefono": TELEFONO,
    "producto": "impresion_digital",
    "material": FORMULA,
    "medidas": "9x5 cm",
    "cantidad": "100",
    "tiene_diseno": "si",
    "archivos": "",
    "fecha_necesita": "2026-10-20",
}
RANGO = "'Pedidos'!A2:K2"
ESCRITA = {"updates": {"updatedRange": RANGO}}
RUTA = "credenciales/cuenta-de-prueba.json"


class HojaFalsa:
    """La pestaña Pedidos: anota cada fila y contesta lo que se le pida."""

    def __init__(self, respuesta: Any = ESCRITA, error: Exception | None = None) -> None:
        self.respuesta, self.error = respuesta, error
        self.filas: list[list[str]] = []

    def append_row(self, values: list[str], **opciones: Any) -> Any:
        self.filas.append(values)
        if self.error is not None:
            raise self.error
        return self.respuesta


class _Respuesta:
    """Lo que gspread lee de un requests.Response."""

    def __init__(self, estado: int, cuerpo: Any) -> None:
        self.status_code, self.ok, self.text, self._cuerpo = estado, estado < 400, "", cuerpo

    def json(self) -> Any:
        return self._cuerpo


def _metadata() -> dict[str, Any]:
    pestana = {"sheetId": 0, "title": "Pedidos", "index": 0, "gridProperties": {"rowCount": 1000, "columnCount": 11}}
    return {"properties": {"title": "Planilla de prueba"}, "sheets": [{"properties": pestana}]}


class SesionFalsa:
    """La sesión HTTP de gspread: anota cada request y contesta como Google, sin red."""

    def __init__(self, append: _Respuesta | Exception) -> None:
        self.append = append
        self.requests: list[dict[str, Any]] = []

    def request(self, **request: Any) -> _Respuesta:
        self.requests.append(request)
        if request["method"] == "get":
            return _Respuesta(200, _metadata())
        if isinstance(self.append, Exception):
            raise self.append
        return self.append

    def posts(self) -> list[dict[str, Any]]:
        return [request for request in self.requests if request["method"] == "post"]


def _conectar(
    monkeypatch: pytest.MonkeyPatch, append: _Respuesta | Exception = _Respuesta(200, ESCRITA)
) -> tuple[SesionFalsa, list[dict[str, Any]]]:
    """gspread de verdad sobre una sesión falsa; devuelve la sesión y cada llamada a service_account."""
    monkeypatch.setenv("GOOGLE_CREDENCIALES", RUTA)
    monkeypatch.setenv("SHEET_ID", "id-de-planilla-de-prueba")
    sesion, llamadas = SesionFalsa(append), []

    def service_account(**argumentos: Any) -> gspread.Client:
        llamadas.append(argumentos)
        return gspread.Client(None, session=sesion, http_client=argumentos["http_client"])  # type: ignore[arg-type]

    monkeypatch.setattr(gspread, "service_account", service_account)
    return sesion, llamadas


# ── La fila ──────────────────────────────────────────────────────────────


def test_las_columnas_son_las_de_la_planilla_en_orden() -> None:
    """R9: las 11 columnas de SPECS §5, en el orden exacto en que se cargan los encabezados."""
    assert COLUMNAS == (
        "fecha_ingreso", "nombre_cliente", "telefono", "producto", "material", "medidas",
        "cantidad", "tiene_diseno", "archivos", "fecha_necesita", "anticipo",
    )


def test_una_formula_se_escribe_raw_y_queda_como_texto(monkeypatch: pytest.MonkeyPatch) -> None:
    """R9: valueInputOption=RAW en el request; =IMPORTXML(...) llega a la celda tal cual, como texto."""
    sesion, _ = _conectar(monkeypatch)
    Planilla().escribir_fila(VALORES)
    [post] = sesion.posts()
    assert post["params"]["valueInputOption"] == "RAW"
    assert post["json"]["values"] == [[VALORES[columna] for columna in COLUMNAS[:-1]] + [""]]
    assert post["json"]["values"][0][COLUMNAS.index("material")] == FORMULA


@pytest.mark.parametrize("fecha", ["", "   "])
def test_sin_fecha_de_ingreso_lanza_en_vez_de_completarla(fecha: str) -> None:
    """R9: fecha_ingreso vacía no se completa con otro reloj: lanza y no escribe nada."""
    hoja = HojaFalsa()
    with pytest.raises(ValueError):
        Planilla(hoja).escribir_fila(VALORES | {"fecha_ingreso": fecha})
    assert hoja.filas == []


@pytest.mark.parametrize(
    "valores",
    [
        {columna: valor for columna, valor in VALORES.items() if columna != "archivos"},
        VALORES | {"anticipo": "5000"},
        VALORES | {"otra": "x"},
    ],
    ids=["falta_una", "trae_anticipo", "clave_de_mas"],
)
def test_claves_de_mas_o_de_menos_lanzan_sin_escribir(valores: dict[str, str]) -> None:
    """R9: la fila lleva justo las columnas de COLUMNAS; el anticipo no lo pone quien llama."""
    hoja = HojaFalsa()
    with pytest.raises(ValueError):
        Planilla(hoja).escribir_fila(valores)
    assert hoja.filas == []


# ── Tope, reintentos y fallas ────────────────────────────────────────────


def test_el_cliente_no_reintenta_y_cada_request_lleva_el_tope(monkeypatch: pytest.MonkeyPatch) -> None:
    """R10: HTTPClient, no BackOffHTTPClient, y (5, 180) en cada request, también en los que abren la planilla."""
    sesion, llamadas = _conectar(monkeypatch)
    Planilla().escribir_fila(VALORES)
    assert llamadas[0]["http_client"] is gspread.HTTPClient
    assert len(sesion.requests) == 3 and all(request["timeout"] == (5, 180) for request in sesion.requests)


@pytest.mark.parametrize(
    ("append", "tipo"),
    [
        (_Respuesta(503, {"error": {"code": 503, "message": CENTINELA, "status": "UNAVAILABLE"}}), "APIError"),
        (TimeoutError(CENTINELA), "TimeoutError"),
        (ConnectionError(CENTINELA), "ConnectionError"),
    ],
    ids=["503", "timeout", "conexion"],
)
def test_una_escritura_que_falla_es_error_planilla_tras_un_solo_intento(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, append: Any, tipo: str
) -> None:
    """R10, R2: un 5xx, un timeout o un corte dan ErrorPlanilla con un solo POST; del error se loguea el tipo."""
    sesion, _ = _conectar(monkeypatch, append)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ErrorPlanilla) as error:
        Planilla().escribir_fila(VALORES)
    assert len(sesion.posts()) == 1
    assert str(error.value) == NO_SE_ESCRIBIO
    assert f"planilla: fallo al escribir tipo={tipo}" in caplog.text
    assert CENTINELA not in caplog.text


def test_el_error_no_encadena_la_excepcion_original() -> None:
    """R53, R52: la excepción original puede traer la fila o la credencial; no queda ni en __context__."""
    with pytest.raises(ErrorPlanilla) as error:
        Planilla(HojaFalsa(error=RuntimeError(CENTINELA))).escribir_fila(VALORES)
    assert error.value.__cause__ is None and error.value.__context__ is None
    assert CENTINELA not in str(error.value)


@pytest.mark.parametrize("respuesta", [None, {}, {"updates": None}, {"updates": {}}, "texto", []])
def test_si_no_se_lee_el_rango_la_fila_cuenta_como_escrita(caplog: pytest.LogCaptureFixture, respuesta: Any) -> None:
    """R3: la escritura respondió; un rango que no se puede leer no la desdice ni lanza."""
    caplog.set_level(logging.INFO)
    hoja = HojaFalsa(respuesta)
    Planilla(hoja).escribir_fila(VALORES)
    assert len(hoja.filas) == 1
    assert f"rango={RANGO_DESCONOCIDO}" in caplog.text


def test_el_log_de_la_fila_escrita_lleva_el_rango_y_ningun_valor(caplog: pytest.LogCaptureFixture) -> None:
    """R52: de la fila escrita se loguea el rango, nunca un valor del pedido."""
    caplog.set_level(logging.DEBUG)
    Planilla(HojaFalsa()).escribir_fila(VALORES)
    assert f"rango={RANGO}" in caplog.text
    assert CENTINELA not in caplog.text and TELEFONO not in caplog.text


def test_los_loggers_de_google_no_quedan_en_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    """R52: en DEBUG, google-auth vuelca el request con la fila; al abrir la planilla suben a INFO."""
    for nombre in ("google", "urllib3"):
        monkeypatch.setattr(logging.getLogger(nombre), "level", logging.DEBUG)
    _conectar(monkeypatch)
    Planilla().escribir_fila(VALORES)
    assert all(logging.getLogger(nombre).getEffectiveLevel() >= logging.INFO for nombre in ("google", "urllib3"))


# ── Credencial ───────────────────────────────────────────────────────────


def test_la_credencial_se_lee_en_la_primera_escritura_y_una_sola_vez(monkeypatch: pytest.MonkeyPatch) -> None:
    """R53: construir la planilla sin credenciales no lanza ni arma el cliente; la primera escritura sí, y lo reusa."""
    sesion, llamadas = _conectar(monkeypatch)
    monkeypatch.delenv("GOOGLE_CREDENCIALES")
    planilla = Planilla()
    assert llamadas == []
    monkeypatch.setenv("GOOGLE_CREDENCIALES", RUTA)
    planilla.escribir_fila(VALORES)
    planilla.escribir_fila(VALORES)
    assert [llamada["filename"] for llamada in llamadas] == [RUTA]
    assert len(sesion.posts()) == 2


@pytest.mark.parametrize("falta", ["GOOGLE_CREDENCIALES", "SHEET_ID"])
def test_sin_credenciales_el_error_es_fijo_y_no_se_arma_el_cliente(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, falta: str
) -> None:
    """R53: una variable que falta es ErrorPlanilla con mensaje fijo, sin llegar a gspread."""
    _, llamadas = _conectar(monkeypatch)
    monkeypatch.delenv(falta)
    caplog.set_level(logging.INFO)
    with pytest.raises(ErrorPlanilla) as error:
        Planilla().escribir_fila(VALORES)
    assert str(error.value) == FALTAN_CREDENCIALES
    assert FALTAN_CREDENCIALES in caplog.text
    assert llamadas == []


@pytest.mark.parametrize(
    "pegado",
    ['{"type": "service_account", "private_key": "Centinela-7Q"}', "PRIVATE KEY Centinela-7Q", CENTINELA * 30],
    ids=["json", "clave", "base64"],
)
def test_la_credencial_pegada_en_lugar_de_la_ruta_da_error_fijo(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, pegado: str
) -> None:
    """R53: el contenido de la credencial en la variable de la ruta se detecta antes de tocar el disco."""
    _, llamadas = _conectar(monkeypatch)
    monkeypatch.setenv("GOOGLE_CREDENCIALES", pegado)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ErrorPlanilla) as error:
        Planilla().escribir_fila(VALORES)
    assert str(error.value) == CREDENCIAL_PEGADA
    assert CENTINELA not in caplog.text
    assert llamadas == []


@pytest.mark.parametrize(
    "contenido", ['{"private_key": "Centinela-7Q"', '{"type": "service_account", "private_key": "Centinela-7Q"}']
)
def test_un_json_de_credencial_roto_es_error_fijo_sin_su_contenido(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path, contenido: str
) -> None:
    """R53: con gspread de verdad y sin red, un JSON roto o incompleto no deja su contenido en el error ni el log."""
    archivo = tmp_path / "cuenta.json"
    archivo.write_text(contenido, encoding="utf-8")
    monkeypatch.setenv("GOOGLE_CREDENCIALES", str(archivo))
    monkeypatch.setenv("SHEET_ID", "id-de-planilla-de-prueba")
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ErrorPlanilla) as error:
        Planilla().escribir_fila(VALORES)
    assert str(error.value) == NO_SE_ESCRIBIO
    assert error.value.__context__ is None
    assert CENTINELA not in caplog.text
