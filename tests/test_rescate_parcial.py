"""Rescate parcial: aprovechar lo legible y preguntar solo lo que falta.

Antes, cualquier hueco mandaba a teclear el albarán entero. Estos tests fijan el
comportamiento nuevo: se conserva todo lo bien leído, no se inventa nada, y lo
dudoso se resuelve con una acción concreta.
"""
from __future__ import annotations

import copy

import pytest

from src.albaran_processor import AlbaranLLM
from src.accounting_validation import validate_candidate
from src.ingestion_service import (
    WARNING_REASONS, _descartar_fecha_no_observada, _derivar_totales_en_modelo,
    _fecha_observada,
)
from src.review_service import (
    CARGO_SIN_IDENTIFICAR, _acciones_para, _euros, _hueco_de_la_base,
    _resumen_importes, _set_correction,
)


def _candidato(lineas, **cabecera):
    base = {"header": {"base_imponible": None, "total": None, **cabecera}, "lines": lineas}
    return copy.deepcopy(base)


def _linea(nombre, importe):
    return {"descripcion_limpia": nombre, "cantidad": 1, "precio_unitario": importe,
            "importe_neto": importe}


# ── Filas de catálogo en formularios preimpresos ─────────────────────────────

def test_filas_vacias_de_catalogo_no_cuentan_como_productos():
    """Un albarán de matadero trae impresos sus 18 artículos y el repartidor solo
    rellena a mano los que sirve. El OCR devolvía la plantilla entera: 21 líneas
    para 4 productos reales. Cada fila en blanco generaba sus propios errores y
    además impedía sumar el total, así que el albarán se daba por ilegible."""
    model = AlbaranLLM.model_validate({"lineas": [
        {"nombre_producto": "Pollos 1ª", "cantidad": 15.9, "precio_unitario": 2.75},
        {"nombre_producto": "Pollos 2ª"},          # fila impresa, no servida
        {"nombre_producto": "Gallinas Pesadas"},   # idem
        {"nombre_producto": "Pechugas", "cantidad": 3.2, "precio_unitario": 5.7},
    ]})
    assert len(model.lineas) == 2
    assert model.lineas_descartadas == 2
    assert [l.nombre_producto for l in model.lineas] == ["Pollos 1ª", "Pechugas"]


def test_una_sola_cifra_basta_para_conservar_la_linea():
    """El criterio es conservador a propósito: con un único dato económico la
    línea se queda y se revisa. Descartarla escondería un producto real."""
    model = AlbaranLLM.model_validate({"lineas": [
        {"nombre_producto": "Con importe", "importe_neto": 10.0},
        {"nombre_producto": "Solo cantidad", "cantidad": 2},
        {"nombre_producto": "Solo tarifa", "precio_tarifa": 3.0},
    ]})
    assert len(model.lineas) == 3
    assert model.lineas_descartadas == 0


def test_un_albaran_ilegible_entero_no_se_vacia():
    """Si NINGUNA línea tiene datos no es una plantilla: es un documento que no se
    ha podido leer. Vaciarlo escondería el problema en vez de mostrarlo."""
    model = AlbaranLLM.model_validate({"lineas": [
        {"nombre_producto": "A"}, {"nombre_producto": "B"},
    ]})
    assert len(model.lineas) == 2
    assert model.lineas_descartadas == 0


# ── Fechas: preguntar antes que inventar ─────────────────────────────────────

@pytest.mark.parametrize("ocr", [
    "Fecha 06/05/26", "FECHA 06-05-2026", "Fecha de venta: 2026-05-06",
    "emitido el 6 de mayo de 2026", "6/5/2026",
])
def test_una_fecha_impresa_se_reconoce_en_sus_formatos_habituales(ocr):
    assert _fecha_observada("2026-05-06", ocr)


def test_una_fecha_que_no_esta_en_el_documento_se_descarta_y_se_pregunta():
    """En un albarán cuyo pie decía "de 6 de 202" (año cortado) el modelo devolvió
    06/01/2024: se inventó mes y año. Una fecha falsa no salta a la vista y
    descuadra los informes de gasto por meses."""
    model = AlbaranLLM.model_validate({"fecha": "06/01/2024", "lineas": [
        {"nombre_producto": "Pollos", "cantidad": 1, "precio_unitario": 10.0},
    ]})
    descartada = _descartar_fecha_no_observada(model, "ALBARAN N 0734079 de 6 de 202")

    assert descartada == "2024-01-06"
    assert model.fecha is None
    codigos = {i.code for i in validate_candidate(model, ocr_confidence=0.9).issues}
    assert "date_invalid" in codigos, "sin fecha hay que preguntarla, no seguir"


def test_una_fecha_corroborada_por_el_documento_se_respeta():
    model = AlbaranLLM.model_validate({"fecha": "01/06/2026", "lineas": [
        {"nombre_producto": "Tomate", "cantidad": 1, "precio_unitario": 10.0},
    ]})
    assert _descartar_fecha_no_observada(model, "FECHA 01-06-2026") is None
    assert model.fecha == "2026-06-01"


# ── IVA inventado sobre una base ya descartada ───────────────────────────────

def test_al_descartar_una_base_inventada_tambien_cae_su_iva():
    """En un albarán sin tabla de impuestos el modelo fabricó un tramo entero
    (21% sobre 1.138,46 = 216,31 €) sin que ninguna cifra estuviera en el papel.
    Quedarse con la base calculada y el IVA inventado daba un híbrido peor."""
    model = AlbaranLLM.model_validate({
        "base_imponible": 1138.46, "total_iva": 216.31, "total": 1354.77,
        "detalle_iva": [{"tipo": 21, "base": 1138.46, "cuota": 216.31}],
        "lineas": [{"nombre_producto": "Chorizo", "cantidad": 1,
                    "precio_unitario": 1000.14, "importe_neto": 1000.14}],
    })
    _derivar_totales_en_modelo(model, "tabla de productos sin totales impresos")

    assert model.base_imponible == 1000.14
    assert model.total == 1000.14
    assert model.total_iva is None and model.detalle_iva is None


# ── Añadir, borrar y cargos ──────────────────────────────────────────────────

def test_se_puede_anadir_una_linea_que_el_ocr_no_vio():
    candidato = _candidato([_linea("Tomate", 10.0)])
    _set_correction(candidato, ["añadir", "Huevos frescos, 2, 3,50"], 7)

    assert len(candidato["lines"]) == 2
    nueva = candidato["lines"][-1]
    assert nueva["descripcion_limpia"] == "Huevos frescos"
    assert nueva["importe_neto"] == 7.0
    assert nueva["decisiones"]["source"] == "human_correction"


def test_se_puede_borrar_una_linea_duplicada():
    candidato = _candidato([_linea("Tomate", 10.0), _linea("Tomate", 10.0)])
    _set_correction(candidato, ["borrar", "linea", "2"], 7)
    assert len(candidato["lines"]) == 1


def test_nunca_se_borra_la_ultima_linea():
    candidato = _candidato([_linea("Tomate", 10.0)])
    with pytest.raises(ValueError, match="sin ninguna línea"):
        _set_correction(candidato, ["borrar", "linea", "1"], 7)


def test_borrar_una_linea_inexistente_dice_cuantas_hay():
    candidato = _candidato([_linea("Tomate", 10.0), _linea("Ajo", 5.0)])
    with pytest.raises(ValueError, match="tiene 2 líneas"):
        _set_correction(candidato, ["borrar", "linea", "9"], 7)


def test_el_hueco_se_materializa_como_cargo_y_las_cuentas_cuadran():
    """Caso real: la base del albarán incluye 1,25 € de P.V. que no está en
    ninguna línea. Antes había que bloquearlo o falsear la base; ahora el hueco
    se convierte en una línea con nombre y la contabilidad vuelve a cuadrar."""
    candidato = _candidato([_linea("Cerveza", 314.97)], base_imponible=316.22)
    assert _hueco_de_la_base(candidato) == 1.25

    mensaje = _set_correction(candidato, ["cargo"], 7)

    assert "1,25€" in mensaje
    assert _hueco_de_la_base(candidato) == 0.0
    cargo = candidato["lines"][-1]
    assert cargo["descripcion_limpia"] == CARGO_SIN_IDENTIFICAR
    assert cargo["importe_neto"] == 1.25
    assert cargo["decisiones"]["rule"] == "cargo-sin-identificar"


def test_el_cargo_admite_un_importe_explicito():
    candidato = _candidato([_linea("Cerveza", 100.0)], base_imponible=100.0)
    _set_correction(candidato, ["cargo", "4,50"], 7)
    assert candidato["lines"][-1]["importe_neto"] == 4.5


# ── Acciones de un toque ─────────────────────────────────────────────────────

def test_se_ofrece_un_boton_para_cada_hueco_resoluble():
    candidato = _candidato([_linea("Cerveza", 314.97)], base_imponible=316.22)
    acciones = _acciones_para(
        [{"reason_code": "date_invalid"}, {"reason_code": "base_lines_mismatch"}], candidato
    )
    assert acciones == [("📅 Es de hoy", "hoy"), ("➕ Cargo de 1,25€", "cargo")]


def test_sin_nada_que_arreglar_no_se_ofrecen_botones():
    candidato = _candidato([_linea("Cerveza", 100.0)], base_imponible=100.0)
    assert _acciones_para([], candidato) == []


def test_no_se_ofrece_cargo_cuando_sobran_lineas():
    """Si las líneas suman MÁS que la base, el arreglo no es añadir dinero sino
    quitar una línea repetida; ofrecer un cargo empeoraría el descuadre."""
    candidato = _candidato([_linea("Cerveza", 110.0)], base_imponible=100.0)
    assert _acciones_para([{"reason_code": "base_lines_mismatch"}], candidato) == []


# ── Presentación ─────────────────────────────────────────────────────────────

def test_un_albaran_sin_iva_no_muestra_un_iva_vacio():
    assert _resumen_importes({"base_imponible": 133.67, "total_iva": None, "total": 133.67}) == (
        "Base 133,67€ = TOTAL 133,67€ (sin IVA)"
    )


def test_los_importes_de_dinero_llevan_siempre_dos_decimales():
    assert _euros(6) == "6,00€"
    assert _euros(1.25) == "1,25€"


# ── Hallazgos de las pruebas de recorrido completo ───────────────────────────

def test_corregir_la_fecha_la_deja_en_formato_valido():
    """La validación exige ISO (date.fromisoformat) pero la corrección guardaba
    el texto tal cual: se aceptaba "02/09/2026" y el albarán seguía marcado como
    fecha inválida. Corregir la fecha no servía absolutamente de nada."""
    from src.accounting_validation import validate_candidate

    for escrita in ("02/09/2026", "2026-09-02", "2-9-26"):
        candidato = _candidato([_linea("Tomate", 10.0)],
                               proveedor_nombre="P", base_imponible=10.0, total=10.0)
        _set_correction(candidato, ["fecha", escrita], 7)
        assert candidato["header"]["fecha"] == "2026-09-02"
        combinado = {**candidato["header"], "lineas": candidato["lines"]}
        codigos = {i.code for i in validate_candidate(
            combinado, extraction_complete=True, ocr_confidence=0.9).issues}
        assert "date_invalid" not in codigos


def test_una_fecha_que_no_se_entiende_se_rechaza_con_un_ejemplo():
    candidato = _candidato([_linea("Tomate", 10.0)])
    with pytest.raises(ValueError, match="02/09/2026"):
        _set_correction(candidato, ["fecha", "ayer por la tarde"], 7)


def test_no_se_ofrece_cargo_si_el_pie_del_albaran_no_cuadra_consigo_mismo():
    """Un albarán manuscrito declaraba "TOTAL BRUTO 427,0" con dos líneas que
    sumaban 421,00: parecía faltar un cargo de 6 €. Pero su propio pie tampoco
    cuadraba (427 + 47,10 ≠ 463,10): era un 421 mal leído. Ofrecer el cargo
    habría convertido un error de lectura en un apunte contable permanente."""
    candidato = _candidato([_linea("Pescado", 421.0)], base_imponible=427.0)
    reviews = [{"reason_code": "base_lines_mismatch"}, {"reason_code": "vat_quota_mismatch"}]
    assert _acciones_para(reviews, candidato) == []


def test_el_cargo_sigue_ofreciendose_cuando_el_pie_es_coherente():
    candidato = _candidato([_linea("Cerveza", 314.97)], base_imponible=316.22)
    assert _acciones_para([{"reason_code": "base_lines_mismatch"}], candidato) == [
        ("➕ Cargo de 1,25€", "cargo")
    ]


def test_pulsar_el_cargo_cuando_ya_cuadra_lo_explica_en_vez_de_fallar():
    candidato = _candidato([_linea("Cerveza", 100.0)], base_imponible=100.0)
    with pytest.raises(ValueError, match="ya cuadran"):
        _set_correction(candidato, ["cargo"], 7)
