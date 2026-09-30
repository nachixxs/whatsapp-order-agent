"""Memoria del bot en SQLite: charla, pedido en curso, confirmación (§7) y dedup (R1 a R5, R21 a R28)."""

import logging
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from app.pedidos import Pedido

logger = logging.getLogger(__name__)

TOPE_MENSAJES = 20
TTL_CHARLA = timedelta(hours=6)  # R22; también el del pedido recién confirmado (R6, R7)
RETENCION_PROCESADOS = timedelta(days=8)  # R23
RETENCION_GENERACIONES = timedelta(days=30)  # R5
HUERFANA = timedelta(minutes=5)  # R25
CONFIRMACION_FALLIDA = "confirmacion_fallida"  # R2: el marcador con el que vuelve el pedido

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS charlas (
    conversacion INTEGER PRIMARY KEY, actualizada REAL NOT NULL, datos TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS procesados (id_mensaje INTEGER PRIMARY KEY, marcado REAL NOT NULL);
CREATE TABLE IF NOT EXISTS generaciones (
    conversacion INTEGER PRIMARY KEY, generacion INTEGER NOT NULL, actualizada REAL NOT NULL);
CREATE TABLE IF NOT EXISTS confirmaciones (
    conversacion INTEGER PRIMARY KEY, actualizada REAL NOT NULL, datos TEXT NOT NULL,
    epoca TEXT NOT NULL, escrita INTEGER NOT NULL);
"""


class ErrorMemoria(Exception):
    """La base falló. El mensaje lleva la tabla y el tipo de error, nunca un valor (R52)."""


class Mensaje(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["user", "assistant"]
    content: str


class Toma(BaseModel):
    """Un pedido tomado para confirmar (§7): EN ESCRITURA hasta que la fila queda, después CONFIRMADO."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pedido: Pedido
    generacion: int  # R5: la que subió esta toma; la identifica al confirmarla o devolverla
    escrita: bool = False  # no va en `datos`: sale de su columna


class Charla(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mensajes: list[Mensaje] = []
    pedido: Pedido | None = None  # R26: uno que ya no valida descarta la charla al leerla
    generacion: int = 0  # R5: no se guarda con la charla, sale de `generaciones`
    toma: Toma | None = None  # R6, R7: tampoco; sale de `confirmaciones` y vence aparte


def _segundos(ahora: datetime) -> float:
    if ahora.tzinfo is None:
        raise ValueError("ahora tiene que venir con zona horaria")  # R37
    return ahora.timestamp()


def _conectar(ruta: Path) -> sqlite3.Connection:
    con = None
    try:
        # R28: timeout 0 y lock exclusivo; un segundo proceso sobre el archivo no abre
        con = sqlite3.connect(ruta, autocommit=True, check_same_thread=False, timeout=0)
        con.execute("PRAGMA locking_mode=EXCLUSIVE")
        modo = con.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(modo).lower() != "wal":  # R27: en un disco de red no falla, devuelve otro modo
            raise ErrorMemoria("no abre: el modo no quedó en WAL")
        con.execute("PRAGMA secure_delete=ON")
        con.executescript(_ESQUEMA)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()  # R27
        return con
    except BaseException as error:
        if con is not None:
            con.close()
        if isinstance(error, sqlite3.Error):
            raise ErrorMemoria(f"no abre: {type(error).__name__}") from None
        raise


def _validar[M: BaseModel](
    con: sqlite3.Connection, tabla: str, modelo: type[M], conversacion: int, datos: str
) -> M | None:
    try:
        return modelo.model_validate_json(datos)
    except ValidationError as error:  # R26: el error trae el valor adentro; va solo el tipo
        logger.warning("Memoria: ilegible descartado tabla=%s error=%s", tabla, type(error).__name__)
        con.execute(f"DELETE FROM {tabla} WHERE conversacion = ?", (conversacion,))
        return None


def _leer_toma(con: sqlite3.Connection, conversacion: int, ahora_s: float) -> Toma | None:
    sql = "SELECT actualizada, datos, escrita FROM confirmaciones WHERE conversacion = ?"
    fila = con.execute(sql, (conversacion,)).fetchone()
    if fila is None or ahora_s - fila[0] >= TTL_CHARLA.total_seconds():
        return None
    toma = _validar(con, "confirmaciones", Toma, conversacion, fila[1])
    return None if toma is None else toma.model_copy(update={"escrita": bool(fila[2])})


def _leer(con: sqlite3.Connection, conversacion: int, ahora_s: float) -> Charla:
    clave = (conversacion,)
    fila = con.execute(
        "SELECT generacion FROM generaciones WHERE conversacion = ?", clave
    ).fetchone()
    vacia = Charla(generacion=fila[0] if fila else 0, toma=_leer_toma(con, conversacion, ahora_s))
    fila = con.execute(
        "SELECT actualizada, datos FROM charlas WHERE conversacion = ?", clave
    ).fetchone()
    if fila is None or ahora_s - fila[0] >= TTL_CHARLA.total_seconds():  # R22
        return vacia
    charla = _validar(con, "charlas", Charla, conversacion, fila[1])
    if charla is None:
        return vacia
    return charla.model_copy(update={"generacion": vacia.generacion, "toma": vacia.toma})


def _escribir(con: sqlite3.Connection, conversacion: int, charla: Charla, ahora_s: float) -> None:
    datos = charla.model_dump_json(exclude={"generacion", "toma"})
    con.execute("REPLACE INTO charlas VALUES (?, ?, ?)", (conversacion, ahora_s, datos))


class Memoria:
    """Una conexión por proceso; cada método público es una transacción bajo un candado."""

    def __init__(self, ruta: Path | str, tope_mensajes: int = TOPE_MENSAJES) -> None:
        if tope_mensajes < 1:
            raise ValueError("tope_mensajes tiene que ser al menos 1")  # R22
        if str(ruta).strip() in ("", ":memory:"):
            raise ValueError("la memoria necesita la ruta de un archivo")  # R27
        self._tope = tope_mensajes
        self._candado = threading.Lock()
        self.epoca = uuid.uuid4().hex  # R25
        self._con = _conectar(Path(ruta))

    def cerrar(self) -> None:
        with self._candado:
            self._con.close()

    @contextmanager
    def _transaccion(self, tabla: str, checkpoint: bool = False) -> Iterator[sqlite3.Connection]:
        with self._candado:
            try:
                self._con.execute("SAVEPOINT memoria")  # R27: todo o nada, también el barrido
                try:
                    yield self._con
                    self._con.execute("RELEASE memoria")
                except BaseException:
                    self._con.execute("ROLLBACK TO memoria")
                    self._con.execute("RELEASE memoria")
                    raise
                if checkpoint:  # R27: lo borrado no queda legible en el -wal
                    self._con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            except sqlite3.Error as error:
                tipo = type(error).__name__
                logger.error("Memoria: error de base tabla=%s error=%s", tabla, tipo)
                raise ErrorMemoria(f"{tabla}: {tipo}") from None

    def leer_charla(self, conversacion: int, ahora: datetime) -> Charla:
        with self._transaccion("charlas") as con:
            return _leer(con, conversacion, _segundos(ahora))

    def anotar_cliente(self, conversacion: int, texto: str, ahora: datetime) -> None:
        self._anotar(conversacion, Mensaje(role="user", content=texto), ahora)

    def anotar_marcador(self, conversacion: int, marcador: str, ahora: datetime) -> None:
        # R21: del lado del bot va el marcador, nunca la prosa de la respuesta
        self._anotar(conversacion, Mensaje(role="assistant", content=f"[{marcador}]"), ahora)

    def _anotar(self, conversacion: int, mensaje: Mensaje, ahora: datetime) -> None:
        ahora_s = _segundos(ahora)
        with self._transaccion("charlas") as con:
            charla = _leer(con, conversacion, ahora_s)
            mensajes = [*charla.mensajes, mensaje][-self._tope :]  # R22
            _escribir(con, conversacion, Charla(mensajes=mensajes, pedido=charla.pedido), ahora_s)

    def guardar_pedido(
        self, conversacion: int, pedido: Pedido | None, generacion: int, ahora: datetime
    ) -> bool:
        """False, sin tocar nada, si la generación cambió desde que se leyó la charla (R5)."""
        ahora_s = _segundos(ahora)
        with self._transaccion("charlas") as con:
            charla = _leer(con, conversacion, ahora_s)
            if charla.generacion != generacion:
                return False
            _escribir(con, conversacion, Charla(mensajes=charla.mensajes, pedido=pedido), ahora_s)
            return True

    def tomar_para_confirmar(self, conversacion: int, ahora: datetime) -> Toma | None:
        """R4: en un paso, el pedido completo sale de la charla (R22) y queda EN ESCRITURA.
        None si no hay pendiente o si ya hay otra toma en escritura."""
        ahora_s = _segundos(ahora)
        with self._transaccion("confirmaciones") as con:
            charla = _leer(con, conversacion, ahora_s)
            en_escritura = charla.toma is not None and not charla.toma.escrita
            if charla.pedido is None or not charla.pedido.completo or en_escritura:  # R1
                return None
            toma = Toma(pedido=charla.pedido, generacion=charla.generacion + 1)
            # R5: lo que se leyó antes de este paso ya no pisa ni reabre
            sql = "REPLACE INTO generaciones VALUES (?, ?, ?)"
            con.execute(sql, (conversacion, toma.generacion, ahora_s))
            datos = toma.model_dump_json(exclude={"escrita"})
            sql = "REPLACE INTO confirmaciones VALUES (?, ?, ?, ?, 0)"
            con.execute(sql, (conversacion, ahora_s, datos, self.epoca))  # R25
            _escribir(con, conversacion, Charla(mensajes=charla.mensajes), ahora_s)
            return toma

    def confirmar_escrito(self, conversacion: int, toma: Toma, ahora: datetime) -> None:
        """La fila quedó: la toma pasa a CONFIRMADO y la charla se cierra (R22)."""
        ahora_s = _segundos(ahora)
        with self._transaccion("confirmaciones") as con:
            charla = _leer(con, conversacion, ahora_s)
            if charla.toma == toma:  # R6, R7: queda como recién confirmado, con el TTL desde ahora
                sql = "UPDATE confirmaciones SET actualizada = ?, escrita = 1 WHERE conversacion = ?"
                con.execute(sql, (ahora_s, conversacion))
            if charla.pedido is None:
                con.execute("DELETE FROM charlas WHERE conversacion = ?", (conversacion,))
            else:  # R22: nació durante la escritura; se conserva, sin el historial del confirmado
                logger.warning("Memoria: nació un pedido durante la escritura; se conserva")
                _escribir(con, conversacion, Charla(pedido=charla.pedido), ahora_s)

    def devolver_a_pendiente(self, conversacion: int, toma: Toma, ahora: datetime) -> None:
        """R2: falló la planilla; el pedido vuelve a la charla con el marcador de la falla."""
        ahora_s = _segundos(ahora)
        with self._transaccion("confirmaciones") as con:
            charla = _leer(con, conversacion, ahora_s)
            if charla.toma != toma:  # ya escrita, liberada (R25) o de otra toma: no se devuelve
                logger.warning("Memoria: la toma ya no está en escritura; el pedido no vuelve")
                return
            con.execute("DELETE FROM confirmaciones WHERE conversacion = ?", (conversacion,))
            if charla.pedido is not None:  # como el bot viejo: queda el devuelto
                logger.warning("Memoria: falló la escritura con otro pedido en curso; queda el devuelto")
            marcador = Mensaje(role="assistant", content=f"[{CONFIRMACION_FALLIDA}]")  # R21
            mensajes = [*charla.mensajes, marcador][-self._tope :]  # R22
            _escribir(con, conversacion, Charla(mensajes=mensajes, pedido=toma.pedido), ahora_s)

    def marcar_procesado(self, id_mensaje: int, ahora: datetime) -> bool:
        """True si el id es nuevo (R23). Si la base falla, lanza en vez de dejar pasar (R24)."""
        with self._transaccion("procesados") as con:
            sql = "INSERT OR IGNORE INTO procesados VALUES (?, ?)"
            return con.execute(sql, (id_mensaje, _segundos(ahora))).rowcount == 1

    def desmarcar_procesado(self, id_mensaje: int) -> None:
        """R23: el proceso falló; un reintento del mismo id lo vuelve a procesar."""
        with self._transaccion("procesados") as con:
            con.execute("DELETE FROM procesados WHERE id_mensaje = ?", (id_mensaje,))

    def barrer(self, ahora: datetime) -> None:
        ahora_s = _segundos(ahora)
        with self._transaccion("barrido", checkpoint=True) as con:
            # R25: se libera sin devolver el pedido a pendiente; la fila pudo haberse escrito
            sql = "DELETE FROM confirmaciones WHERE escrita = 0 AND epoca != ? AND actualizada < ?"
            if con.execute(sql, (self.epoca, ahora_s - HUERFANA.total_seconds())).rowcount:
                logger.warning("Memoria: toma huérfana liberada")
            for tabla, columna, retencion in (
                ("charlas", "actualizada", TTL_CHARLA),
                ("confirmaciones", "actualizada", TTL_CHARLA),
                ("procesados", "marcado", RETENCION_PROCESADOS),
                ("generaciones", "actualizada", RETENCION_GENERACIONES),
            ):
                limite = ahora_s - retencion.total_seconds()
                con.execute(f"DELETE FROM {tabla} WHERE {columna} <= ?", (limite,))
