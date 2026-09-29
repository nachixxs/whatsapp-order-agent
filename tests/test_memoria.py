import logging
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.memoria import (
    RETENCION_GENERACIONES,
    RETENCION_PROCESADOS,
    TOPE_MENSAJES,
    TTL_CHARLA,
    Charla,
    ErrorMemoria,
    Memoria,
    Mensaje,
)

ZONA = timezone(timedelta(hours=-3))
T0 = datetime(2026, 9, 28, 10, 0, tzinfo=ZONA)
UN_SEGUNDO = timedelta(seconds=1)
CONV = 555
ID_MENSAJE = 101
PEDIDO = {"producto": "impresion_digital", "cantidad": "100"}


@pytest.fixture
def ruta(tmp_path: Path) -> Path:
    return tmp_path / "memoria.db"


@pytest.fixture
def memoria(ruta: Path) -> Iterator[Memoria]:
    abierta = Memoria(ruta)
    yield abierta
    abierta.cerrar()


def _sql(ruta: Path, script: str) -> list[tuple[object, ...]]:
    """Toca el archivo desde afuera, con la memoria cerrada (R28 no deja abrirlo en paralelo)."""
    con = sqlite3.connect(ruta, autocommit=True)
    try:
        if script.lstrip().upper().startswith("SELECT"):
            return con.execute(script).fetchall()
        con.executescript(script)
        return []
    finally:
        con.close()


def _hacer_fallar(ruta: Path, operacion: str) -> None:
    """Un trigger que hace fallar esa operación sobre `procesados`, como un error de la base."""
    disparador = f"CREATE TRIGGER f BEFORE {operacion} ON procesados"
    _sql(ruta, f"{disparador} BEGIN SELECT RAISE(ABORT, 'x'); END;")


def _bytes_en_disco(ruta: Path) -> bytes:
    wal = ruta.with_name(ruta.name + "-wal")
    return ruta.read_bytes() + (wal.read_bytes() if wal.exists() else b"")


# R21 · historial con marcadores


def test_historial_guarda_al_cliente_y_el_marcador_del_bot(memoria: Memoria) -> None:
    """R21: del lado del bot queda el marcador entre corchetes, no la prosa."""
    memoria.anotar_cliente(CONV, "quiero 100 tarjetas", T0)
    memoria.anotar_marcador(CONV, "dato_faltante: material", T0)

    assert memoria.leer_charla(CONV, T0).mensajes == [
        Mensaje(role="user", content="quiero 100 tarjetas"),
        Mensaje(role="assistant", content="[dato_faltante: material]"),
    ]


# R22 · tope y TTL


def test_el_tope_deja_los_ultimos_mensajes(memoria: Memoria) -> None:
    """R22: con más de 20 mensajes quedan los últimos 20, en orden."""
    for numero in range(TOPE_MENSAJES + 5):
        memoria.anotar_cliente(CONV, f"mensaje {numero}", T0)

    mensajes = memoria.leer_charla(CONV, T0).mensajes

    assert len(mensajes) == TOPE_MENSAJES
    assert mensajes[0].content == "mensaje 5"
    assert mensajes[-1].content == f"mensaje {TOPE_MENSAJES + 4}"


def test_un_tope_menor_que_uno_se_rechaza(ruta: Path) -> None:
    """R22: tope 0 desactivaría el tope en vez de ponerlo en cero."""
    with pytest.raises(ValueError):
        Memoria(ruta, tope_mensajes=0)


def test_la_charla_vence_a_las_6_horas_con_su_pedido(memoria: Memoria) -> None:
    """R22: un "OK" de mañana no confirma el resumen de hoy."""
    memoria.anotar_marcador(CONV, "resumen_mostrado", T0)
    memoria.guardar_pedido(CONV, PEDIDO, 0, T0)

    assert memoria.leer_charla(CONV, T0 + TTL_CHARLA - UN_SEGUNDO).pedido == PEDIDO
    vencida = memoria.leer_charla(CONV, T0 + TTL_CHARLA)

    assert vencida.mensajes == [] and vencida.pedido is None


def test_cada_mensaje_renueva_el_ttl(memoria: Memoria) -> None:
    """R22: el TTL cuenta desde el último movimiento, no desde el primero."""
    memoria.anotar_cliente(CONV, "hola", T0)
    memoria.anotar_cliente(CONV, "sigo acá", T0 + timedelta(hours=5))

    assert len(memoria.leer_charla(CONV, T0 + timedelta(hours=10)).mensajes) == 2


def test_anotar_en_una_charla_vencida_arranca_de_cero(memoria: Memoria) -> None:
    """R22: lo vencido no vuelve pegado al primer mensaje nuevo."""
    memoria.anotar_cliente(CONV, "viejo", T0)
    memoria.guardar_pedido(CONV, PEDIDO, 0, T0)

    memoria.anotar_cliente(CONV, "nuevo", T0 + TTL_CHARLA)
    charla = memoria.leer_charla(CONV, T0 + TTL_CHARLA)

    assert charla.mensajes == [Mensaje(role="user", content="nuevo")]
    assert charla.pedido is None


def test_cerrar_la_charla_borra_el_historial(memoria: Memoria) -> None:
    """R22: al confirmar se cierra la charla; el próximo mensaje arranca limpio."""
    memoria.anotar_cliente(CONV, "sí", T0)

    memoria.cerrar_charla(CONV, T0)

    assert memoria.leer_charla(CONV, T0) == Charla()


def test_cerrar_la_charla_conserva_el_pedido_nacido_durante_la_escritura(memoria: Memoria) -> None:
    """R22: un pedido que nació durante la escritura se conserva al cerrar."""
    memoria.anotar_cliente(CONV, "también quiero volantes", T0)
    memoria.guardar_pedido(CONV, PEDIDO, 0, T0)

    memoria.cerrar_charla(CONV, T0)
    charla = memoria.leer_charla(CONV, T0)

    assert charla.mensajes == [] and charla.pedido == PEDIDO


# R23 y R24 · dedup y compuerta


def test_un_id_se_procesa_una_sola_vez(memoria: Memoria) -> None:
    """R23: el reintento de un mensaje ya procesado se descarta."""
    assert memoria.marcar_procesado(ID_MENSAJE, T0) is True
    assert memoria.marcar_procesado(ID_MENSAJE, T0) is False


def test_un_id_desmarcado_se_vuelve_a_procesar(memoria: Memoria) -> None:
    """R23: si el proceso falló, el reintento de Chatwoot lo procesa."""
    memoria.marcar_procesado(ID_MENSAJE, T0)

    memoria.desmarcar_procesado(ID_MENSAJE)

    assert memoria.marcar_procesado(ID_MENSAJE, T0) is True


def test_cerrar_o_vencer_la_charla_no_borra_los_ids(memoria: Memoria) -> None:
    """R23: los ids viven aparte de la charla."""
    memoria.marcar_procesado(ID_MENSAJE, T0)
    memoria.anotar_cliente(CONV, "hola", T0)
    memoria.cerrar_charla(CONV, T0)
    memoria.barrer(T0 + TTL_CHARLA)

    assert memoria.marcar_procesado(ID_MENSAJE, T0 + TTL_CHARLA) is False


def test_los_ids_se_retienen_8_dias(memoria: Memoria) -> None:
    """R23: el barrido olvida un id recién cumplida la retención."""
    memoria.marcar_procesado(ID_MENSAJE, T0)

    memoria.barrer(T0 + RETENCION_PROCESADOS - UN_SEGUNDO)
    assert memoria.marcar_procesado(ID_MENSAJE, T0) is False

    memoria.barrer(T0 + RETENCION_PROCESADOS)
    assert memoria.marcar_procesado(ID_MENSAJE, T0) is True


def test_el_mismo_id_en_paralelo_se_procesa_una_vez(memoria: Memoria) -> None:
    """R23: dos hilos con el mismo id no lo procesan los dos."""
    with ThreadPoolExecutor(max_workers=8) as hilos:
        resultados = list(hilos.map(lambda _: memoria.marcar_procesado(ID_MENSAJE, T0), range(32)))

    assert resultados.count(True) == 1


def test_si_falla_el_dedup_lanza_en_vez_de_dejar_procesar(
    ruta: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """R24: sin compuerta no se procesa; el log dice tabla y tipo de error (R52)."""
    Memoria(ruta).cerrar()
    _hacer_fallar(ruta, "INSERT")
    memoria = Memoria(ruta)
    caplog.set_level(logging.INFO, logger="app.memoria")

    with pytest.raises(ErrorMemoria):
        memoria.marcar_procesado(ID_MENSAJE, T0)
    memoria.cerrar()

    assert "tabla=procesados error=IntegrityError" in caplog.text


# R25 · época


def test_cada_apertura_tiene_su_epoca(ruta: Path) -> None:
    """R25: la época distingue al proceso que tomó una confirmación de uno que murió."""
    primera = Memoria(ruta)
    epoca = primera.epoca
    primera.cerrar()
    segunda = Memoria(ruta)
    segunda.cerrar()

    assert len(epoca) == 32 and epoca != segunda.epoca


# R26 · registro ilegible


@pytest.mark.parametrize(
    "datos",
    [
        "no es json dato-del-cliente",
        '{"mensajes": [{"role": "system", "content": "dato-del-cliente"}], "pedido": null}',
        '{"mensajes": [], "pedido": "dato-del-cliente"}',
    ],
)
def test_una_charla_ilegible_se_descarta_sin_loguear_el_valor(
    ruta: Path, datos: str, caplog: pytest.LogCaptureFixture
) -> None:
    """R26: se descarta; el log dice tabla y tipo de error, nunca el valor."""
    memoria = Memoria(ruta)
    memoria.guardar_pedido(CONV, PEDIDO, 0, T0)
    memoria.cerrar()
    _sql(ruta, f"UPDATE charlas SET datos = '{datos}';")
    memoria = Memoria(ruta)
    caplog.set_level(logging.INFO, logger="app.memoria")

    charla = memoria.leer_charla(CONV, T0)
    memoria.cerrar()

    assert charla.mensajes == [] and charla.pedido is None
    assert "tabla=charlas error=ValidationError" in caplog.text
    assert "dato-del-cliente" not in caplog.text
    assert _sql(ruta, "SELECT count(*) FROM charlas") == [(0,)]


# R27 · SQLite en serio


@pytest.mark.parametrize("ruta_invalida", [":memory:", "", "  "])
def test_la_ruta_tiene_que_ser_un_archivo(ruta_invalida: str) -> None:
    """R27: `:memory:` se rechaza; un reinicio se llevaría todo."""
    with pytest.raises(ValueError):
        Memoria(ruta_invalida)


def test_la_charla_sobrevive_a_reabrir(ruta: Path) -> None:
    """R27: la base es un archivo; un reinicio no se lleva la charla ni el pedido."""
    memoria = Memoria(ruta)
    memoria.anotar_cliente(CONV, "hola", T0)
    memoria.guardar_pedido(CONV, PEDIDO, 0, T0)
    memoria.cerrar()

    memoria = Memoria(ruta)
    charla = memoria.leer_charla(CONV, T0)
    memoria.cerrar()

    assert charla.mensajes == [Mensaje(role="user", content="hola")]
    assert charla.pedido == PEDIDO


def test_si_wal_no_queda_activo_la_base_no_abre(
    ruta: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R27: en un disco de red el PRAGMA no falla, devuelve otro modo; se relee y no se abre."""
    conectar = sqlite3.connect

    class ConexionSinWal:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._con = conectar(*args, **kwargs)  # type: ignore[arg-type]

        def execute(self, sentencia: str, *parametros: object) -> sqlite3.Cursor:
            if sentencia == "PRAGMA journal_mode=WAL":
                sentencia = "PRAGMA journal_mode"
            return self._con.execute(sentencia, *parametros)  # type: ignore[arg-type]

        def close(self) -> None:
            self._con.close()

    monkeypatch.setattr(sqlite3, "connect", ConexionSinWal)

    with pytest.raises(ErrorMemoria, match="WAL"):
        Memoria(ruta)


def test_lo_barrido_no_queda_legible_en_el_archivo(memoria: Memoria, ruta: Path) -> None:
    """R27: secure_delete y checkpoint TRUNCATE; el texto borrado no queda en el disco."""
    memoria.anotar_cliente(CONV, "texto-del-cliente-123", T0)
    memoria.barrer(T0)  # checkpoint: el texto pasa del -wal al archivo principal
    assert b"texto-del-cliente-123" in ruta.read_bytes()

    memoria.barrer(T0 + TTL_CHARLA)

    assert b"texto-del-cliente-123" not in _bytes_en_disco(ruta)


def test_un_barrido_que_falla_no_borra_nada(ruta: Path) -> None:
    """R27: el barrido corre en un SAVEPOINT; a medias no deja nada."""
    memoria = Memoria(ruta)
    memoria.anotar_cliente(CONV, "hola", T0)
    memoria.marcar_procesado(ID_MENSAJE, T0)
    memoria.cerrar()
    _hacer_fallar(ruta, "DELETE")
    memoria = Memoria(ruta)

    with pytest.raises(ErrorMemoria):
        memoria.barrer(T0 + RETENCION_PROCESADOS)
    memoria.cerrar()

    assert _sql(ruta, "SELECT count(*) FROM charlas") == [(1,)]


# R28 · un solo proceso por archivo


def test_una_segunda_memoria_sobre_el_mismo_archivo_no_abre(memoria: Memoria, ruta: Path) -> None:
    """R28: dos procesos sobre el mismo archivo pisarían la misma charla."""
    with pytest.raises(ErrorMemoria):
        Memoria(ruta)

    memoria.cerrar()
    Memoria(ruta).cerrar()


# Reglas de otras secciones que pasan por la memoria


def test_un_pedido_leido_con_una_generacion_vieja_no_pisa(ruta: Path) -> None:
    """R5: lo que se leyó antes de una toma no pisa el pedido."""
    memoria = Memoria(ruta)
    memoria.guardar_pedido(CONV, PEDIDO, 0, T0)
    memoria.cerrar()
    _sql(ruta, f"INSERT INTO generaciones VALUES ({CONV}, 1, {T0.timestamp()});")
    memoria = Memoria(ruta)

    assert memoria.leer_charla(CONV, T0).generacion == 1
    assert memoria.guardar_pedido(CONV, {"producto": "sellos"}, 0, T0) is False
    assert memoria.leer_charla(CONV, T0).pedido == PEDIDO
    memoria.cerrar()


def test_las_generaciones_se_retienen_30_dias(ruta: Path) -> None:
    """R5: un mensaje viejo siempre tiene contra qué comparar."""
    Memoria(ruta).cerrar()
    _sql(ruta, f"INSERT INTO generaciones VALUES ({CONV}, 1, {T0.timestamp()});")
    memoria = Memoria(ruta)

    memoria.barrer(T0 + RETENCION_GENERACIONES - UN_SEGUNDO)
    assert memoria.leer_charla(CONV, T0).generacion == 1

    memoria.barrer(T0 + RETENCION_GENERACIONES)
    assert memoria.leer_charla(CONV, T0).generacion == 0
    memoria.cerrar()


def test_una_hora_sin_zona_se_rechaza(memoria: Memoria) -> None:
    """R37: la hora viene de config.ahora(), con zona; sin zona sería la del servidor."""
    with pytest.raises(ValueError):
        memoria.anotar_cliente(CONV, "hola", datetime(2026, 9, 28, 10, 0))


def test_una_base_que_no_abre_falla_al_construir(tmp_path: Path) -> None:
    """R54: el arranque descubre la base rota, no el primer mensaje."""
    with pytest.raises(ErrorMemoria):
        Memoria(tmp_path)
