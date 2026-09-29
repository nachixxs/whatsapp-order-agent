"""Memoria del bot en SQLite: charla con marcadores, pedido en curso y dedup (R21 a R28)."""

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
TTL_CHARLA = timedelta(hours=6)
RETENCION_PROCESADOS = timedelta(days=8)  # R23
RETENCION_GENERACIONES = timedelta(days=30)  # R5

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS charlas (
    conversacion INTEGER PRIMARY KEY, actualizada REAL NOT NULL, datos TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS procesados (id_mensaje INTEGER PRIMARY KEY, marcado REAL NOT NULL);
CREATE TABLE IF NOT EXISTS generaciones (
    conversacion INTEGER PRIMARY KEY, generacion INTEGER NOT NULL, actualizada REAL NOT NULL);
"""


class ErrorMemoria(Exception):
    """La base falló. El mensaje lleva la tabla y el tipo de error, nunca un valor (R52)."""


class Mensaje(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["user", "assistant"]
    content: str


class Charla(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mensajes: list[Mensaje] = []
    pedido: Pedido | None = None  # R26: uno que ya no valida descarta la charla al leerla
    generacion: int = 0  # R5: no se guarda con la charla, sale de `generaciones`


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


def _leer(con: sqlite3.Connection, conversacion: int, ahora_s: float) -> Charla:
    clave = (conversacion,)
    fila = con.execute(
        "SELECT generacion FROM generaciones WHERE conversacion = ?", clave
    ).fetchone()
    vacia = Charla(generacion=fila[0] if fila else 0)
    fila = con.execute(
        "SELECT actualizada, datos FROM charlas WHERE conversacion = ?", clave
    ).fetchone()
    if fila is None or ahora_s - fila[0] >= TTL_CHARLA.total_seconds():  # R22
        return vacia
    try:
        charla = Charla.model_validate_json(fila[1])
    except ValidationError as error:  # R26: el error trae el valor adentro; va solo el tipo
        logger.warning("Memoria: ilegible descartado tabla=charlas error=%s", type(error).__name__)
        con.execute("DELETE FROM charlas WHERE conversacion = ?", clave)
        return vacia
    return charla.model_copy(update={"generacion": vacia.generacion})


def _escribir(con: sqlite3.Connection, conversacion: int, charla: Charla, ahora_s: float) -> None:
    datos = charla.model_dump_json(exclude={"generacion"})
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

    def cerrar_charla(self, conversacion: int, ahora: datetime) -> None:
        # R22: el pedido confirmado sale de la charla al tomarlo (CP3); si queda uno, nació
        # durante la escritura y se conserva
        ahora_s = _segundos(ahora)
        with self._transaccion("charlas") as con:
            pedido = _leer(con, conversacion, ahora_s).pedido
            if pedido is None:
                con.execute("DELETE FROM charlas WHERE conversacion = ?", (conversacion,))
            else:
                _escribir(con, conversacion, Charla(pedido=pedido), ahora_s)

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
            for tabla, columna, retencion in (
                ("charlas", "actualizada", TTL_CHARLA),
                ("procesados", "marcado", RETENCION_PROCESADOS),
                ("generaciones", "actualizada", RETENCION_GENERACIONES),
            ):
                limite = ahora_s - retencion.total_seconds()
                con.execute(f"DELETE FROM {tabla} WHERE {columna} <= ?", (limite,))
