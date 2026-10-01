"""Cada regla del SPECS (de la 1 a la 55) la nombra el docstring de algún test.

Las reglas van como constante: el CI no ve las notas donde vive el SPECS.
"""

import ast
import re
from pathlib import Path

REGLAS = frozenset(range(1, 56))
# Las del CP4 (primer contacto y nombre). Al cubrir una, se saca de acá
PENDIENTES: frozenset[int] = frozenset()
_TESTS = Path(__file__).parent


def _reglas_nombradas() -> set[int]:
    nombradas = set()
    for archivo in _TESTS.rglob("*.py"):
        for nodo in ast.walk(ast.parse(archivo.read_text(encoding="utf-8"))):
            if isinstance(nodo, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                docstring = ast.get_docstring(nodo) or ""
                nombradas |= {int(numero) for numero in re.findall(r"\bR(\d{1,2})\b", docstring)}
    return nombradas


def test_cada_regla_tiene_un_test_o_esta_pendiente():
    assert REGLAS - _reglas_nombradas() == PENDIENTES


def test_no_quedan_pendientes_de_confirmacion_planilla_memoria_ni_archivos():
    """El DoD del CP3: nada pendiente de §8 a §11 (reglas 1 a 36)."""
    assert not {regla for regla in PENDIENTES if regla <= 36}
