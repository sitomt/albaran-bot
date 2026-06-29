"""
Verifica que las frases naturales de corrección se interpretan al campo y valor correctos.
El usuario responde con frases sencillas tipo "El precio del 1 es 4,84".
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bot import _parsear_correccion  # noqa: E402


def test_precio():
    assert _parsear_correccion("El precio del 1 es 4,84") == ("precio_unitario", 1, "4,84")


def test_importe():
    assert _parsear_correccion("El importe del 2 es 27,76") == ("importe_neto", 2, "27,76")


def test_cantidad():
    assert _parsear_correccion("La cantidad del 1 es 5,5") == ("cantidad", 1, "5,5")


def test_nombre_con_espacios():
    assert _parsear_correccion("El nombre del 1 es Longaniza Blanca") == (
        "descripcion_limpia", 1, "Longaniza Blanca",
    )


def test_kilos_alias_cantidad():
    assert _parsear_correccion("Los kilos del 3 son: 12") == ("cantidad", 3, "12")


def test_con_simbolo_euro():
    assert _parsear_correccion("El precio del 1 = 4,84 €") == ("precio_unitario", 1, "4,84 €")


def test_producto_dos_digitos():
    assert _parsear_correccion("El precio del 12 es 9,90") == ("precio_unitario", 12, "9,90")


def test_frase_sin_sentido_devuelve_none():
    assert _parsear_correccion("hola que tal") is None


def test_ok_no_es_correccion():
    # "ok" se gestiona aparte; aquí no debe parsearse como corrección.
    assert _parsear_correccion("ok") is None
