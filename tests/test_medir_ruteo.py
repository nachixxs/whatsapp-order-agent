import json
import re
import socket
from typing import Any

import anthropic
import pytest
from anthropic.types import Message
from anthropic.types.messages import MessageBatchIndividualResponse

from app import agente
from app.agente import MODELO
from app.config import ConfigNegocio
from app.tools import CAMPOS_DEL_PEDIDO, MATERIAL_A_DEFINIR, MOTIVOS_DERIVACION, TEMAS_CONSULTA, definir_tools
from scripts import medir_ruteo
from scripts.medir_ruteo import Caso, SetDorado, cargar_casos, puntuar, request_del_caso

DORADO = cargar_casos()
POR_ID = {caso.id: caso for caso in DORADO.casos}
_TODO_NULL = dict.fromkeys(agente.RegistrarPedido.model_fields)


@pytest.fixture(autouse=True)
def sin_cliente_real(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ningún test llega al cliente de verdad ni al .env de quien los corre."""

    def _explota() -> None:
        raise AssertionError("un test quiso armar el cliente real")

    monkeypatch.setattr(medir_ruteo, "_cliente_real", _explota)


def _tool(nombre: str, **argumentos: object) -> dict[str, Any]:
    return {"type": "tool_use", "id": "toolu_prueba", "name": nombre, "input": argumentos}


def _registrar(**campos: object) -> dict[str, Any]:
    return _tool("registrar_pedido", **_TODO_NULL | campos)


def _respuesta(*bloques: dict[str, Any], stop_reason: str = "tool_use") -> Message:
    return Message.model_validate({
        "id": "msg_prueba", "type": "message", "role": "assistant", "model": MODELO,
        "content": list(bloques), "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 700, "output_tokens": 120,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 3000},
    })


def _acierta(caso_id: str, *bloques: dict[str, Any], stop_reason: str = "tool_use") -> bool:
    return puntuar(DORADO, POR_ID[caso_id], _respuesta(*bloques, stop_reason=stop_reason)).acierto


# ── El set dorado ────────────────────────────────────────────────────────


def test_el_set_carga_con_entre_40_y_60_casos_de_id_unico() -> None:
    """R17: el set dorado sintético carga entero y cada caso se puede nombrar en un reporte."""
    assert 40 <= len(DORADO.casos) <= 60
    assert len(POR_ID) == len(DORADO.casos)


def test_cada_caso_arma_un_request_valido(config: ConfigNegocio) -> None:
    """R11, R20: el request de producción, con el historial alternado que arranca y termina en user."""
    for caso in DORADO.casos:
        request = request_del_caso(DORADO, caso, config)
        assert set(request) == {
            "model", "max_tokens", "thinking", "output_config", "system", "tools", "tool_choice", "messages"
        }, caso.id
        assert request["model"] == MODELO
        roles = [mensaje["role"] for mensaje in request["messages"]]
        assert roles[0] == roles[-1] == "user", caso.id
        assert all(anterior != siguiente for anterior, siguiente in zip(roles, roles[1:])), caso.id
        assert request["messages"][-1]["content"].endswith(caso.mensaje), caso.id


def test_el_prefijo_cacheable_es_el_mismo_en_todos_los_casos(config: ConfigNegocio) -> None:
    """R18: tools y bloque estático iguales en todo el lote; si no, la estimación de caché miente."""
    requests = [request_del_caso(DORADO, caso, config) for caso in DORADO.casos]
    assert all(request["tools"] == requests[0]["tools"] for request in requests)
    assert all(request["system"][0] == requests[0]["system"][0] for request in requests)
    assert requests[0]["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_cada_tool_argumento_y_valor_esperado_existe_en_las_tools(config: ConfigNegocio) -> None:
    """R11: la batería no espera nada que el modelo no pueda elegir."""
    assert medir_ruteo.errores_contra_tools(DORADO, definir_tools(config)) == []


@pytest.mark.parametrize(
    ("opcion", "error"),
    [
        ({"tool": "cotizar"}, "tool desconocida"),
        ({"tool": "derivar_a_asesor", "argumentos": {"motivo": "precio"}}, "fuera del enum"),
        ({"tool": "registrar_pedido", "argumentos": {"producto": "remeras"}}, "fuera del enum"),
        ({"tool": "registrar_pedido", "argumentos": {"telefono": "+54 11 5555-0000"}}, "no tiene telefono"),
    ],
)
def test_una_expectativa_imposible_se_detecta(config: ConfigNegocio, opcion: dict[str, Any], error: str) -> None:
    """R11, R13: una tool, un valor fuera del enum o un telefono esperado no pasan la validación del set."""
    dorado = SetDorado.model_validate({
        "ahora_por_defecto": "2026-10-06T10:00:00-03:00", "perfil_por_defecto": "Ana Prueba", "pedidos": {},
        "casos": [{"id": "Z01", "camino": "x", "descripcion": "x", "mensaje": "hola", "esperado": [opcion]}],
    })
    errores = medir_ruteo.errores_contra_tools(dorado, definir_tools(config))
    assert errores and error in errores[0]


def _claves(nodo: object) -> set[str]:
    if isinstance(nodo, dict):
        return set(nodo) | {clave for valor in nodo.values() for clave in _claves(valor)}
    if isinstance(nodo, list):
        return {clave for valor in nodo for clave in _claves(valor)}
    return set()


def test_ningun_caso_tiene_telefono(config: ConfigNegocio) -> None:
    """R13: telefono no figura en ningún lado del archivo del set, ni esperado ni en los pedidos."""
    crudo = json.loads(medir_ruteo.RUTA_CASOS.read_text(encoding="utf-8"))
    assert "telefono" not in _claves(crudo)


# Los que escribe turno.py (2.7) del lado del bot, tal cual quedan en el historial
_SIN_VALOR = (
    "pedido_pendiente_confirmacion", "producto_invalido", "fecha_invalida", "pedido_rechazado",
    "sin_pedido_para_confirmar", "sin_tool", "argumentos_invalidos", "tool_desconocida", "error_interno",
)
MARCADORES = {f"[{estado}]" for estado in _SIN_VALOR} | {
    f"[{estado}: {valor}]"
    for estado, valores in (
        ("dato_faltante", CAMPOS_DEL_PEDIDO), ("consulta_general", TEMAS_CONSULTA),
        ("derivado_a_asesor", MOTIVOS_DERIVACION),
    )
    for valor in valores
}


def test_los_turnos_del_bot_son_marcadores_reales() -> None:
    """R21: del lado del bot solo van marcadores, los mismos que escribe el turno; nunca prosa."""
    for caso in DORADO.casos:
        for mensaje in caso.historial:
            if mensaje.role == "assistant":
                assert mensaje.content in MARCADORES, caso.id


def test_el_set_cubre_los_cinco_caminos_temas_motivos_y_feriado(config: ConfigNegocio) -> None:
    """SPECS §3 y §6, R39: las 5 tools, los 8 temas, los 4 motivos y un caso en feriado."""
    opciones = [opcion for caso in DORADO.casos for opcion in caso.esperado]
    assert {opcion.tool for opcion in opciones} == {tool["name"] for tool in definir_tools(config)}
    assert {o.argumentos.get("tema") for o in opciones} >= set(TEMAS_CONSULTA)
    assert {o.argumentos.get("motivo") for o in opciones} >= set(MOTIVOS_DERIVACION)
    assert any(caso.ahora and caso.ahora.date() in config.feriados for caso in DORADO.casos)


def test_los_criticos_son_confirmacion_gracias_y_pulgar() -> None:
    """R16: todo 'gracias', 👍, 'sí' o 'dale' suelto y toda respuesta al resumen están marcados críticos."""
    sueltos = {"gracias!", "👍", "sí", "dale"}
    for caso in DORADO.casos:
        responde_al_resumen = caso.historial and caso.historial[-1].content == "[pedido_pendiente_confirmacion]"
        pregunta = caso.mensaje.endswith("?")
        if caso.mensaje in sueltos or (responde_al_resumen and not pregunta):
            assert caso.critico, caso.id
    assert {"C03", "C07", "S01", "S02", "M03"} <= {caso.id for caso in DORADO.casos if caso.critico}


# ── Puntuación ───────────────────────────────────────────────────────────


def test_el_pulgar_frente_al_resumen_acierta_solo_si_confirma() -> None:
    """R16: 👍 frente al resumen es confirmar_pedido(acepta=true); otra cosa es fallo."""
    assert _acierta("C03", _tool("confirmar_pedido", acepta=True))
    assert not _acierta("C03", _tool("confirmar_pedido", acepta=False))
    assert not _acierta("C03", _registrar())


def test_sin_tool_o_refusal_es_fallo() -> None:
    """R11: prosa, refusal o argumentos que no validan (un telefono) cuentan como fallo."""
    assert not _acierta("C01", {"type": "text", "text": "¡Listo!"}, stop_reason="end_turn")
    assert not _acierta("C01", _tool("confirmar_pedido", acepta=True), stop_reason="refusal")
    assert not _acierta("C01", _tool("confirmar_pedido", acepta=True, telefono="+54 11 5555-0000"))
    obtenido = puntuar(DORADO, POR_ID["C01"], _respuesta({"type": "text", "text": "hola"}, stop_reason="end_turn"))
    assert obtenido.obtenido == "sin_tool (sin_tool_use)"


def test_gracias_pelado_frente_al_resumen_acierta_si_no_cambia_el_pedido() -> None:
    """R16: 'gracias' frente al resumen no confirma; registrar_pedido sin cambios es acierto."""
    assert _acierta("C07", _registrar())
    assert _acierta("C07", _registrar(nombre_cliente="Ana Prueba", fecha_necesita="2026-10-16"))
    assert not _acierta("C07", _registrar(cantidad=300))
    assert not _acierta("C07", _tool("confirmar_pedido", acepta=True))


def test_el_material_a_definir_acepta_las_variantes_que_normaliza_el_pedido() -> None:
    """R15: 'A definir con el asesor.' se guarda igual que el literal; otro material es fallo."""
    assert _acierta("M03", _registrar(material=MATERIAL_A_DEFINIR))
    assert _acierta("M03", _registrar(material="A definir con el asesor."))
    assert not _acierta("M03", _registrar(material="lona"))
    assert not _acierta("M03", _tool("pedir_dato_faltante", dato="material"))


def test_otros_materiales_no_borra_el_que_ya_dijo() -> None:
    """R15: '¿qué otros materiales hay?' acierta sin material, con 'lona' o con el catálogo; nunca a definir."""
    assert _acierta("M09", _registrar())
    assert _acierta("M09", _registrar(material="Lona"))
    assert _acierta("M09", _tool("consulta_general", tema="catalogo"))
    assert not _acierta("M09", _registrar(material=MATERIAL_A_DEFINIR))
    assert not _acierta("M09", _tool("consulta_general", tema="horarios"))


def test_tener_lona_blanca_es_sin_stock() -> None:
    """R15: '¿tienen lona blanca?' es derivar_a_asesor(sin_stock), no registrar el material."""
    assert _acierta("M08", _tool("derivar_a_asesor", motivo="sin_stock"))
    assert not _acierta("M08", _registrar(material="lona blanca"))


def test_nombre_solo_sin_pedido_es_nombre_y_frente_al_resumen_no_cancela() -> None:
    """R16: el nombre solo va a nombre_cliente, sin inventar producto; frente al resumen no confirma ni cancela."""
    assert _acierta("N01", _registrar(nombre_cliente="Carla"))
    assert not _acierta("N01", _registrar(nombre_cliente="Carla", producto="impresion_digital"))
    assert not _acierta("N01", _tool("pedir_dato_faltante", dato="nombre_cliente"))
    assert _acierta("C09", _registrar(nombre_cliente="carla"))
    assert not _acierta("C09", _tool("confirmar_pedido", acepta=False))


def test_encargar_sin_decir_que_acepta_registrar_el_nombre_del_perfil() -> None:
    """R11: Q01 pide el producto o registra el nombre del perfil; inventar el producto es fallo."""
    assert _acierta("Q01", _tool("pedir_dato_faltante", dato="producto"))
    assert _acierta("Q01", _registrar(nombre_cliente="Ana Prueba"))
    assert not _acierta("Q01", _registrar(nombre_cliente="Ana Prueba", producto="impresion_digital"))
    assert not _acierta("Q01", _registrar(nombre_cliente="Carla"))


def test_fecha_relativa_y_campos_con_valor() -> None:
    """R14: la fecha se compara resuelta; con_valor pide que el campo venga, con cualquier valor."""
    assert _acierta("F01", _registrar(fecha_necesita="2026-10-09"))
    assert not _acierta("F01", _registrar(fecha_necesita="2026-10-16"))
    assert _acierta("P04", _registrar(medidas="5 x 5 cm"))
    assert not _acierta("P04", _registrar())


# ── Estimación ───────────────────────────────────────────────────────────


def test_el_estimador_no_toca_la_red(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """R53: --estimar, que es el default, no construye el cliente ni abre un socket."""

    def _sin_red(*_: object, **__: object) -> None:
        raise AssertionError("el estimador intentó usar la red")

    monkeypatch.setattr(socket.socket, "connect", _sin_red)
    monkeypatch.setattr(anthropic, "Anthropic", _sin_red)
    monkeypatch.setattr(agente, "_cliente", None)
    assert medir_ruteo.main([]) == 0
    assert medir_ruteo.main(["--estimar", "--corridas", "1"]) == 0
    salida = capsys.readouterr().out
    assert "llamadas: 180" in salida and "llamadas: 60" in salida
    assert "No se hizo ninguna llamada" in salida
    assert agente._cliente is None


def test_la_estimacion_escribe_la_cache_una_vez_y_lee_el_resto(config: ConfigNegocio) -> None:
    """R18: con caché, una escritura del prefijo por lote; sin caché, el techo es más caro."""
    requests = [request_del_caso(DORADO, caso, config) for caso in DORADO.casos[:4]]
    estimacion = medir_ruteo.estimar(requests, corridas=3)
    assert estimacion.llamadas == 12
    esperado = medir_ruteo.costo(
        estimacion.no_cacheables, estimacion.cacheables, estimacion.cacheables * 11, estimacion.salida
    )
    assert estimacion.con_cache == pytest.approx(esperado)
    assert 0 < estimacion.con_cache < estimacion.sin_cache


def test_el_costo_medido_usa_batch_y_los_factores_de_cache() -> None:
    """Costo medido: la mitad del precio de lista, con la caché escrita a 1,25x y la leída a 0,1x."""
    precio = medir_ruteo.PRECIO_ENTRADA
    assert medir_ruteo.costo(1_000_000, 0, 0, 0) == pytest.approx(precio * 0.5)
    assert medir_ruteo.costo(0, 1_000_000, 0, 0) == pytest.approx(precio * 1.25 * 0.5)
    assert medir_ruteo.costo(0, 0, 1_000_000, 0) == pytest.approx(precio * 0.1 * 0.5)
    assert medir_ruteo.costo(0, 0, 0, 1_000_000) == pytest.approx(medir_ruteo.PRECIO_SALIDA * 0.5)


# ── El lote, con un doble del SDK ────────────────────────────────────────


class _Estado:
    def __init__(self, status: str) -> None:
        self.processing_status = status
        self.request_counts = type("Cuenta", (), {"processing": 1, "succeeded": 0, "errored": 0})()


class _Lotes:
    def __init__(self, respuestas: dict[str, Message | str]) -> None:
        self.respuestas = respuestas  # custom_id -> Message, o el tipo de un resultado sin mensaje
        self.creados: list[list[dict[str, Any]]] = []
        self.estados = ["in_progress", "ended"]

    def create(self, *, requests: list[dict[str, Any]]) -> Any:
        self.creados.append(requests)
        return type("Lote", (), {"id": "msgbatch_prueba"})()

    def retrieve(self, lote: str) -> _Estado:
        return _Estado(self.estados.pop(0))

    def results(self, lote: str) -> list[MessageBatchIndividualResponse]:
        entradas = []
        for custom_id, respuesta in self.respuestas.items():
            if isinstance(respuesta, Message):
                resultado: dict[str, Any] = {"type": "succeeded", "message": respuesta.model_dump()}
            else:
                error = {"type": "error", "error": {"type": "api_error", "message": "falló"}, "request_id": None}
                resultado = {"type": respuesta, "error": error}
            entradas.append(MessageBatchIndividualResponse.model_validate(
                {"custom_id": custom_id, "result": resultado}
            ))
        return entradas


class _ClienteDeLotes:
    def __init__(self, respuestas: dict[str, Message | str]) -> None:
        self.messages = type("Mensajes", (), {})()
        self.messages.batches = _Lotes(respuestas)


CONFIRMA = _respuesta(_tool("confirmar_pedido", acepta=True))
DIRECCION = _respuesta(_tool("consulta_general", tema="direccion"))


def _correr(tmp_path: Any, cliente: _ClienteDeLotes, *extra: str) -> int:
    fallados = tmp_path / "fallados.json"
    fallados.write_text(json.dumps({"casos": ["C03", "G01"]}), encoding="utf-8")
    argv = ["--correr", "--solo-fallados", str(fallados), "--guardar-fallados", str(fallados), *extra]
    return medir_ruteo.main(argv, cliente=cliente, dormir=lambda _: None)


def test_un_lote_que_acierta_todo_pasa_y_manda_el_request_de_produccion(
    tmp_path: Any, config: ConfigNegocio, capsys: pytest.CaptureFixture[str]
) -> None:
    """R11, R18: el lote lleva el request de armar_request por caso y corrida; todo acierto sale 0."""
    respuestas = {f"{caso}-c{n}": r for n in (1, 2, 3) for caso, r in (("C03", CONFIRMA), ("G01", DIRECCION))}
    cliente = _ClienteDeLotes(respuestas)
    assert _correr(tmp_path, cliente) == 0
    (enviados,) = cliente.messages.batches.creados
    assert [pedido["custom_id"] for pedido in enviados] == ["G01-c1", "C03-c1", "G01-c2", "C03-c2", "G01-c3", "C03-c3"]
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", pedido["custom_id"]) for pedido in enviados)
    assert enviados[1]["params"] == request_del_caso(DORADO, POR_ID["C03"], config)
    salida = capsys.readouterr().out
    assert "Corrida 3: 2/2 = 100.0% [OK]" in salida
    assert "caché leída 18000" in salida
    assert json.loads((tmp_path / "fallados.json").read_text(encoding="utf-8")) == {"casos": []}


def test_un_critico_que_falla_una_vez_no_pasa_y_queda_en_fallados(
    tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """R16: un 👍 frente al resumen que falla en una sola corrida tumba la batería; sin tool es fallo."""
    sin_tool = _respuesta({"type": "text", "text": "¡Genial!"}, stop_reason="end_turn")
    respuestas = {
        "C03-c1": CONFIRMA, "C03-c2": sin_tool, "C03-c3": CONFIRMA,
        "G01-c1": DIRECCION, "G01-c2": DIRECCION, "G01-c3": "errored",
    }
    assert _correr(tmp_path, _ClienteDeLotes(respuestas)) == 1
    salida = capsys.readouterr().out
    assert "FALLO C03 corrida 2 [CRÍTICO]" in salida and "sin_tool (sin_tool_use)" in salida
    assert "FALLO G01 corrida 3: " in salida and "el lote no la procesó (errored)" in salida
    assert "NO PASA" in salida
    assert json.loads((tmp_path / "fallados.json").read_text(encoding="utf-8")) == {"casos": ["C03", "G01"]}


def test_un_resultado_que_falta_es_fallo(tmp_path: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """R11: un caso sin resultado no se cuenta como acierto."""
    respuestas = {f"{caso}-c{n}": r for n in (1, 2) for caso, r in (("C03", CONFIRMA), ("G01", DIRECCION))}
    assert _correr(tmp_path, _ClienteDeLotes(respuestas)) == 1
    assert "FALLO C03 corrida 3 [CRÍTICO]" in capsys.readouterr().out


def test_retomar_no_manda_otro_lote_y_toma_las_corridas_del_lote(
    tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Gasto: --retomar baja un lote ya pagado, sin crear otro; casos y corridas salen de sus custom_id."""
    cliente = _ClienteDeLotes({"C03-c1": CONFIRMA})
    argv = ["--retomar", "msgbatch_prueba", "--guardar-fallados", str(tmp_path / "f.json")]
    assert medir_ruteo.main(argv, cliente=cliente, dormir=lambda _: None) == 0
    assert cliente.messages.batches.creados == []
    assert "Corrida 1: 1/1 = 100.0% [OK]" in capsys.readouterr().out


def test_solo_fallados_con_un_caso_que_no_existe_no_corre(tmp_path: Any) -> None:
    """Gasto: un archivo de fallados de otro set corta antes de armar el lote."""
    ruta = tmp_path / "fallados.json"
    ruta.write_text(json.dumps({"casos": ["Z99"]}), encoding="utf-8")
    cliente = _ClienteDeLotes({})
    assert medir_ruteo.main(["--correr", "--solo-fallados", str(ruta)], cliente=cliente) == 2
    assert cliente.messages.batches.creados == []


def test_un_caso_con_varias_opciones_exige_por_que() -> None:
    """R17: más de una respuesta aceptable va justificada en el caso."""
    with pytest.raises(ValueError, match="sin por_que"):
        Caso.model_validate({
            "id": "Z01", "camino": "x", "descripcion": "x", "mensaje": "hola",
            "esperado": [{"tool": "confirmar_pedido"}, {"tool": "registrar_pedido"}],
        })
