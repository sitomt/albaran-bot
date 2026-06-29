"""
Verifica que el precio NETO prevalece SIEMPRE (regla determinista de _resolver_precio_neto).

Simula "muchas situaciones" de columnas de precio en un albarán:
  - TARIFA + DTO% + NETO explícito  → debe usar el NETO de la columna.
  - TARIFA + DTO% sin columna neto   → debe calcular tarifa × (1 - dto/100).
  - Solo precio, sin descuento        → precio tal cual.
  - importe_neto que viene en BRUTO   → debe recalcularse al neto.
  - Línea en kg (cantidad = peso)     → el importe cuadra con precio × kg.
  - Caso real HERBAHER (chorizo)      → 6,80 con 15% dto → 5,78.

Añade aquí nuevos escenarios cuando aparezcan en albaranes reales.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.albaran_processor import LineaAlbaranLLM, _resolver_precio_neto, _validar_linea  # noqa: E402


def _linea(**kwargs) -> LineaAlbaranLLM:
    base = {"nombre_producto": "Producto", "cantidad": 1.0}
    base.update(kwargs)
    linea = LineaAlbaranLLM.model_validate(base)
    _resolver_precio_neto(linea)
    return linea


def test_columna_neto_explicita_prevalece():
    # TARIFA=7,74 | DTO=15% | NETO=6,58 → debe usar 6,58 (no 7,74, no recálculo 6,579)
    l = _linea(precio_tarifa=7.74, descuento_pct=15, precio_neto=6.58, cantidad=10, importe_neto=65.80)
    assert l.precio_unitario == 6.58
    assert l.importe_neto == 65.80


def test_sin_columna_neto_calcula_descuento():
    # precio 2,01 con 10% dto, sin columna neto → 2,01 × 0,90 = 1,809
    l = _linea(precio_tarifa=2.01, descuento_pct=10, precio_neto=None, cantidad=5)
    assert round(l.precio_unitario, 4) == 1.809
    assert l.importe_neto == round(1.809 * 5, 2)


def test_solo_precio_sin_descuento():
    l = _linea(precio_tarifa=3.50, descuento_pct=None, precio_neto=None, cantidad=4, importe_neto=14.0)
    assert l.precio_unitario == 3.50
    assert l.importe_neto == 14.0


def test_importe_bruto_se_recalcula_a_neto():
    # El LLM trae importe BRUTO (10 × 7,74 = 77,40) pero hay NETO 6,58 → importe debe ser 65,80
    l = _linea(precio_tarifa=7.74, descuento_pct=15, precio_neto=6.58, cantidad=10, importe_neto=77.40)
    assert l.precio_unitario == 6.58
    assert l.importe_neto == 65.80


def test_caso_real_herbaher_chorizo():
    # Bug original: TARIFA 6,80 metida en precio_unitario con 15% dto sin aplicar.
    # El LLM nuevo transcribiría tarifa=6,80, dto=15 y (si no hay col neto) se calcula.
    l = _linea(precio_tarifa=6.80, descuento_pct=15, precio_neto=None, cantidad=36, unidad="kg",
               peso_total_kg=36, importe_neto=244.80)
    assert round(l.precio_unitario, 2) == 5.78
    assert l.importe_neto == round(5.78 * 36, 2)


def test_linea_kg_importe_cuadra():
    l = _linea(precio_tarifa=10.0, descuento_pct=5, precio_neto=9.5, cantidad=12.5, unidad="kg",
               peso_total_kg=12.5, importe_neto=118.75)
    assert l.precio_unitario == 9.5
    assert l.importe_neto == 118.75
    ok, motivo = _validar_linea(l)
    assert ok, motivo


def test_validacion_detecta_descuento_no_aplicado():
    # Simula el bug: precio_unitario quedó igual a la tarifa pese a haber descuento.
    l = LineaAlbaranLLM.model_validate({
        "nombre_producto": "X", "cantidad": 1.0,
        "precio_tarifa": 6.80, "precio_unitario": 6.80, "descuento_pct": 15, "importe_neto": 6.80,
    })
    ok, motivo = _validar_linea(l)
    assert not ok
    assert "no aplicado" in motivo
