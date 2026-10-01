import logging
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.memoria import (
    CARRERA,
    HUERFANA,
    RETENCION_GENERACIONES,
    RETENCION_PROCESADOS,
    TOPE_MENSAJES,
    TTL_CHARLA,
    ErrorMemoria,
    Memoria,
    Mensaje,
    Toma,
)
from app.pedidos import Pedido
from tests.conftest import TELEFONO

ZONA = timezone(timedelta(hours=-3))
T0 = datetime(2026, 9, 28, 10, 0, tzinfo=ZONA)
UN_SEGUNDO = timedelta(seconds=1)
UNA_HORA = timedelta(hours=1)
CONV = 555
ID_MENSAJE = 101
PEDIDO = Pedido(
    telefono=TELEFONO, producto="impresion_digital", cantidad=100, fecha_necesita=date(2026, 10, 9)
)
COMPLETO = PEDIDO.model_copy(
    update={"nombre_cliente": "Ana Prueba", "material": "obra 90 g", "medidas": "A5", "tiene_diseno": "si"}
)


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


def _tomar(memoria: Memoria) -> Toma:
    """El resumen de un pedido completo y el "sí" que lo toma, en una memoria recién abierta."""
    memoria.guardar_pedido(CONV, COMPLETO, 0, T0)
    toma = memoria.tomar_para_confirmar(CONV, T0)
    assert toma is not None
    return toma


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


def test_confirmar_conserva_el_pedido_nacido_durante_la_escritura(memoria: Memoria) -> None:
    """R22: un pedido que nació durante la escritura se conserva al cerrar, sin el historial."""
    toma = _tomar(memoria)
    memoria.anotar_cliente(CONV, "también quiero volantes", T0)
    memoria.guardar_pedido(CONV, PEDIDO, toma.generacion, T0)

    memoria.confirmar_escrito(CONV, toma, T0)
    charla = memoria.leer_charla(CONV, T0)

    assert charla.mensajes == [] and charla.pedido == PEDIDO


def test_lo_escrito_en_el_contacto_sobrevive_a_la_confirmacion(memoria: Memoria) -> None:
    """R45, R42: lo que la charla escribió en el contacto sigue ahí al anotar, tomar, devolver y confirmar,
    para que un payload viejo no dé de alta otra vez al contacto; vence con la charla (R22)."""
    memoria.anotar_atributos(CONV, {"primer_contacto": T0.isoformat(), "nombre_preguntado": True}, T0)
    memoria.anotar_atributos(CONV, {"nombre_cliente": "Ana Prueba"}, T0)
    esperado = {"primer_contacto": T0.isoformat(), "nombre_preguntado": True, "nombre_cliente": "Ana Prueba"}

    memoria.anotar_cliente(CONV, "hola", T0)
    memoria.devolver_a_pendiente(CONV, _tomar(memoria), T0)
    toma = memoria.tomar_para_confirmar(CONV, T0)
    assert toma is not None and memoria.leer_charla(CONV, T0).atributos == esperado
    memoria.confirmar_escrito(CONV, toma, T0)

    assert memoria.leer_charla(CONV, T0).atributos == esperado
    assert memoria.leer_charla(CONV, T0 + TTL_CHARLA).atributos == {}


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
    memoria.confirmar_escrito(CONV, _tomar(memoria), T0)
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


def test_una_toma_de_otra_epoca_se_libera_pasados_5_minutos_sin_volver_a_pendiente(ruta: Path) -> None:
    """R25: el proceso murió escribiendo; pasados 5 minutos se libera y el pedido no vuelve."""
    memoria = Memoria(ruta)
    _tomar(memoria)
    memoria.cerrar()
    memoria = Memoria(ruta)

    memoria.barrer(T0 + HUERFANA)
    assert memoria.leer_charla(CONV, T0 + HUERFANA).toma is not None
    despues = T0 + HUERFANA + UN_SEGUNDO
    memoria.barrer(despues)
    charla = memoria.leer_charla(CONV, despues)
    otra = memoria.tomar_para_confirmar(CONV, despues)
    memoria.cerrar()

    assert charla.toma is None and charla.pedido is None and otra is None


def test_una_toma_de_la_misma_epoca_no_es_huerfana(memoria: Memoria) -> None:
    """R25: la toma en vuelo de este proceso no se libera, aunque pase más de 5 minutos."""
    toma = _tomar(memoria)

    memoria.barrer(T0 + UNA_HORA)

    assert memoria.leer_charla(CONV, T0 + UNA_HORA).toma == toma


def test_una_toma_escrita_de_otra_epoca_no_es_huerfana(ruta: Path) -> None:
    """R25: solo se libera la que está en escritura; la escrita sigue como recién confirmada."""
    memoria = Memoria(ruta)
    memoria.confirmar_escrito(CONV, _tomar(memoria), T0)
    memoria.cerrar()
    memoria = Memoria(ruta)

    memoria.barrer(T0 + UNA_HORA)
    toma = memoria.leer_charla(CONV, T0 + UNA_HORA).toma
    memoria.cerrar()

    assert toma == Toma(pedido=COMPLETO, generacion=1, escrita=True)


# R26 · registro ilegible


@pytest.mark.parametrize(
    "datos",
    [
        "no es json dato-del-cliente",
        '{"mensajes": [{"role": "system", "content": "dato-del-cliente"}], "pedido": null}',
        '{"mensajes": [], "pedido": "dato-del-cliente"}',
        # R26: un pedido con forma de pedido que el modelo de hoy ya no acepta
        '{"mensajes": [], "pedido": {"telefono": "dato-del-cliente", "cantidad": 0}}',
        '{"mensajes": [], "pedido": {"producto": "dato-del-cliente"}}',
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


@pytest.mark.parametrize(
    "datos",
    [
        "no es json dato-del-cliente",
        '{"pedido": {"telefono": "dato-del-cliente"}, "generacion": "dato-del-cliente"}',
    ],
)
def test_una_toma_ilegible_se_descarta_sin_loguear_el_valor(
    ruta: Path, datos: str, caplog: pytest.LogCaptureFixture
) -> None:
    """R26: una toma que ya no valida se descarta; el log dice tabla y tipo de error, nunca el valor."""
    memoria = Memoria(ruta)
    _tomar(memoria)
    memoria.cerrar()
    _sql(ruta, f"UPDATE confirmaciones SET datos = '{datos}';")
    memoria = Memoria(ruta)
    caplog.set_level(logging.INFO, logger="app.memoria")

    toma = memoria.leer_charla(CONV, T0).toma
    memoria.cerrar()

    assert toma is None
    assert "tabla=confirmaciones error=ValidationError" in caplog.text
    assert "dato-del-cliente" not in caplog.text
    assert _sql(ruta, "SELECT count(*) FROM confirmaciones") == [(0,)]


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


# §7 · máquina de confirmación (R1 a R5)


def test_la_toma_saca_el_pedido_de_la_charla_y_lo_deja_en_escritura(memoria: Memoria) -> None:
    """R4, R5: la toma sube la generación y el pedido pasa de la charla (R22) a EN ESCRITURA."""
    memoria.anotar_cliente(CONV, "sí", T0)
    memoria.guardar_pedido(CONV, COMPLETO, 0, T0)

    toma = memoria.tomar_para_confirmar(CONV, T0)
    charla = memoria.leer_charla(CONV, T0)

    assert toma == Toma(pedido=COMPLETO, generacion=1)
    assert charla.pedido is None and charla.generacion == 1 and charla.toma == toma
    assert charla.mensajes == [Mensaje(role="user", content="sí")]


def test_sin_pedido_completo_no_hay_toma(memoria: Memoria) -> None:
    """R1: un pedido a medias no se toma; sigue en la charla y la generación no sube."""
    memoria.guardar_pedido(CONV, PEDIDO, 0, T0)

    assert memoria.tomar_para_confirmar(CONV, T0) is None
    charla = memoria.leer_charla(CONV, T0)
    assert charla.pedido == PEDIDO and charla.generacion == 0


def test_un_pendiente_vencido_no_se_toma(memoria: Memoria) -> None:
    """R22: un "sí" de mañana no confirma el resumen de hoy."""
    memoria.guardar_pedido(CONV, COMPLETO, 0, T0)

    assert memoria.tomar_para_confirmar(CONV, T0 + TTL_CHARLA) is None


def test_dos_si_a_la_vez_toman_el_pedido_una_sola_vez(memoria: Memoria) -> None:
    """R4: dos hilos confirman el mismo pedido a la vez y uno solo se lo lleva."""
    memoria.guardar_pedido(CONV, COMPLETO, 0, T0)

    with ThreadPoolExecutor(max_workers=8) as hilos:
        tomas = list(hilos.map(lambda _: memoria.tomar_para_confirmar(CONV, T0), range(32)))

    assert [toma for toma in tomas if toma is not None] == [Toma(pedido=COMPLETO, generacion=1)]


def test_una_toma_nueva_reemplaza_a_la_trabada_de_esta_epoca(memoria: Memoria) -> None:
    """R25, R28: si falló cerrar la toma, la que quedó en escritura de esta época ya murió (el candado del turno
    no deja entrar a otro mensaje mientras escribe): el pedido siguiente la reemplaza en vez de esperar 6 horas."""
    toma = _tomar(memoria)
    memoria.guardar_pedido(CONV, COMPLETO, toma.generacion, T0)

    nueva = memoria.tomar_para_confirmar(CONV, T0)

    assert nueva == Toma(pedido=COMPLETO, generacion=2)
    assert memoria.leer_charla(CONV, T0).toma == nueva


def test_la_toma_trabada_no_deja_tomar_dos_veces_el_mismo_pedido(memoria: Memoria) -> None:
    """R4: "sí" y "dale" seguidos: aunque el "sí" haya quedado trabado, el "dale" no encuentra pendiente."""
    toma = _tomar(memoria)

    assert memoria.tomar_para_confirmar(CONV, T0) is None
    charla = memoria.leer_charla(CONV, T0)
    assert charla.toma == toma and charla.generacion == 1


def test_una_toma_en_escritura_de_otra_epoca_no_se_reemplaza(ruta: Path) -> None:
    """R25: la de otra época la libera el barrido pasados 5 minutos; antes, un pedido nuevo no la reemplaza."""
    memoria = Memoria(ruta)
    toma = _tomar(memoria)
    memoria.guardar_pedido(CONV, COMPLETO, toma.generacion, T0)
    memoria.cerrar()
    memoria = Memoria(ruta)

    otra = memoria.tomar_para_confirmar(CONV, T0)
    charla = memoria.leer_charla(CONV, T0)
    memoria.cerrar()

    assert otra is None and charla.toma == toma and charla.pedido == COMPLETO


def test_si_falla_con_otro_pedido_en_curso_queda_el_devuelto(
    memoria: Memoria, caplog: pytest.LogCaptureFixture
) -> None:
    """R2: como en el bot viejo, el devuelto pisa al que nació durante la escritura, con un aviso."""
    toma = _tomar(memoria)
    memoria.guardar_pedido(CONV, PEDIDO, toma.generacion, T0)
    caplog.set_level(logging.WARNING, logger="app.memoria")

    memoria.devolver_a_pendiente(CONV, toma, T0)

    assert memoria.leer_charla(CONV, T0).pedido == COMPLETO
    assert "otro pedido en curso" in caplog.text


def test_una_toma_confirmada_no_vuelve_a_pendiente(memoria: Memoria) -> None:
    """R3: una fila escrita no se desdice; devolverla después no deja un pedido para otro "sí"."""
    toma = _tomar(memoria)
    memoria.confirmar_escrito(CONV, toma, T0)

    memoria.devolver_a_pendiente(CONV, toma, T0)
    charla = memoria.leer_charla(CONV, T0)

    assert charla.pedido is None
    assert charla.toma == Toma(pedido=COMPLETO, generacion=1, escrita=True, en_carrera=True)


def test_el_recien_confirmado_vence_con_el_ttl_contado_desde_la_confirmacion(
    memoria: Memoria, ruta: Path
) -> None:
    """R6, R7: el confirmado se lee 6 horas desde que quedó escrito; después el barrido lo borra."""
    toma = _tomar(memoria)
    escrito = T0 + UNA_HORA
    memoria.confirmar_escrito(CONV, toma, escrito)

    assert memoria.leer_charla(CONV, escrito + TTL_CHARLA - UN_SEGUNDO).toma is not None
    assert memoria.leer_charla(CONV, escrito + TTL_CHARLA).toma is None
    memoria.barrer(escrito + TTL_CHARLA)
    memoria.cerrar()
    assert _sql(ruta, "SELECT count(*) FROM confirmaciones") == [(0,)]


def test_la_carrera_dura_5_minutos_desde_la_fila(memoria: Memoria) -> None:
    """R6, R7: la toma escrita está en carrera hasta 5 minutos después de la fila, no de la toma; después sigue
    como recién confirmada, sin carrera."""
    toma = _tomar(memoria)
    assert memoria.leer_charla(CONV, T0).toma == toma and not toma.en_carrera  # en escritura: el candado (R28)
    escrito = T0 + UNA_HORA
    memoria.confirmar_escrito(CONV, toma, escrito)

    antes = memoria.leer_charla(CONV, escrito + CARRERA - UN_SEGUNDO).toma
    despues = memoria.leer_charla(CONV, escrito + CARRERA).toma

    assert antes == Toma(pedido=COMPLETO, generacion=1, escrita=True, en_carrera=True)
    assert despues == Toma(pedido=COMPLETO, generacion=1, escrita=True)


def test_lo_leido_antes_de_la_toma_no_pisa_ni_reabre(memoria: Memoria) -> None:
    """R5: un "gracias" que leyó el pedido antes del "sí" no lo deja pendiente otra vez."""
    memoria.guardar_pedido(CONV, COMPLETO, 0, T0)
    leida = memoria.leer_charla(CONV, T0)
    toma = memoria.tomar_para_confirmar(CONV, T0)
    assert toma is not None

    assert memoria.guardar_pedido(CONV, leida.pedido, leida.generacion, T0) is False
    assert memoria.leer_charla(CONV, T0).pedido is None
    memoria.devolver_a_pendiente(CONV, toma, T0)  # tampoco pisa al que volvió a pendiente
    assert memoria.guardar_pedido(CONV, PEDIDO, leida.generacion, T0) is False
    assert memoria.leer_charla(CONV, T0).pedido == COMPLETO


def test_las_generaciones_se_retienen_30_dias(memoria: Memoria) -> None:
    """R5: un mensaje viejo siempre tiene contra qué comparar."""
    _tomar(memoria)

    memoria.barrer(T0 + RETENCION_GENERACIONES - UN_SEGUNDO)
    assert memoria.leer_charla(CONV, T0).generacion == 1

    memoria.barrer(T0 + RETENCION_GENERACIONES)
    assert memoria.leer_charla(CONV, T0).generacion == 0


# Reglas de otras secciones que pasan por la memoria


def test_una_hora_sin_zona_se_rechaza(memoria: Memoria) -> None:
    """R37: la hora viene de config.ahora(), con zona; sin zona sería la del servidor."""
    with pytest.raises(ValueError):
        memoria.anotar_cliente(CONV, "hola", datetime(2026, 9, 28, 10, 0))


def test_una_base_que_no_abre_falla_al_construir(tmp_path: Path) -> None:
    """R54: el arranque descubre la base rota, no el primer mensaje."""
    with pytest.raises(ErrorMemoria):
        Memoria(tmp_path)
