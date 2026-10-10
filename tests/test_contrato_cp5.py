"""Contrato de aceptación del CP5 (CONTRATO-CP5.md): los casos marcados [test].

La interfaz que fijan estos tests para lo que todavía no existe (la implementa el dev):

- A, tope de mensajes (5.4):
  - `app.turno.TOPE_DE_MENSAJES = 30`: mensajes del cliente por conversación de Chatwoot. Cada mensaje
    nuevo del lote cuenta uno; el que llega con el contador ya en el tope no va a la API.
  - El motivo `"tope_de_mensajes"` sale por el mismo camino que los demás motivos de derivación:
    `respuestas.texto_derivacion("tope_de_mensajes", config)` es el texto fijo (detrás va
    `con_frase_de_horario`, R38) y `respuestas.nota_de_derivacion("tope_de_mensajes", ...)` es la nota.
    No es una constante nueva de `respuestas.py`, y no entra en el enum de `derivar_a_asesor` (A5).
  - `procesar_lote` devuelve `Resultado(texto, nota, derivar=True)`, como cualquier derivación (R47).
  - El contador vive en SQLite, por conversación: sobrevive a reabrir la `Memoria` sobre el mismo archivo.
- C, batería configurable (5.5): `scripts/medir_ruteo.py` suma `--config RUTA` y `--casos RUTA`. Con
  `--casos`, los fallados se guardan en la carpeta de los casos; con un `--casos` adentro del repo,
  `--correr` termina con un código distinto de 0 sin mandar el lote. Sin las dos opciones, como hoy.

B1 no tiene test propio: es que la suite entera pase. E2 guarda solo hashes: los textos prohibidos no
pueden figurar en el repo, ni siquiera acá.
"""

import hashlib
import json
import re
import shutil
import socket
import subprocess
from collections.abc import Callable, Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

import anthropic
import pytest
from anthropic.types import Message
from anthropic.types.messages import MessageBatchIndividualResponse

from app import agente, respuestas
from app.agente import MODELO, ConsultaGeneral, Decision
from app.chatwoot import Contacto, MensajeEntrante
from app.config import RAIZ, ConfigNegocio
from app.memoria import Memoria
from app.pedidos import Pedido
from app.prompt import bloques_de_sistema
from app.respuestas import con_frase_de_horario, nota_de_derivacion, respuesta_faq, texto_derivacion
from app.tools import CAMPOS_DEL_PEDIDO, ESTADOS_DISENO, MOTIVOS_DERIVACION, TEMAS_CONSULTA, definir_tools
from app.turno import Resultado, procesar_lote
from scripts import medir_ruteo
from tests.conftest import HORA_DE_PRUEBA, TELEFONO

TOPE_DECIDIDO = 30  # CONTRATO-CP5, decidido por Nacho el 2026-10-10
MOTIVO_TOPE = "tope_de_mensajes"
CONV = 73915
OTRA_CONV = 73916
HORARIOS = Decision("consulta_general", ConsultaGeneral(tema="horarios"))
INSTANTANEA = Path(__file__).parent / "fixtures" / "contrato_cp5_textos.json"
_IDS = iter(range(1, 1_000_000))


# ── A. Tope de mensajes ──────────────────────────────────────────────────


def _tope() -> int:
    """Se importa adentro: si falta, falla el caso A, no el archivo entero."""
    from app.turno import TOPE_DE_MENSAJES

    return int(TOPE_DE_MENSAJES)


class _Agente:
    """El `decidir` falso de tests/test_turno.py: siempre contesta horarios y cuenta las llamadas a la API."""

    def __init__(self) -> None:
        self.llamadas = 0

    def __call__(self, *_: Any, **__: Any) -> Decision:
        self.llamadas += 1
        return HORARIOS


def _sin_chatwoot(_: int, __: dict[str, object]) -> bool:
    return False


def _mensaje(conversacion: int) -> MensajeEntrante:
    """Sin atributos: el contacto llega sin registro (R41) y el turno no escribe en Chatwoot."""
    contacto = Contacto(nombre="Cliente Prueba", telefono=TELEFONO, id=4821, atributos=None)
    return MensajeEntrante(
        evento="message_created", id_mensaje=next(_IDS), contenido="¿a qué hora abren?",
        id_conversacion=conversacion, contacto=contacto, adjuntos=[], creado=None,
    )


def _escribir(memoria: Memoria, config: ConfigNegocio, api: _Agente, conversacion: int = CONV) -> Resultado:
    """Un mensaje del cliente, en su propio lote."""
    lote = [_mensaje(conversacion)]
    return procesar_lote(conversacion, lote, config, memoria, api, actualizar_contacto=_sin_chatwoot)


def _llenar(memoria: Memoria, config: ConfigNegocio, cuantos: int, conversacion: int = CONV) -> None:
    """`cuantos` mensajes del cliente, cada uno contestado normal y por la API."""
    api = _Agente()
    for _ in range(cuantos):
        assert _escribir(memoria, config, api, conversacion) == Resultado(respuesta_faq("horarios", config))
    assert api.llamadas == cuantos


def _derivado_por_tope(config: ConfigNegocio) -> Resultado:
    """R47, R38: el texto fijo del motivo con la frase de horario, la nota y el pase a una persona."""
    texto = con_frase_de_horario(texto_derivacion(MOTIVO_TOPE, config), config, HORA_DE_PRUEBA)
    return Resultado(texto, nota_de_derivacion(MOTIVO_TOPE, None, None, config), derivar=True)


@pytest.fixture
def memoria(tmp_path: Path) -> Iterator[Memoria]:
    abierta = Memoria(tmp_path / "memoria.db")
    yield abierta
    abierta.cerrar()


def test_a1_pasado_el_tope_deriva_sin_llamar_a_la_api(memoria: Memoria, config: ConfigNegocio) -> None:
    """A1 (R47, R19): con TOPE mensajes del cliente en la conversación, el que sigue no va a la API: contesta el
    texto fijo de derivación por tope, deja la nota con motivo tope_de_mensajes y pasa la conversación a open."""
    assert _tope() == TOPE_DECIDIDO
    _llenar(memoria, config, TOPE_DECIDIDO)
    api = _Agente()

    resultado = _escribir(memoria, config, api)

    assert api.llamadas == 0
    assert resultado == _derivado_por_tope(config)


def test_a2_el_mensaje_que_llega_al_tope_sigue_normal(memoria: Memoria, config: ConfigNegocio) -> None:
    """A2: con TOPE − 1 mensajes, el que llega al tope sigue igual que hoy y llama a la API."""
    _llenar(memoria, config, _tope() - 1)
    api = _Agente()

    resultado = _escribir(memoria, config, api)

    assert api.llamadas == 1
    assert resultado == Resultado(respuesta_faq("horarios", config))


def test_a3_una_conversacion_nueva_arranca_el_contador_de_cero(memoria: Memoria, config: ConfigNegocio) -> None:
    """A3 (R47): la conversación que llegó al tope se resolvió y Chatwoot abrió otra para el mismo cliente: en la
    nueva, los TOPE mensajes van a la API."""
    tope = _tope()
    _llenar(memoria, config, tope)
    api = _Agente()
    assert _escribir(memoria, config, api).derivar and api.llamadas == 0  # llegó al tope

    _llenar(memoria, config, tope, conversacion=OTRA_CONV)


def test_a4_el_contador_sobrevive_al_reinicio(tmp_path: Path, config: ConfigNegocio) -> None:
    """A4 (R27): el contador está en SQLite; reabrir la memoria sobre el mismo archivo lo conserva."""
    ruta = tmp_path / "memoria.db"
    antes = Memoria(ruta)
    try:
        _llenar(antes, config, _tope())
    finally:
        antes.cerrar()
    despues = Memoria(ruta)  # el bot se reinició
    api = _Agente()
    try:
        resultado = _escribir(despues, config, api)
    finally:
        despues.cerrar()

    assert api.llamadas == 0
    assert resultado == _derivado_por_tope(config)


def test_a5_el_modelo_no_puede_elegir_el_tope(config: ConfigNegocio) -> None:
    """A5 (R17, R19): tope_de_mensajes es un motivo de derivación del bot, con su texto y su nota, pero no está en
    el enum de derivar_a_asesor."""
    assert texto_derivacion(MOTIVO_TOPE, config).strip()
    assert nota_de_derivacion(MOTIVO_TOPE, None, None, config).strip()
    (derivar,) = [tool for tool in definir_tools(config) if tool["name"] == "derivar_a_asesor"]

    assert MOTIVO_TOPE not in derivar["input_schema"]["properties"]["motivo"]["enum"]


# ── B. Recortes sin cambio de comportamiento ─────────────────────────────

# B2: las entradas fijas con las que se arma la instantánea. Cambiarlas obliga a regenerarla desde main
_COMPLETO = Pedido(
    telefono=TELEFONO, producto="sellos", material="goma", medidas="3x2 cm", cantidad=3,
    fecha_necesita=date(2026, 10, 9), tiene_diseno="si", nombre_cliente="Ana Prueba",
)
_A_MEDIAS = Pedido(telefono=TELEFONO, producto="gran_formato", material="lona", cantidad=2)
_ZONA = HORA_DE_PRUEBA.tzinfo
_RELOJES = {  # R38: abierto, antes de abrir, de noche y el sábado con el lunes feriado
    "abierto": HORA_DE_PRUEBA,
    "antes_de_abrir": datetime(2026, 10, 6, 7, 0, tzinfo=_ZONA),
    "de_noche": datetime(2026, 10, 6, 20, 0, tzinfo=_ZONA),
    "sabado_con_feriado": datetime(2026, 10, 10, 14, 0, tzinfo=_ZONA),
}


def _prompts(config: ConfigNegocio) -> dict[str, Any]:
    feriado = datetime(2026, 10, 12, 10, 0, tzinfo=_ZONA)
    return {
        "sin_pedido": bloques_de_sistema(config, HORA_DE_PRUEBA, nombre_perfil="Ana Prueba", pedido=None),
        "a_medias_sin_perfil": bloques_de_sistema(
            config, HORA_DE_PRUEBA, nombre_perfil=None, pedido=_A_MEDIAS, nombre_preguntado=True
        ),
        "confirmado_en_feriado": bloques_de_sistema(
            config, feriado, nombre_perfil="Ana Prueba", pedido=_COMPLETO, confirmado=_COMPLETO
        ),
    }


def _constantes() -> dict[str, str]:
    """Los textos públicos de respuestas.py: los que turno y confirmacion mandan tal cual."""
    return {
        nombre: valor for nombre, valor in vars(respuestas).items()
        if not nombre.startswith("_") and nombre.isupper() and isinstance(valor, str)
    }


def _salidas(config: ConfigNegocio) -> dict[str, str | None]:
    """Lo que devuelven las funciones de respuestas.py con entradas fijas."""
    salidas: dict[str, str | None] = {}
    for tema in (*TEMAS_CONSULTA, "otro"):
        salidas[f"respuesta_faq({tema})"] = respuestas.respuesta_faq(tema, config)
    for dato in (*CAMPOS_DEL_PEDIDO, "otro"):
        salidas[f"pregunta_por_dato({dato})"] = respuestas.pregunta_por_dato(dato, config)
    for descartado in ("producto", "fecha_necesita", "nombre_cliente"):
        salidas[f"aviso_de_descartados({descartado})"] = respuestas.aviso_de_descartados(
            [descartado], _A_MEDIAS, config
        )
    for es_resumen in (True, False):
        salidas[f"con_aviso_de_material({es_resumen})"] = respuestas.con_aviso_de_material(
            "Texto.", es_resumen=es_resumen
        )
    for estado in ESTADOS_DISENO:
        pedido = _COMPLETO.model_copy(update={"tiene_diseno": estado})
        salidas[f"resumen_pedido({estado})"] = respuestas.resumen_pedido(pedido, config)
        salidas[f"nota_de_derivacion(sin_stock, {estado})"] = respuestas.nota_de_derivacion(
            "sin_stock", pedido, "Ana Prueba", config
        )
    for motivo in MOTIVOS_DERIVACION:
        salidas[f"texto_derivacion({motivo})"] = respuestas.texto_derivacion(motivo, config)
    for motivo in (*MOTIVOS_DERIVACION, "cambio_sobre_pedido_confirmado"):
        salidas[f"nota_de_derivacion({motivo})"] = respuestas.nota_de_derivacion(motivo, None, None, config)
    salidas["nota_de_derivacion(a_medias)"] = respuestas.nota_de_derivacion(
        "fuera_de_alcance", _A_MEDIAS, "Ana [Prueba](mention://x)", config
    )
    for nombre, reloj in _RELOJES.items():
        salidas[f"con_frase_de_horario({nombre})"] = respuestas.con_frase_de_horario("Texto.", config, reloj)
    salidas["nota_de_archivos(1)"] = respuestas.nota_de_archivos("diseno.pdf")
    salidas["nota_de_archivos(2)"] = respuestas.nota_de_archivos("diseno.pdf\nlogo.png")
    salidas["con_pregunta_del_nombre"] = respuestas.con_pregunta_del_nombre("Texto.")
    salidas["acuse_de_archivos(1, a_medias)"] = respuestas.acuse_de_archivos(1, _A_MEDIAS, config)
    salidas["acuse_de_archivos(2, a_medias)"] = respuestas.acuse_de_archivos(2, _A_MEDIAS, config)
    salidas["acuse_de_archivos(1, completo)"] = respuestas.acuse_de_archivos(1, _COMPLETO, config)
    for recibidos in (1, 2):
        salidas[f"acuse_de_archivos_despues_de_confirmar({recibidos})"] = (
            respuestas.acuse_de_archivos_despues_de_confirmar(recibidos)
        )
    return salidas


def textos_actuales(config: ConfigNegocio) -> dict[str, Any]:
    """B2: lo que se compara byte a byte. Con esto se generó la instantánea, desde main (628dae7)."""
    return {
        "prompt": _prompts(config),
        "tools": definir_tools(config),
        "constantes": _constantes(),
        "salidas": _salidas(config),
    }


def _json(valor: object) -> str:
    return json.dumps(valor, ensure_ascii=False, indent=2)  # sin sort_keys: el orden de las tools es R18


def test_b2_prompt_tools_y_textos_iguales_byte_a_byte_a_main(config: ConfigNegocio) -> None:
    """B2 (R17, R18, R19): el system prompt, las tools y los textos de respuestas.py son los de main (628dae7). Lo
    único nuevo permitido es el texto de A1, que entra por texto_derivacion y no está en la instantánea."""
    instantanea = json.loads(INSTANTANEA.read_text(encoding="utf-8"))
    actuales = textos_actuales(config)

    for clave in ("prompt", "tools", "constantes", "salidas"):
        assert _json(actuales[clave]).encode("utf-8") == _json(instantanea[clave]).encode("utf-8"), clave


def _lineas(archivo: Path) -> int:
    return archivo.read_bytes().count(b"\n")  # como wc -l


def test_b3_app_entra_en_el_presupuesto_de_lineas() -> None:
    """B3: app/ suma 2.730 líneas o menos (2.600 + 130 aprobadas) y ningún archivo pasa de 300."""
    archivos = {archivo.name: _lineas(archivo) for archivo in (RAIZ / "app").glob("*.py")}

    assert sum(archivos.values()) <= 2730
    assert {nombre: lineas for nombre, lineas in archivos.items() if lineas > 300} == {}


# ── C. Batería configurable ──────────────────────────────────────────────


@pytest.fixture
def sin_red(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ni el cliente real, ni el .env, ni un socket: los casos C corren sin red."""

    def _explota(*_: object, **__: object) -> None:
        raise AssertionError("un test quiso usar la red o el cliente real")

    monkeypatch.setattr(medir_ruteo, "_cliente_real", _explota)
    monkeypatch.setattr(socket.socket, "connect", _explota)
    monkeypatch.setattr(anthropic, "Anthropic", _explota)
    monkeypatch.setattr(agente, "_cliente", None)


def _medir(argv: list[str], **opciones: Any) -> int:
    """El código de salida de medir_ruteo.main; el de argparse si no conoce una opción."""
    try:
        return medir_ruteo.main(argv, dormir=lambda _: None, **opciones)
    except SystemExit as salida:
        return salida.code if isinstance(salida.code, int) else 2


def _casos(carpeta: Path, ids: tuple[str, ...] | None = None) -> Path:
    """Un set de casos fuera del repo: el del repo entero, o solo `ids`."""
    carpeta.mkdir(parents=True, exist_ok=True)
    crudo = json.loads(medir_ruteo.RUTA_CASOS.read_text(encoding="utf-8"))
    if ids is not None:
        crudo["casos"] = [caso for caso in crudo["casos"] if caso["id"] in ids]
    ruta = carpeta / "casos.json"
    ruta.write_text(json.dumps(crudo, ensure_ascii=False), encoding="utf-8")
    return ruta


def _config(carpeta: Path, **cambios: str) -> Path:
    """La config de ejemplo fuera del repo, con `cambios`."""
    carpeta.mkdir(parents=True, exist_ok=True)
    crudo = json.loads(medir_ruteo.RUTA_CONFIG.read_text(encoding="utf-8")) | cambios
    ruta = carpeta / "negocio.json"
    ruta.write_text(json.dumps(crudo, ensure_ascii=False), encoding="utf-8")
    return ruta


def _respuesta(tool: str, **argumentos: object) -> Message:
    return Message.model_validate({
        "id": "msg_prueba", "type": "message", "role": "assistant", "model": MODELO,
        "content": [{"type": "tool_use", "id": "toolu_prueba", "name": tool, "input": argumentos}],
        "stop_reason": "tool_use", "stop_sequence": None,
        "usage": {"input_tokens": 700, "output_tokens": 120,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 3000},
    })


CONFIRMA = _respuesta("confirmar_pedido", acepta=True)  # C03 acierta
DIRECCION = _respuesta("consulta_general", tema="direccion")  # G01 acierta
HORARIO = _respuesta("consulta_general", tema="horarios")  # G01 falla


class _Lotes:
    """El doble de client.messages.batches de tests/test_medir_ruteo.py."""

    def __init__(self, respuestas: dict[str, Message]) -> None:
        self.respuestas = respuestas
        self.creados: list[list[dict[str, Any]]] = []
        self.estados = ["in_progress", "ended"]

    def create(self, *, requests: list[dict[str, Any]]) -> Any:
        self.creados.append(requests)
        return type("Lote", (), {"id": "msgbatch_prueba"})()

    def retrieve(self, lote: str) -> Any:
        cuenta = type("Cuenta", (), {"processing": 1, "succeeded": 0, "errored": 0})()
        return type("Estado", (), {"processing_status": self.estados.pop(0), "request_counts": cuenta})()

    def results(self, lote: str) -> list[MessageBatchIndividualResponse]:
        return [
            MessageBatchIndividualResponse.model_validate(
                {"custom_id": custom_id, "result": {"type": "succeeded", "message": respuesta.model_dump()}}
            )
            for custom_id, respuesta in self.respuestas.items()
        ]


class _Cliente:
    def __init__(self, respuestas: dict[str, Message]) -> None:
        self.messages = type("Mensajes", (), {})()
        self.messages.batches = _Lotes(respuestas)


def _estimacion(salida: str) -> tuple[str, str, str]:
    """De la estimación impresa: la línea de casos, los tokens cacheables y el costo con caché."""
    casos = re.search(r"Casos: .*", salida)
    cacheables = re.search(r"cacheables ~(\d+)", salida)
    costo = re.search(r"Costo estimado .*US\$ ([0-9.]+)", salida)
    assert casos and cacheables and costo, salida
    return casos[0], cacheables[1], costo[1]


def _archivos(carpeta: Path) -> set[Path]:
    return {ruta for ruta in carpeta.rglob("*") if ruta.is_file()}


def _foto_del_repo() -> dict[Path, int]:
    """Donde la batería guarda hoy (la raíz) y la carpeta de los casos del repo, con su fecha de modificación."""
    rutas = [*RAIZ.iterdir(), *medir_ruteo.RUTA_CASOS.parent.iterdir()]
    return {ruta: ruta.stat().st_mtime_ns for ruta in rutas if ruta.is_file()}


@pytest.mark.usefixtures("sin_red")
def test_c1_estimar_usa_la_config_y_los_casos_dados(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """C1 (R17, R53): --config X --casos Y --estimar estima con la config X y los casos Y, imprime el costo y no
    llama a la API. X tiene una dirección 2.000 caracteres más larga: el prefijo cacheable crece."""
    casos = _casos(tmp_path / "privado", ("C03", "G01"))
    larga = _config(tmp_path / "larga", nombre="Imprenta Prueba", direccion="Calle Falsa 123 " + "x" * 2000)
    corta = _config(tmp_path / "corta")

    assert _medir(["--config", str(larga), "--casos", str(casos), "--estimar", "--corridas", "1"]) == 0
    con_la_larga = capsys.readouterr().out
    assert _medir(["--config", str(corta), "--casos", str(casos), "--estimar", "--corridas", "1"]) == 0
    con_la_corta = capsys.readouterr().out

    linea, cacheables_larga, costo = _estimacion(con_la_larga)
    assert linea.startswith("Casos: 2 · corridas: 1 · llamadas: 2") and float(costo) > 0
    assert int(cacheables_larga) > int(_estimacion(con_la_corta)[1]) + 500
    assert "No se hizo ninguna llamada" in con_la_larga
    assert agente._cliente is None


@pytest.mark.usefixtures("sin_red")
def test_c2_los_fallados_se_guardan_junto_a_los_casos(tmp_path: Path) -> None:
    """C2: una corrida con un caso fallado guarda los fallados en la carpeta de Y y no escribe nada en el repo."""
    carpeta = tmp_path / "privado" / "ruteo"
    casos = _casos(carpeta, ("C03", "G01"))
    config = _config(tmp_path / "config")
    antes, repo = _archivos(carpeta), _foto_del_repo()
    cliente = _Cliente({"C03-c1": CONFIRMA, "G01-c1": HORARIO})

    codigo = _medir(["--config", str(config), "--casos", str(casos), "--retomar", "msgbatch_prueba"], cliente=cliente)

    assert codigo == 1  # G01 falló: no pasa
    nuevos = _archivos(carpeta) - antes
    assert [json.loads(ruta.read_text(encoding="utf-8")) for ruta in nuevos] == [{"casos": ["G01"]}]
    assert _foto_del_repo() == repo


@pytest.mark.usefixtures("sin_red")
def test_c2_con_los_casos_adentro_del_repo_se_niega_a_correr(tmp_path: Path) -> None:
    """C2: con --casos adentro del repo, --correr termina con error sin mandar el lote; la misma corrida con
    los casos afuera sí lo manda."""
    afuera = _cliente_que_acierta()
    casos = _casos(tmp_path, ("C03", "G01"))
    assert _medir(["--correr", "--corridas", "1", "--casos", str(casos)], cliente=afuera) == 0
    assert len(afuera.messages.batches.creados) == 1

    adentro = _cliente_que_acierta()
    codigo = _medir(["--correr", "--corridas", "1", "--casos", str(medir_ruteo.RUTA_CASOS)], cliente=adentro)

    assert codigo != 0
    assert adentro.messages.batches.creados == []


def _cliente_que_acierta() -> _Cliente:
    return _Cliente({"C03-c1": CONFIRMA, "G01-c1": DIRECCION})


@pytest.mark.usefixtures("sin_red")
def test_c3_sin_config_ni_casos_es_como_hoy(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """C3 (R17): sin --config ni --casos, la estimación es la de la config de ejemplo con los casos del repo:
    igual a pasarle copias de esos dos archivos."""
    assert _medir(["--estimar", "--corridas", "1"]) == 0
    por_defecto = capsys.readouterr().out
    copias = ["--config", str(_config(tmp_path)), "--casos", str(_casos(tmp_path))]
    assert _medir([*copias, "--estimar", "--corridas", "1"]) == 0
    explicita = capsys.readouterr().out

    total = len(medir_ruteo.cargar_casos().casos)
    assert _estimacion(por_defecto)[0].startswith(f"Casos: {total} · corridas: 1 · llamadas: {total}")
    assert _estimacion(por_defecto) == _estimacion(explicita)


# ── E. Repo público ──────────────────────────────────────────────────────

# SHA-256 de cada texto prohibido en minúscula: el nombre del repo privado y datos de la imprenta real.
# Los textos no van acá: escribirlos sería la filtración que este test busca
_PROHIBIDOS = frozenset({
    "91eff07d654fa4df909f71beb328440aab1e942303cde59d2e70787ab30f46df",
    "5a90293aa097f4a9f8226fc86fe7309c68e8a932c8d7fc31600137e5af5cdfb2",
    "27f41d75b028f4449303a4c75b2fa824b6bd787cf4f62793afdb38277d81fc08",
    "c1432ff17862c17407b8b7beb21028e32c6ab524d7a3645c6ae25c07973ad5e6",
})
_LARGOS_DE_DIGITOS = (10,)  # los prohibidos que son números; un teléfono puede venir con prefijo y guiones


def _hash(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def _candidatos(texto: str) -> Iterator[str]:
    """Las palabras sueltas, las compuestas con guiones y cada tramo de dígitos de cada línea."""
    yield from re.findall(r"[a-z0-9]+", texto)
    yield from re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)+", texto)
    for linea in texto.splitlines():
        digitos = re.sub(r"\D", "", linea)
        for largo in _LARGOS_DE_DIGITOS:
            yield from (digitos[inicio:inicio + largo] for inicio in range(len(digitos) - largo + 1))


def _versionados() -> list[str]:
    git = shutil.which("git")
    assert git is not None, "E2 necesita git para listar los archivos versionados"
    salida = subprocess.run([git, "ls-files", "-z"], cwd=RAIZ, capture_output=True, check=True)
    return [ruta for ruta in salida.stdout.decode("utf-8").split("\0") if ruta]


def _leer(ruta: str) -> str:
    archivo = RAIZ / ruta
    return archivo.read_bytes().decode("utf-8", errors="ignore") if archivo.is_file() else ""


def _filtraciones(rutas: list[str], leer: Callable[[str], str], prohibidos: frozenset[str]) -> list[str]:
    return [
        ruta for ruta in rutas
        if any(_hash(candidato) in prohibidos for candidato in _candidatos(f"{ruta}\n{leer(ruta)}".lower()))
    ]


def test_e2_ningun_archivo_versionado_nombra_el_repo_privado_ni_la_imprenta_real() -> None:
    """E2 (R13): ni el nombre del repo privado ni los datos de la imprenta real (nombre, calle, teléfono) aparecen
    en ningún archivo versionado ni en su ruta. Junto con gitleaks en el CI."""
    # El buscador encuentra un texto de prueba en una palabra compuesta y un teléfono con prefijo y guiones
    de_prueba = frozenset({_hash("ejemplo-falso"), _hash("1155550000")})
    textos = {"a.txt": "Ver Ejemplo-Falso.", "b.txt": f"tel: {TELEFONO}", "c.txt": "ejemplo falso 115555000"}
    assert _filtraciones(list(textos), textos.__getitem__, de_prueba) == ["a.txt", "b.txt"]
    rutas = _versionados()
    assert "app/turno.py" in rutas

    assert _filtraciones(rutas, _leer, _PROHIBIDOS) == []
