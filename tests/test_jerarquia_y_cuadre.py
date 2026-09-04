"""Quién manda cuando dos cifras del albarán se contradicen.

Nace de un albarán de pescadería manuscrito. El OCR se equivocó dos veces con el
mismo trazo —un 1 leído como 7— y dejó el pie así:

    TOTAL BRUTO 427,0   (el papel pone 421,0)
    % I.V.A.     47,10  (el papel pone  42,10)
    TOTAL       463,10

Con eso el modelo montó un tramo de IVA del 47,1%, un tipo que no existe en
ningún país, y una cuota inventada de 36,10 €. El resultado era un callejón sin
salida: el tramo bloqueaba la confirmación, no se podía editar desde Telegram, y
tras corregir la base y el IVA a mano el bot llegaba a pedir «los tramos no suman
el IVA total: 42,1 → 36,1» — o sea, que deshicieras tu propia corrección.

La regla que lo resuelve es una jerarquía de confianza:
    1. corregido por una persona   ← manda siempre
    2. impreso y legible en la foto
    3. calculado por nosotros
"""
from __future__ import annotations

import copy

import pytest

from src.accounting_validation import validate_candidate
from src.ingestion_service import WARNING_REASONS
from src.review_service import (
    _campos_fijados, _explicacion_de_iva, _set_correction,
    _tramo_unico_desde_la_cabecera, _acciones_para, aplicar_cuadre,
    arbitrar_con_el_total_impreso, descartar_avisos_que_revierten,
)


def _juanin(**cabecera):
    """El albarán real, tal y como lo leyó el OCR."""
    return copy.deepcopy({
        "header": {
            "proveedor_nombre": "Mariscos Juanín", "fecha": "2026-06-01",
            "numero_albaran": "2251", "base_imponible": 427.0, "total_iva": 36.1,
            "total": 463.1, "detalle_iva": [{"tipo": 47.1, "base": 427.0, "cuota": 36.1}],
            "decisiones": {"impresos": {
                "base_imponible": True, "total_iva": True, "total": True,
            }},
            **cabecera,
        },
        "lines": [
            {"descripcion_limpia": "C N P", "cantidad": 25.0, "unidad": "kg",
             "precio_unitario": 14.95, "importe_neto": 373.75, "confianza": 95},
            {"descripcion_limpia": "5 Hectana CIP", "cantidad": 5.0, "unidad": "kg",
             "precio_unitario": 9.45, "importe_neto": 47.25, "confianza": 95},
        ],
    })


def _bloqueantes(candidate):
    informe = validate_candidate(
        {**candidate["header"], "lineas": candidate["lines"]},
        extraction_complete=True, document_is_handwritten=True, ocr_confidence=None,
    )
    return {issue.code for issue in informe.issues} - WARNING_REASONS


def _items(candidate):
    return [{"reason_code": code} for code in _bloqueantes(candidate)]


# ── El árbitro: las líneas y el total impreso se dan la razón ────────────────

def test_las_lineas_y_el_total_impreso_revelan_la_lectura_correcta():
    """373,75 + 47,25 = 421,00 y el TOTAL impreso pone 463,10. La diferencia,
    42,10, es exactamente el 10% de 421,00. Dos cifras independientes del papel
    apuntando al mismo resultado no es casualidad: es la lectura buena."""
    candidato = _juanin()

    propuesta = arbitrar_con_el_total_impreso(candidato, _items(candidato))

    assert propuesta["base"] == 421.0
    assert propuesta["iva"] == 42.1
    assert propuesta["etiqueta"] == "10%"
    assert propuesta["total"] == 463.1


def test_cuadrar_deja_el_albaran_confirmable_de_un_toque():
    candidato = _juanin()
    assert _bloqueantes(candidato) == {"base_lines_mismatch", "vat_quota_mismatch"}

    aplicar_cuadre(candidato, 7)

    assert _bloqueantes(candidato) == set()
    assert candidato["header"]["detalle_iva"] == [{"tipo": 10.0, "base": 421.0, "cuota": 42.1}]


def test_cuadrar_no_toca_ni_el_total_impreso_ni_las_lineas():
    """Lo único que se deduce es la base y el IVA. El total impreso es el hecho
    más fiable del papel y las líneas son lo que de verdad se ha entregado."""
    candidato = _juanin()
    lineas_antes = copy.deepcopy(candidato["lines"])

    aplicar_cuadre(candidato, 7)

    assert candidato["header"]["total"] == 463.1
    assert candidato["lines"] == lineas_antes


def test_cuadrar_deja_rastro_de_lo_que_descarta():
    candidato = _juanin()
    aplicar_cuadre(candidato, 7)

    decision = candidato["header"]["decisiones"]["totales"]
    assert decision["rule"] == "cuadrado-con-el-total-impreso"
    assert decision["base_descartada"] == 427.0
    assert decision["iva_descartado"] == 36.1
    assert decision["actor"] == "7"


# ── Cuándo NO debe salir: el dinero que no es IVA ────────────────────────────

def test_un_porte_de_verdad_no_se_disfraza_de_iva():
    """Base 427 con 6€ de portes e IVA del 10% sobre ella: la diferencia contra
    las líneas da 11,57%, que no es ningún tipo legal. Convertir ese dinero en
    IVA falsearía la contabilidad."""
    candidato = _juanin(total=469.70, total_iva=42.70)

    assert arbitrar_con_el_total_impreso(candidato, _items(candidato)) is None


def test_un_cargo_de_125_no_pasa_por_iva():
    """Caja Gómez: 1,25€ de P.V. El mejor encaje se queda a 1,84€, sesenta veces
    la tolerancia. Ese albarán sigue con su «Cargo sin identificar»."""
    candidato = _juanin(base_imponible=316.22, total=379.27, total_iva=63.05, detalle_iva=[])
    candidato["lines"] = [{"descripcion_limpia": "Varios", "cantidad": 1.0,
                           "precio_unitario": 314.97, "importe_neto": 314.97, "confianza": 95}]

    assert arbitrar_con_el_total_impreso(candidato, _items(candidato)) is None


def test_sin_total_impreso_no_hay_arbitro():
    """Si el total lo hemos calculado nosotros sumando líneas, no aporta nada:
    cuadraría consigo mismo por construcción."""
    candidato = _juanin()
    candidato["header"]["decisiones"]["impresos"]["total"] = False

    assert arbitrar_con_el_total_impreso(candidato, _items(candidato)) is None


def test_con_una_linea_sin_importe_la_suma_seria_mentira():
    candidato = _juanin()
    candidato["lines"][1]["importe_neto"] = None

    assert arbitrar_con_el_total_impreso(candidato, _items(candidato)) is None


def test_con_varios_tramos_de_iva_no_se_puede_repartir_la_base():
    """Protege los albaranes de dos y tres tramos, que ya funcionan bien."""
    candidato = _juanin(detalle_iva=[
        {"tipo": 10.0, "base": 200.0, "cuota": 20.0},
        {"tipo": 21.0, "base": 221.0, "cuota": 46.41},
    ])

    assert arbitrar_con_el_total_impreso(candidato, _items(candidato)) is None


def test_si_las_lineas_superan_el_total_algo_va_muy_mal():
    candidato = _juanin(total=300.0)

    assert arbitrar_con_el_total_impreso(candidato, _items(candidato)) is None


def test_sin_nada_bloqueado_no_se_ofrece_cuadrar():
    candidato = _juanin(base_imponible=421.0, total_iva=42.1,
                        detalle_iva=[{"tipo": 10.0, "base": 421.0, "cuota": 42.1}])

    assert arbitrar_con_el_total_impreso(candidato, []) is None


def test_el_boton_sale_junto_a_los_demas():
    candidato = _juanin()
    textos = [texto for texto, _ in _acciones_para(_items(candidato), candidato)]

    assert any(t.startswith("✅ Cuadrar") and "421" in t and "10%" in t for t in textos)


# ── La jerarquía: lo corregido a mano manda ──────────────────────────────────

def test_una_correccion_queda_marcada_como_tal():
    candidato = _juanin()
    _set_correction(candidato, ["base", "421"], 7)

    assert "base_imponible" in _campos_fijados(candidato)


def test_el_tramo_unico_se_rehace_desde_la_cabecera_corregida():
    """Con un solo tramo, el desglose no es un dato aparte: es la cabecera
    escrita dos veces. Dejar la copia vieja bloqueaba el albarán por un campo
    que además no se puede editar desde Telegram."""
    candidato = _juanin()
    _set_correction(candidato, ["base", "421"], 7)
    _set_correction(candidato, ["iva", "42,10"], 7)

    assert candidato["header"]["detalle_iva"] == [{"tipo": 10.0, "base": 421.0, "cuota": 42.1}]
    assert _bloqueantes(candidato) == set()


def test_con_varios_tramos_la_cabecera_corregida_no_los_reescribe():
    candidato = _juanin(detalle_iva=[
        {"tipo": 10.0, "base": 200.0, "cuota": 20.0},
        {"tipo": 21.0, "base": 227.0, "cuota": 47.67},
    ])
    _set_correction(candidato, ["base", "421"], 7)

    assert _tramo_unico_desde_la_cabecera(candidato) is None
    assert len(candidato["header"]["detalle_iva"]) == 2


def test_ningun_aviso_puede_pedirte_deshacer_tu_correccion():
    """El bot llegaba a decir «los tramos no suman el IVA total: 42,1 → 36,1»
    justo después de que corrigieras el IVA a 42,1. Eso no es un aviso, es un
    bucle."""
    candidato = _juanin()
    _set_correction(candidato, ["iva", "42,10"], 7)
    avisos = [
        {"reason_code": "vat_total_mismatch", "observed_value": 42.1, "calculated_value": 36.1},
        {"reason_code": "date_invalid"},
    ]

    quedan = descartar_avisos_que_revierten(avisos, candidato)

    assert [a["reason_code"] for a in quedan] == ["date_invalid"]


def test_sin_correcciones_no_se_descarta_ningun_aviso():
    """La regla solo protege lo que una persona ha fijado; no tapa nada más."""
    candidato = _juanin()
    avisos = [{"reason_code": "vat_total_mismatch"}, {"reason_code": "base_lines_mismatch"}]

    assert descartar_avisos_que_revierten(avisos, candidato) == avisos


# ── El mensaje: nada de números que no están en el papel ─────────────────────

def test_un_tipo_de_iva_imposible_se_explica_como_lo_que_es():
    """Antes: «la cuota no coincide con base × tipo: 36,1 → 201,12». Los 201,12
    salían de aplicar el 47,1% a una base mal leída — un número que no está en
    el albarán y al que nadie puede llegar mirando la foto."""
    candidato = _juanin()

    texto = _explicacion_de_iva({"reason_code": "vat_quota_mismatch"}, candidato)

    assert "47,1% , no existe".replace(" ,", ",") in texto or "47,1%" in texto
    assert "no existe" in texto
    assert "201" not in texto


def test_con_un_tipo_legal_el_aviso_habla_de_las_tres_cifras_del_pie():
    candidato = _juanin(detalle_iva=[{"tipo": 10.0, "base": 400.0, "cuota": 40.0}])

    texto = _explicacion_de_iva({"reason_code": "vat_bases_mismatch"}, candidato)

    assert "cuál de las tres cifras está mal" in texto
