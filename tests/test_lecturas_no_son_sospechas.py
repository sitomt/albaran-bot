"""Leer bien un albarán no debe sonar a haber encontrado un error.

Caso real (Lucas Caballero, 04-05-2026): dos tipos de IVA impresos y un queso
cobrado al peso. Todo perfecto en el papel, y el bot decía haber "calculado"
base e IVA y "corregido" la cantidad. Los datos eran correctos; el relato no.
"""
from __future__ import annotations

from src.accounting_validation import ValidationReport
from src.ingestion_service import (
    _compuesto_de_tramos_visibles,
    _header_provenance_issues,
    _provenance_issues,
    _review_items,
    notas_informativas,
)

OCR_LUCAS = (
    "| QU049 | Queso Cremette cubo 3.5kg | | 1,000 | 3,500 | 9,80 | 10,00 | 8,82 | 30,87 |\n"
    "| RECIBÍ | BASE IMPONIBLE | I.V.A. | RECARGO | TOTAL |\n"
    "| | 307,53 | 10,00 | 30,75 | 370,38 |\n"
    "| | 30,87 | 4,00 | 1,23 | |"
)


def _header_dos_tramos() -> dict:
    return {
        "base_imponible": 338.40, "total_iva": 31.98, "total": 370.38,
        "detalle_iva": [
            {"tipo": 10, "base": 307.53, "cuota": 30.75},
            {"tipo": 4, "base": 30.87, "cuota": 1.23},
        ],
    }


def test_base_e_iva_sumados_de_tramos_impresos_no_generan_aviso():
    header = _header_dos_tramos()
    issues = _header_provenance_issues({"header": header, "lines": []}, OCR_LUCAS)

    assert "header_value_not_observed" not in {i.code for i in issues}
    # La trazabilidad no se pierde: queda anotado de dónde sale cada suma.
    assert header["decisiones"]["base_imponible"]["rule"] == "suma-de-tramos-iva"
    assert header["decisiones"]["base_imponible"]["suma"] == 338.40
    assert header["decisiones"]["total_iva"]["suma"] == 31.98


def test_con_un_solo_tramo_la_suma_no_cuenta_como_observada():
    header = {
        "base_imponible": 338.40, "total_iva": 31.98, "total": 370.38,
        "detalle_iva": [{"tipo": 10, "base": 338.40, "cuota": 31.98}],
    }
    # El OCR no imprime 338,40 ni 31,98: con un tramo no hay composición que valga.
    ocr = "| | 307,53 | 10,00 | 30,75 | 370,38 |"
    issues = _header_provenance_issues({"header": header, "lines": []}, ocr)
    assert {i.field for i in issues if i.code == "header_value_not_observed"} == {
        "base_imponible", "total_iva",
    }


def test_un_tramo_ilegible_mantiene_el_aviso():
    header = _header_dos_tramos()
    ocr_sin_la_cuota_del_4 = OCR_LUCAS.replace("1,23", "??")
    assert _compuesto_de_tramos_visibles("total_iva", header, ocr_sin_la_cuota_del_4) is None
    issues = _header_provenance_issues({"header": header, "lines": []}, ocr_sin_la_cuota_del_4)
    assert "total_iva" in {i.field for i in issues if i.code == "header_value_not_observed"}


def test_una_suma_que_no_cuadra_mantiene_el_aviso():
    header = _header_dos_tramos()
    header["base_imponible"] = 340.00  # los tramos suman 338,40, no 340
    assert _compuesto_de_tramos_visibles("base_imponible", header, OCR_LUCAS) is None


def _linea_queso() -> dict:
    return {
        "descripcion_limpia": "Queso Cremette Cubo",
        "cantidad": 3.5, "unidad": "kg", "precio_unitario": 8.82, "importe_neto": 30.87,
        "valores_observados": {
            "cantidad": 1.0, "unidad": "ud", "precio_neto": 8.82, "importe_neto": 30.87,
        },
    }


def test_linea_cobrada_al_peso_es_una_nota_no_una_correccion():
    line = _linea_queso()
    issues = _provenance_issues({"lines": [line]})

    codes = {i.code for i in issues}
    assert "line_priced_by_weight" in codes
    assert "line_quantity_adjusted" not in codes
    assert all(i.severity == "info" for i in issues if i.code == "line_priced_by_weight")
    assert line["decisiones"]["cantidad"]["rule"] == "cobrada-por-peso"

    # Una nota no abre revisión: nadie tiene que aceptar ni corregir nada.
    report = ValidationReport(issues=tuple(issues), line_sum=None, auto_confirmable=True)
    assert _review_items(report, "artifact", None) == []


def test_la_nota_dice_la_cuenta_completa():
    line = _linea_queso()
    _provenance_issues({"lines": [line]})
    [nota] = notas_informativas([line])
    assert nota == (
        "ℹ️ La línea 1 (Queso Cremette Cubo) se cobra por peso: 3,5 kg × 8,82€ = 30,87€."
    )


def test_cantidad_cambiada_que_no_cuadra_con_los_kilos_sigue_siendo_ajuste():
    line = _linea_queso()
    line["importe_neto"] = 31.50
    line["valores_observados"]["importe_neto"] = 31.50  # 3,5 × 8,82 = 30,87 ≠ 31,50
    issues = _provenance_issues({"lines": [line]})
    codes = {i.code for i in issues}
    assert "line_quantity_adjusted" in codes
    assert "line_priced_by_weight" not in codes


def test_cantidad_cambiada_en_unidades_sigue_siendo_ajuste():
    line = _linea_queso()
    line["unidad"] = "ud"
    issues = _provenance_issues({"lines": [line]})
    assert "line_quantity_adjusted" in {i.code for i in issues}
