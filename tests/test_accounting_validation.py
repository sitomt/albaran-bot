from types import SimpleNamespace

from src.accounting_validation import validate_candidate
from src.albaran_processor import AlbaranLLM
from src.ingestion_service import (
    ValidationReport, _amount_is_visible, _derivar_totales_ausentes,
    _header_provenance_issues, _page_text, _provenance_issues, _review_items,
)
from src.review_service import _explicacion_descuadre


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


def test_dos_tramos_de_iva_no_se_leen_como_uno_solo_caja_gomez():
    """Regresión: un albarán con la tabla de totales a dos filas, donde la segunda
    trae un rótulo pegado ("P.V.: 1,25") en otra columna, perdía el tramo del 10%.
    Con un solo tramo, base+IVA daba 345,72 y el bot pedía "corregir" el total
    correcto (379,27) a un valor falso. Con los dos tramos las cuentas cierran y
    solo queda el aviso legítimo del cargo de 1,25 que una persona debe asignar."""
    candidate = _candidate(
        base_imponible=316.22, total_iva=63.05, total=379.27,
        detalle_iva=[
            {"tipo": 21, "base": 285.72, "cuota": 60.0},
            {"tipo": 10, "base": 30.50, "cuota": 3.05},
        ],
        lineas=[
            {"nombre_producto": n, "cantidad": 1, "precio_unitario": importe,
             "importe_neto": importe, "confianza": 100}
            for n, importe in [
                ("Barril Pilsen", 120.60), ("Cajones Verna Limón", 24.24),
                ("Agua Fuente Liviana", 30.50), ("Pack Pilsen", 45.96),
                ("Latas Sin", 11.44), ("Cajones Estrella Sin", 40.40),
                ("Cajones 1/5 Pilsen", 15.63), ("Latas Estrella", 11.80),
                ("Nestea Limón", 14.40),
            ]
        ],
    )
    report = validate_candidate(candidate, ocr_confidence=0.95)
    codes = {issue.code for issue in report.issues}

    assert "document_total_mismatch" not in codes, (
        "con los dos tramos, base+IVA ya cuadra con el total impreso"
    )
    assert "vat_breakdown_mismatch" not in codes
    # Queda el hueco de 1,25 (el P.V.), que sí debe revisar una persona.
    assert "base_lines_mismatch" in codes
    hueco = next(i for i in report.issues if i.code == "base_lines_mismatch")
    assert round(float(hueco.observed) - float(hueco.expected), 2) == 1.25


# ── Totales ausentes o inventados ────────────────────────────────────────────

def _lineas_importes(*importes):
    return [{"importe_neto": importe} for importe in importes]


def test_totales_ausentes_se_suman_en_python_no_los_adivina_el_modelo():
    """Un albarán sin tabla de totales hacía que el LLM sumase de cabeza sus 18
    líneas y devolviera un número distinto en cada pasada (1183, 1194, 1197,
    1034 € cuando la suma real era 1000,14 €). Ahora el modelo deja null y la
    suma la hace Python, que siempre da lo mismo."""
    header = {"base_imponible": None, "total": None, "total_iva": None}
    _derivar_totales_ausentes(header, _lineas_importes(37.76, 36.78, 30.80), "")
    assert header["base_imponible"] == 105.34
    assert header["total"] == 105.34
    assert header["decisiones"]["totales"]["rule"] == "sumado-de-lineas"


def test_total_inventado_por_el_modelo_se_sustituye_por_la_suma_real():
    """El prompt prohíbe calcular totales, pero el modelo reincide. Si el número
    no aparece en el documento y además no cuadra con las líneas, es inventado."""
    header = {"base_imponible": 1138.46, "total": 1138.46, "total_iva": None}
    _derivar_totales_ausentes(header, _lineas_importes(37.76, 36.78, 30.80), "sin totales impresos")
    assert header["base_imponible"] == 105.34
    assert header["decisiones"]["totales"]["base_descartada"] == 1138.46


def test_un_total_impreso_en_el_albaran_nunca_se_reescribe():
    """Si el importe SÍ está en el documento es un hecho, aunque no cuadre con
    las líneas: puede haber portes o envases cobrados aparte. Ese desajuste lo
    revisa una persona; el sistema no lo tapa reescribiendo el dato bueno."""
    ocr = "BASE IMPONIBLE 316,22 TOTAL 379,27"
    header = {"base_imponible": 316.22, "total": 379.27, "total_iva": 63.05}
    _derivar_totales_ausentes(header, _lineas_importes(314.97), ocr)
    assert header["base_imponible"] == 316.22, "no debe sustituirse por 314,97"
    assert "totales" not in header.get("decisiones", {})


def test_sin_todos_los_importes_de_linea_no_se_inventa_una_suma():
    header = {"base_imponible": None, "total": None, "total_iva": None}
    _derivar_totales_ausentes(header, _lineas_importes(10.0, None, 5.0), "")
    assert header["base_imponible"] is None


def test_el_descuadre_se_explica_por_la_diferencia_no_por_el_valor_a_copiar():
    """Decir "observado 316,22 → propuesto 314,97" empujaba a sustituir la base
    por la suma de líneas, borrando un cargo legítimo de 1,25 €."""
    frase = _explicacion_descuadre({
        "reason_code": "base_lines_mismatch",
        "observed_value": 316.22, "calculated_value": 314.97,
    })
    assert frase is not None
    assert "1,25" in frase
    assert "cargo aparte" in frase
    assert "→" not in frase and "propuesto" not in frase


# ── IVA indicado como referencia pero no repercutido ─────────────────────────

def _albaran_sin_iva_repercutido(**cambios):
    datos = {
        "proveedor_nombre": "Embutidos Mateo", "fecha": "13/05/2026",
        "base_imponible": 100.0, "total_iva": None, "total": 100.0,
        "detalle_iva": [{"tipo": 10, "base": 100.0, "cuota": None}],
        "lineas": [{"nombre_producto": "Chorizo", "cantidad": 1,
                    "precio_unitario": 100.0, "importe_neto": 100.0, "confianza": 100}],
    }
    datos.update(cambios)
    return AlbaranLLM.model_validate(datos)


def test_cuota_de_iva_en_blanco_se_lee_como_cero_no_como_tramo_invalido():
    """Muchos albaranes de ENTREGA muestran el tipo ("% IVA: 10") y dejan vacía
    la casilla del importe, con TOTAL = BASE: el IVA se repercutirá en factura.
    Eso disparaba `vat_detail_invalid`, que es bloqueante y no tiene campo que
    corregir, así que la única salida era reteclear el albarán entero a mano."""
    model = _albaran_sin_iva_repercutido()
    assert model.detalle_iva[0].cuota == 0.0
    assert model.total_iva == 0.0

    codes = {issue.code for issue in validate_candidate(model, ocr_confidence=0.95).issues}
    assert "vat_detail_invalid" not in codes
    assert "vat_total_mismatch" not in codes
    assert not codes, f"el albarán cuadra y debe poder confirmarse: {codes}"


def test_si_el_iva_si_se_cobra_y_falta_la_cuota_se_sigue_avisando():
    """La normalización no puede tapar un IVA realmente cobrado: si el total
    supera a la base, falta dinero y debe saltar un aviso concreto y accionable
    (el descuadre del total), no un genérico "tramo inválido"."""
    model = _albaran_sin_iva_repercutido(total=110.0)
    codes = {issue.code for issue in validate_candidate(model, ocr_confidence=0.95).issues}
    assert "document_total_mismatch" in codes
    assert "vat_detail_invalid" not in codes


def test_una_cuota_informada_nunca_se_reescribe():
    model = _albaran_sin_iva_repercutido(
        total_iva=10.0, total=110.0,
        detalle_iva=[{"tipo": 10, "base": 100.0, "cuota": 10.0}],
    )
    assert model.detalle_iva[0].cuota == 10.0
    assert model.total_iva == 10.0


def test_importe_con_separador_de_miles_cuenta_como_observado():
    """"1,000.14" impreso en el albarán se marcaba como "no aparece en el OCR"
    y ensuciaba la revisión con un aviso falso en todo albarán de ≥ 1.000 €."""
    assert _amount_is_visible(1000.14, "TOTAL 1,000.14 €")
    assert _amount_is_visible(1000.14, "TOTAL 1.000,14 €")
    assert _amount_is_visible(1000.14, "TOTAL 1000,14 €")
    assert not _amount_is_visible(1000.14, "TOTAL 999,99 €")
