from types import SimpleNamespace

from src.accounting_validation import validate_candidate
from src.albaran_processor import AlbaranLLM
from src.ingestion_service import (
    ValidationReport, _header_provenance_issues, _page_text, _provenance_issues,
    _review_items,
)


def _candidate(**changes):
    data = {
        "proveedor_nombre": "Proveedor",
        "fecha": "2026-06-01",
        "base_imponible": 100.0,
        "total_iva": 10.0,
        "total": 110.0,
        "detalle_iva": [{"tipo": 10, "base": 100.0, "cuota": 10.0}],
        "lineas": [
            {"nombre_producto": "Tomate", "cantidad": 10, "precio_unitario": 10,
             "importe_neto": 100, "descuento_pct": 0, "confianza": 99},
        ],
    }
    data.update(changes)
    return SimpleNamespace(**data)


def test_candidate_contablemente_cuadrado_puede_autoconfirmarse():
    report = validate_candidate(_candidate(), ocr_confidence=0.98)
    assert report.auto_confirmable
    assert not report.issues


def test_base_mas_iva_debe_ser_total():
    report = validate_candidate(_candidate(total=109.0), ocr_confidence=0.98)
    assert not report.auto_confirmable
    assert "document_total_mismatch" in {issue.code for issue in report.issues}


def test_truncado_nunca_se_autoconfirma():
    report = validate_candidate(_candidate(), extraction_complete=False, ocr_confidence=0.98)
    assert not report.auto_confirmable
    assert "extraction_incomplete" in {issue.code for issue in report.issues}


def test_manuscrito_nunca_se_autoconfirma():
    report = validate_candidate(_candidate(), document_is_handwritten=True, ocr_confidence=0.98)
    assert not report.auto_confirmable
    assert "handwritten_document" in {issue.code for issue in report.issues}


def test_confianza_ausente_es_revision():
    report = validate_candidate(_candidate(), ocr_confidence=None)
    assert not report.auto_confirmable
    assert "ocr_confidence_missing" in {issue.code for issue in report.issues}


def test_detecta_total_inconsistente_sin_confundirlo_con_albaran_real_juanín():
    candidate = _candidate(
        base_imponible=421.0,
        # Caso sintético: la fotografía real indica 42,10 € de IVA y sí cuadra.
        total_iva=47.1,
        total=463.1,
        detalle_iva=[],
        lineas=[
            {"nombre_producto": "Atún", "cantidad": 25, "precio_unitario": 14.95,
             "importe_neto": 373.75, "confianza": 70},
            {"nombre_producto": "5 Henda CIP", "cantidad": 5, "precio_unitario": 9.45,
             "importe_neto": 47.25, "confianza": 70},
        ],
    )
    report = validate_candidate(candidate, document_is_handwritten=True, ocr_confidence=0.8)
    codes = {issue.code for issue in report.issues}
    assert "document_total_mismatch" in codes
    assert "line_confidence_low" in codes


def test_campos_ilegibles_crean_candidato_revisable_en_vez_de_romper_pipeline():
    model = AlbaranLLM.model_validate({
        "proveedor_nombre": None,
        "fecha": None,
        "base_imponible": None,
        "total": None,
        "lineas": [{
            "nombre_producto": None, "cantidad": None,
            "precio_unitario": None, "importe_neto": None, "confianza": None,
        }],
    })
    report = validate_candidate(model, ocr_confidence=0.4)
    codes = {issue.code for issue in report.issues}
    assert {"supplier_missing", "date_invalid", "line_description_missing",
            "line_quantity_invalid", "line_price_invalid", "line_amount_invalid"} <= codes


def test_un_valor_calculado_se_muestra_como_decision_no_como_observado():
    issues = _provenance_issues({"lines": [{
        "cantidad": 10,
        "precio_unitario": 9,
        "importe_neto": 90,
        "valores_observados": {
            "cantidad": 10, "precio_tarifa": 10,
            "descuento_pct": 10, "precio_neto": None, "importe_neto": None,
        },
    }]})
    codes = {issue.code for issue in issues}
    assert "line_price_derived" in codes
    assert "line_amount_derived" in codes


def test_ocr_inserta_tablas_separadas_en_lugar_de_enviar_solo_el_enlace():
    page = SimpleNamespace(markdown="Cabecera\n\n[tbl-0.html](tbl-0.html)\n\nPie")
    text = _page_text(page, {
        "tables": [{"id": "tbl-0.html", "content": "<table><tr><td>Total</td><td>110,00</td></tr></table>"}]
    })
    assert "tbl-0.html" not in text
    assert "<td>110,00</td>" in text


def test_redondeo_de_neto_impreso_usa_tarifa_y_descuento_solo_para_validar():
    candidate = _candidate(lineas=[{
        "nombre_producto": "Vino", "cantidad": 12,
        "precio_tarifa": 1.03, "precio_unitario": 0.93,
        "descuento_pct": 10, "importe_neto": 11.12, "confianza": 99,
    }], base_imponible=11.12, total_iva=0, total=11.12, detalle_iva=[])
    report = validate_candidate(candidate, ocr_confidence=0.99)
    assert "line_amount_mismatch" not in {issue.code for issue in report.issues}


def test_review_items_no_choca_cuando_dos_reglas_apuntan_al_mismo_campo():
    """Regresión: un total que no aparece literal en el OCR (aviso informativo,
    header_value_not_observed) y que además no reconcilia con base+IVA (error
    bloqueante, document_total_mismatch) generaban DOS avisos para el mismo
    (entity_type, entity_key, field_name) — la tabla solo admite uno, así que el
    INSERT del lote completo fallaba con 23505 y tumbaba el job entero, aunque
    no hubiera habido ningún reintento ni condición de carrera."""
    candidate = _candidate(total=999.99)  # base 100 + iva 10 = 110, no 999.99
    model = SimpleNamespace(**{**candidate.__dict__})
    report = validate_candidate(model, ocr_confidence=0.99)
    ocr_text = "Tomate 10 10,00 100,00 Base 100,00 IVA 10,00"  # 999.99 no aparece literal
    provenance = _header_provenance_issues(
        {"header": candidate.__dict__, "lines": []}, ocr_text
    )
    assert {"document_total_mismatch", "header_value_not_observed"} <= {
        issue.code for issue in report.issues
    } | {issue.code for issue in provenance}
    report = ValidationReport(
        issues=report.issues + tuple(provenance), line_sum=report.line_sum,
        auto_confirmable=False,
    )

    items = _review_items(report, "artifact-id", None)

    keys = [(i["entity_type"], i["entity_key"], i["field_name"]) for i in items]
    assert len(keys) == len(set(keys)), f"claves duplicadas: {keys}"
    reasons = {i["reason_code"] for i in items}
    assert "document_total_mismatch" in reasons, "se perdió el aviso bloqueante"
    assert "header_value_not_observed" not in reasons  # el bloqueante gana
