"""Batería de ruteo (R17): estima el costo sin red o mide el set dorado por Message Batches.

Uso, desde la raíz del repo (PowerShell):
    .venv\\Scripts\\python.exe scripts\\medir_ruteo.py                         # --estimar: sin red, no gasta
    .venv\\Scripts\\python.exe scripts\\medir_ruteo.py --solo-fallados .ruteo-fallados.json
    .venv\\Scripts\\python.exe scripts\\medir_ruteo.py --correr --corridas 3   # gasta API: solo con OK
    .venv\\Scripts\\python.exe scripts\\medir_ruteo.py --correr --solo-fallados .ruteo-fallados.json
    .venv\\Scripts\\python.exe scripts\\medir_ruteo.py --retomar msgbatch_...  # baja un lote ya pagado

Los requests los arma `agente.armar_request`, igual que en producción. Una respuesta sin tool es fallo (R11).
"""

import argparse
import json
import math
import re
import sys
import time
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))  # corrido como script, `app` no está en el path

import anthropic  # noqa: E402
from anthropic.types import Message  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator  # noqa: E402

from app import agente  # noqa: E402
from app.agente import ARGUMENTOS_POR_TOOL, MODELO, Decision, armar_request, leer_respuesta  # noqa: E402
from app.config import ConfigNegocio, cargar_config  # noqa: E402
from app.memoria import Charla, Mensaje  # noqa: E402
from app.pedidos import Pedido  # noqa: E402
from app.tools import definir_tools  # noqa: E402

RUTA_CASOS = RAIZ / "tests" / "ruteo" / "casos.json"
RUTA_CONFIG = RAIZ / "config" / "negocio.ejemplo.json"
RUTA_FALLADOS = RAIZ / ".ruteo-fallados.json"
TELEFONO_FICTICIO = "+54 9 11 5555-0000"  # Pedido lo exige; no va al prompt (R13)

UMBRAL = 0.95  # por corrida; los críticos, bien en todas (ESTADO 2026-09-28)
CORRIDAS = 3

# Por millón de tokens, de https://platform.claude.com/docs/en/about-claude/pricing
# VERIFICAR: son los de Sonnet 5 (notas/tecnico/Ahorro-de-API-en-tests.md); los de claude-sonnet-5-5 no están
# confirmados. Si cambian, cambia solo esto.
PRECIO_ENTRADA = 2.00  # VERIFICAR
PRECIO_SALIDA = 10.00  # VERIFICAR
FACTOR_CACHE_ESCRITA = 1.25  # TTL de 5 minutos, el de cache_control ephemeral
FACTOR_CACHE_LEIDA = 0.10
FACTOR_BATCH = 0.50  # se suma con los de la caché
# Solo para estimar. Medido en el bot viejo: el JSON de las tools y las tildes dan ~2 caracteres por token
CARACTERES_POR_TOKEN = 2.0
TOKENS_PROMPT_DE_TOOLS = 354  # el que agrega la API con tool_choice auto (Sonnet 5). VERIFICAR
TOKENS_SALIDA_POR_LLAMADA = 300  # thinking adaptive + tool_use; VERIFICAR con el usage de la 2.9

INTERVALO_S = 30
ESPERA_MAXIMA_MIN = 120  # el lote sigue en la API; se baja después con --retomar
FALLOS_DE_CONSULTA = 5
_CUSTOM_ID = re.compile(r"^([A-Z][0-9]{2})-c([0-9]+)$")


class _Estricto(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Opcion(_Estricto):
    """Una decisión aceptable: la tool, y de sus argumentos solo los que importan."""

    tool: str
    argumentos: dict[str, Any] = {}  # null quiere decir "no lo mandó"
    con_valor: tuple[str, ...] = ()  # campos que tienen que venir, con cualquier valor
    no_cambia_el_pedido: bool = False  # todo lo que manda es igual a lo que el pedido ya tiene


class Caso(_Estricto):
    id: str = Field(pattern=r"^[A-Z][0-9]{2}$")
    camino: str
    descripcion: str
    reglas: tuple[str, ...] = ()
    critico: bool = False  # confirmación, "gracias" y 👍: bien en las tres corridas
    ahora: datetime | None = None
    nombre_perfil: str | None = None  # si no figura, el perfil por defecto; null es sin perfil (R43)
    nombre_preguntado: bool = False
    pedido: str | None = None
    confirmado: str | None = None
    historial: tuple[Mensaje, ...] = ()
    mensaje: str = Field(min_length=1)
    esperado: tuple[Opcion, ...] = Field(min_length=1)
    por_que: str | None = None

    @model_validator(mode="after")
    def _justificado(self) -> "Caso":
        if len(self.esperado) > 1 and not self.por_que:
            raise ValueError(f"{self.id}: más de una decisión aceptable sin por_que")
        return self


class SetDorado(_Estricto):
    ahora_por_defecto: datetime
    perfil_por_defecto: str
    pedidos: dict[str, dict[str, Any]]
    casos: tuple[Caso, ...]

    @model_validator(mode="after")
    def _coherente(self) -> "SetDorado":
        ids = [caso.id for caso in self.casos]
        if len(set(ids)) != len(ids):
            raise ValueError("ids de caso repetidos")
        for caso in self.casos:
            if (caso.ahora or self.ahora_por_defecto).tzinfo is None:
                raise ValueError(f"{caso.id}: ahora sin zona (R37)")
            for nombre in (caso.pedido, caso.confirmado):
                if nombre is not None and nombre not in self.pedidos:
                    raise ValueError(f"{caso.id}: pedido desconocido {nombre}")
        for nombre in self.pedidos:
            self.pedido(nombre)  # que cada pedido valide al cargar, no a mitad de un lote
        return self

    def pedido(self, nombre: str | None) -> Pedido | None:
        return None if nombre is None else Pedido(telefono=TELEFONO_FICTICIO, **self.pedidos[nombre])

    def perfil(self, caso: Caso) -> str | None:
        return caso.nombre_perfil if "nombre_perfil" in caso.model_fields_set else self.perfil_por_defecto


def cargar_casos(ruta: Path = RUTA_CASOS) -> SetDorado:
    return SetDorado.model_validate_json(ruta.read_text(encoding="utf-8"))


def request_del_caso(dorado: SetDorado, caso: Caso, config: ConfigNegocio) -> dict[str, Any]:
    charla = Charla(mensajes=[*caso.historial, Mensaje(role="user", content=caso.mensaje)])
    return armar_request(
        config, caso.ahora or dorado.ahora_por_defecto, charla, nombre_perfil=dorado.perfil(caso),
        pedido=dorado.pedido(caso.pedido), nombre_preguntado=caso.nombre_preguntado,
        confirmado=dorado.pedido(caso.confirmado),
    )


def _enum(propiedad: dict[str, Any]) -> list[Any] | None:
    for esquema in (propiedad, *propiedad.get("anyOf", ())):
        if "enum" in esquema:
            return list(esquema["enum"])
    return None


def errores_contra_tools(dorado: SetDorado, tools: list[dict[str, Any]]) -> list[str]:
    """Cada tool, clave y valor esperado existe en los esquemas que ve el modelo."""
    por_nombre = {tool["name"]: tool for tool in tools}
    errores = []
    for caso in dorado.casos:
        for opcion in caso.esperado:
            tool = por_nombre.get(opcion.tool)
            if tool is None or opcion.tool not in ARGUMENTOS_POR_TOOL:
                errores.append(f"{caso.id}: tool desconocida {opcion.tool}")
                continue
            propiedades = tool["input_schema"]["properties"]
            for clave in (*opcion.argumentos, *opcion.con_valor):
                if clave not in propiedades:
                    errores.append(f"{caso.id}: {opcion.tool} no tiene {clave}")
            for clave, valor in opcion.argumentos.items():
                valores = _enum(propiedades.get(clave, {}))
                if valor is not None and valores is not None and valor not in valores:
                    errores.append(f"{caso.id}: {clave}={valor} fuera del enum")
            try:
                ARGUMENTOS_POR_TOOL[opcion.tool].model_validate(opcion.argumentos)
            except ValidationError:
                errores.append(f"{caso.id}: argumentos que no validan en {opcion.tool}")
    return errores


def _normalizar(valor: object) -> object:
    if isinstance(valor, date):
        return valor.isoformat()
    if not isinstance(valor, str):
        return valor
    # Como pedidos._material_a_definir: mayúsculas, espacios y el punto final no cambian el valor
    sin_tildes = "".join(c for c in unicodedata.normalize("NFKD", valor) if not unicodedata.combining(c))
    return " ".join(sin_tildes.casefold().split()).rstrip(".")


def _coincide(opcion: Opcion, tool: str, argumentos: dict[str, Any], pedido: Pedido | None) -> bool:
    if tool != opcion.tool:
        return False
    if any(_normalizar(argumentos.get(clave)) != _normalizar(valor) for clave, valor in opcion.argumentos.items()):
        return False
    if any(argumentos.get(clave) is None for clave in opcion.con_valor):
        return False
    if opcion.no_cambia_el_pedido:
        actual = pedido.model_dump() if pedido is not None else {}
        return all(
            valor is None or _normalizar(valor) == _normalizar(actual.get(clave))
            for clave, valor in argumentos.items()
        )
    return True


def _describir(tool: str, argumentos: dict[str, Any]) -> str:
    return f"{tool}({', '.join(f'{clave}={valor!r}' for clave, valor in argumentos.items() if valor is not None)})"


def describir_esperado(caso: Caso) -> str:
    textos = []
    for opcion in caso.esperado:
        partes = [f"{clave}={valor!r}" for clave, valor in opcion.argumentos.items()]  # acá None sí dice algo
        partes += [f"{clave}=<algo>" for clave in opcion.con_valor]
        partes += ["sin cambiar el pedido"] if opcion.no_cambia_el_pedido else []
        textos.append(f"{opcion.tool}({', '.join(partes)})")
    return " | ".join(textos)


@dataclass(frozen=True)
class Puntaje:
    acierto: bool
    obtenido: str


def puntuar(dorado: SetDorado, caso: Caso, respuesta: Message) -> Puntaje:
    resultado = leer_respuesta(respuesta)
    if not isinstance(resultado, Decision):
        return Puntaje(False, f"sin_tool ({resultado.motivo})")  # R11: la prosa es fallo
    argumentos = resultado.argumentos.model_dump()
    pedido = dorado.pedido(caso.pedido)
    acierto = any(_coincide(opcion, resultado.tool, argumentos, pedido) for opcion in caso.esperado)
    return Puntaje(acierto, _describir(resultado.tool, argumentos))


def costo(entrada: int, cache_escrita: int, cache_leida: int, salida: int) -> float:
    """En dólares, con el descuento de Batch."""
    tokens_entrada = entrada + cache_escrita * FACTOR_CACHE_ESCRITA + cache_leida * FACTOR_CACHE_LEIDA
    return FACTOR_BATCH * (tokens_entrada * PRECIO_ENTRADA + salida * PRECIO_SALIDA) / 1_000_000


def _tokens(texto: str) -> int:
    return math.ceil(len(texto) / CARACTERES_POR_TOKEN)


@dataclass(frozen=True)
class Estimacion:
    llamadas: int
    cacheables: int  # por llamada: tools + bloque estático, iguales en todas (R18)
    no_cacheables: int  # suma de todas las llamadas: bloque dinámico + historial
    salida: int
    con_cache: float
    sin_cache: float


def estimar(requests: Sequence[dict[str, Any]], corridas: int) -> Estimacion:
    """Un lote: la primera llamada escribe el prefijo en la caché y las demás lo leen."""
    if not requests:
        return Estimacion(0, 0, 0, 0, 0.0, 0.0)
    primero = requests[0]
    cacheables = TOKENS_PROMPT_DE_TOOLS + _tokens(
        json.dumps(primero["tools"], ensure_ascii=False) + primero["system"][0]["text"]
    )
    por_corrida = sum(
        _tokens(request["system"][1]["text"] + json.dumps(request["messages"], ensure_ascii=False))
        for request in requests
    )
    llamadas = len(requests) * corridas
    no_cacheables, salida = por_corrida * corridas, TOKENS_SALIDA_POR_LLAMADA * llamadas
    return Estimacion(
        llamadas, cacheables, no_cacheables, salida,
        con_cache=costo(no_cacheables, cacheables, cacheables * (llamadas - 1), salida),
        sin_cache=costo(no_cacheables + cacheables * llamadas, 0, 0, salida),
    )


def imprimir_estimacion(estimacion: Estimacion, casos: int, corridas: int) -> None:
    llamadas = estimacion.llamadas
    promedio = estimacion.no_cacheables // max(llamadas, 1)
    print(f"Batería de ruteo, estimación sin red · modelo {MODELO}")
    print(f"Casos: {casos} · corridas: {corridas} · llamadas: {llamadas}, en un solo lote de Batch")
    print(
        f"Tokens por llamada (~{CARACTERES_POR_TOKEN} caracteres por token): cacheables ~{estimacion.cacheables} "
        f"(tools + bloque estático + {TOKENS_PROMPT_DE_TOOLS} del prompt de tool use), "
        f"no cacheables ~{promedio} de promedio, salida ~{TOKENS_SALIDA_POR_LLAMADA} supuesta"
    )
    print(
        f"Precios (VERIFICAR): US$ {PRECIO_ENTRADA:.2f} entrada / {PRECIO_SALIDA:.2f} salida por millón · "
        f"Batch x{FACTOR_BATCH} · caché escrita x{FACTOR_CACHE_ESCRITA}, leída x{FACTOR_CACHE_LEIDA}"
    )
    print(
        f"Costo estimado con Batch y caché (1 escritura, {max(llamadas - 1, 0)} lecturas): "
        f"US$ {estimacion.con_cache:.4f}"
    )
    print(f"Techo con Batch y sin caché (en un lote la caché es best-effort): US$ {estimacion.sin_cache:.4f}")


def custom_id(caso: str, corrida: int) -> str:
    return f"{caso}-c{corrida}"


@dataclass
class Informe:
    corridas: int
    puntajes: dict[tuple[str, int], Puntaje] = field(default_factory=dict)
    uso: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(("entrada", "cache_escrita", "cache_leida", "salida"), 0)
    )

    def sumar_uso(self, respuesta: Message) -> None:
        uso = respuesta.usage
        self.uso["entrada"] += uso.input_tokens
        self.uso["cache_escrita"] += uso.cache_creation_input_tokens or 0
        self.uso["cache_leida"] += uso.cache_read_input_tokens or 0
        self.uso["salida"] += uso.output_tokens


def puntuar_lote(dorado: SetDorado, casos: Sequence[Caso], corridas: int, resultados: Iterable[Any]) -> Informe:
    por_id = {caso.id: caso for caso in casos}
    informe = Informe(corridas)
    for entrada in resultados:
        partes = _CUSTOM_ID.fullmatch(entrada.custom_id)
        if partes is None or partes[1] not in por_id or not 1 <= int(partes[2]) <= corridas:
            print(f"Resultado que no es de este set: {entrada.custom_id}")
            continue
        clave = (partes[1], int(partes[2]))
        if entrada.result.type != "succeeded":
            informe.puntajes[clave] = Puntaje(False, f"el lote no la procesó ({entrada.result.type})")
            continue
        informe.sumar_uso(entrada.result.message)
        informe.puntajes[clave] = puntuar(dorado, por_id[clave[0]], entrada.result.message)
    for caso in casos:
        for corrida in range(1, corridas + 1):
            informe.puntajes.setdefault((caso.id, corrida), Puntaje(False, "sin resultado"))
    return informe


def reportar(informe: Informe, casos: Sequence[Caso]) -> tuple[bool, list[str]]:
    """Imprime el resultado. Devuelve si pasa el umbral y los ids que fallaron alguna vez."""
    pasa = True
    for corrida in range(1, informe.corridas + 1):
        aciertos = sum(informe.puntajes[(caso.id, corrida)].acierto for caso in casos)
        proporcion = aciertos / len(casos)
        pasa = pasa and proporcion >= UMBRAL
        marca = "OK" if proporcion >= UMBRAL else f"BAJO EL {UMBRAL:.0%}"
        print(f"Corrida {corrida}: {aciertos}/{len(casos)} = {proporcion:.1%} [{marca}]")
    fallados = []
    for caso in casos:
        for corrida in range(1, informe.corridas + 1):
            puntaje = informe.puntajes[(caso.id, corrida)]
            if puntaje.acierto:
                continue
            pasa = pasa and not caso.critico
            if caso.id not in fallados:
                fallados.append(caso.id)
            critico = " [CRÍTICO]" if caso.critico else ""
            print(f"FALLO {caso.id} corrida {corrida}{critico}: esperado {describir_esperado(caso)}"
                  f" · obtenido {puntaje.obtenido}")
    uso = informe.uso
    print(
        f"Uso medido: entrada {uso['entrada']}, caché escrita {uso['cache_escrita']}, "
        f"caché leída {uso['cache_leida']}, salida {uso['salida']}"
    )
    print(f"Costo medido con Batch: US$ {costo(**uso):.4f} (precios VERIFICAR)")
    print("PASA" if pasa else "NO PASA: alguna corrida bajo el umbral o un caso crítico falló")
    return pasa, fallados


def _esperar(cliente: Any, lote: str, espera_s: float, dormir: Callable[[float], None]) -> bool:
    inicio, fallos = time.monotonic(), 0
    while True:
        try:
            estado = cliente.messages.batches.retrieve(lote)
            fallos = 0
            if estado.processing_status == "ended":
                return True
            cuenta = estado.request_counts
            print(f"Lote {lote}: {estado.processing_status} · procesando {cuenta.processing}, "
                  f"listas {cuenta.succeeded}, con error {cuenta.errored}")
        except anthropic.APIError as error:  # sin reintentos del SDK (R12): se reintenta acá
            fallos += 1
            print(f"No se pudo consultar el lote ({type(error).__name__}), intento {fallos}/{FALLOS_DE_CONSULTA}")
            if fallos >= FALLOS_DE_CONSULTA:
                return False
        if time.monotonic() - inicio > espera_s:
            return False
        dormir(INTERVALO_S)


def _del_lote(casos: Sequence[Caso], resultados: Sequence[Any], corridas: int) -> tuple[list[Caso], int]:
    """--retomar: los casos y las corridas salen del lote, no de los argumentos de hoy."""
    vistos = [partes for r in resultados if (partes := _CUSTOM_ID.fullmatch(r.custom_id))]
    if not vistos:
        return list(casos), corridas
    ids = {partes[1] for partes in vistos}
    return [caso for caso in casos if caso.id in ids], max(int(partes[2]) for partes in vistos)


def _cliente_real() -> Any:
    from dotenv import load_dotenv

    load_dotenv(RAIZ / ".env")  # ruta explícita: sin ruta, desde un worktree agarra el .env de otro checkout
    return agente.cliente_api()


def _guardar_fallados(ruta: Path, fallados: list[str]) -> None:
    ruta.write_text(json.dumps({"casos": sorted(fallados)}, indent=2) + "\n", encoding="utf-8")
    print(f"Fallados guardados en {ruta} ({len(fallados)}): úsalo con --solo-fallados")


def _elegir(dorado: SetDorado, ruta: Path | None) -> list[Caso]:
    if ruta is None:
        return list(dorado.casos)
    ids = json.loads(ruta.read_text(encoding="utf-8"))["casos"]
    desconocidos = sorted(set(ids) - {caso.id for caso in dorado.casos})
    if desconocidos:
        raise ValueError(f"casos que no están en el set: {', '.join(desconocidos)}")
    return [caso for caso in dorado.casos if caso.id in ids]


def _argumentos(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batería de ruteo. Sin --correr no toca la red.")
    modo = parser.add_mutually_exclusive_group()
    modo.add_argument("--estimar", action="store_true", help="por defecto: estima el costo sin red")
    modo.add_argument("--correr", action="store_true", help="manda un lote por Batch API (gasta)")
    modo.add_argument("--retomar", metavar="ID_DEL_LOTE", help="espera y puntúa un lote ya mandado")
    parser.add_argument("--corridas", type=int, default=CORRIDAS)
    parser.add_argument("--solo-fallados", type=Path, metavar="ARCHIVO")
    parser.add_argument("--guardar-fallados", type=Path, default=RUTA_FALLADOS, metavar="ARCHIVO")
    parser.add_argument("--espera-maxima", type=float, default=ESPERA_MAXIMA_MIN, metavar="MINUTOS")
    argumentos = parser.parse_args(argv)
    if argumentos.corridas < 1:
        parser.error("--corridas tiene que ser al menos 1")
    return argumentos


def main(
    argv: Sequence[str] | None = None, *, cliente: Any = None, dormir: Callable[[float], None] = time.sleep
) -> int:
    """0 pasa, 1 no pasa el umbral, 2 no se pudo medir."""
    argumentos = _argumentos(argv)
    config, dorado = cargar_config(RUTA_CONFIG), cargar_casos()
    errores = errores_contra_tools(dorado, definir_tools(config))
    if errores:
        print("El set dorado no coincide con las tools:\n" + "\n".join(errores))
        return 2
    try:
        casos = _elegir(dorado, argumentos.solo_fallados)
    except (OSError, ValueError, KeyError) as error:
        print(f"No se pudo leer --solo-fallados: {error}")
        return 2
    if not casos:
        print("No hay casos para medir.")
        return 0
    requests = [request_del_caso(dorado, caso, config) for caso in casos]
    corridas = argumentos.corridas
    if not (argumentos.correr or argumentos.retomar):
        imprimir_estimacion(estimar(requests, corridas), len(casos), corridas)
        print("No se hizo ninguna llamada. Correr gasta API: --correr, y solo con OK.")
        return 0
    try:
        cliente = cliente if cliente is not None else _cliente_real()
    except agente.ErrorCredencial as error:
        print(f"Sin cliente: {error}")  # mensaje fijo, nunca la clave (R53)
        return 2
    lote = argumentos.retomar
    if lote is None:
        imprimir_estimacion(estimar(requests, corridas), len(casos), corridas)
        pedidos = [
            {"custom_id": custom_id(caso.id, corrida), "params": request}
            for corrida in range(1, corridas + 1)
            for caso, request in zip(casos, requests)
        ]
        try:
            lote = cliente.messages.batches.create(requests=pedidos).id
        except anthropic.APIError as error:
            print(f"No se pudo crear el lote: {type(error).__name__} {getattr(error, 'status_code', '')}")
            return 2
        print(f"Lote creado: {lote}. Si la espera se corta, se baja con --retomar {lote}")
    if not _esperar(cliente, lote, argumentos.espera_maxima * 60, dormir):
        print(f"El lote {lote} no terminó a tiempo; sigue en la API: --retomar {lote}")
        return 2
    try:
        resultados = list(cliente.messages.batches.results(lote))
    except anthropic.APIError as error:
        print(f"No se pudieron bajar los resultados ({type(error).__name__}): --retomar {lote}")
        return 2
    if argumentos.retomar:
        casos, corridas = _del_lote(casos, resultados, corridas)
    informe = puntuar_lote(dorado, casos, corridas, resultados)
    pasa, fallados = reportar(informe, casos)
    _guardar_fallados(argumentos.guardar_fallados, fallados)
    return 0 if pasa else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # la consola de Windows es cp1252
    sys.exit(main())
