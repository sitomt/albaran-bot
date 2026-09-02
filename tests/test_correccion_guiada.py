"""Corregir un dato suelto sin escribir un comando.

El caso real que motiva esto: en un albarán de matadero el OCR leyó «15,9 kg»
de pollos donde el papel pone 15,4. Todo lo demás estaba perfecto. Arreglarlo
exigía escribir «/corregir 26e63c27 linea 1 cantidad 15,4»: la referencia del
documento, la palabra "linea", el número y el nombre interno del campo.

Estos tests fijan las dos mitades de la solución: que se pueda llegar al dato a
botones, y que corregirlo deje el albarán coherente en vez de romperlo por otro
lado.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import bot, review_service
from src.albaran_processor import AlbaranLLM
from src.config import settings
from src.ingestion_service import _candidate_payload, _derivar_totales_ausentes
from src.review_service import (
    ReviewView, _set_correction, campos_de_cabecera, campos_de_linea,
    lineas_corregibles, pregunta_de_correccion, valor_actual,
)


def _albaran_intesa() -> dict:
    """El albarán real: base impresa en el papel, cuatro productos."""
    return copy.deepcopy({
        "header": {
            "proveedor_nombre": "Matadero INTESA", "fecha": "2026-09-02",
            "numero_albaran": "0734079", "base_imponible": 133.67,
            "total_iva": None, "total": 133.67, "detalle_iva": [],
        },
        "lines": [
            {"descripcion_limpia": "Pollos 1ª", "cantidad": 15.9, "unidad": "kg",
             "precio_unitario": 2.75, "importe_neto": 43.73},
            {"descripcion_limpia": "Pechugas", "cantidad": 3.2, "unidad": "kg",
             "precio_unitario": 5.7, "importe_neto": 18.24},
            {"descripcion_limpia": "Conejos", "cantidad": 9.1, "unidad": "kg",
             "precio_unitario": 7.0, "importe_neto": 63.7},
            {"descripcion_limpia": "Sangre cocida", "cantidad": 4.0, "unidad": "ud",
             "precio_unitario": 2.0, "importe_neto": 8.0},
        ],
    })


def _vista(candidate: dict) -> ReviewView:
    return ReviewView("2" * 36, "Revisión", True, False, candidate=candidate)


# ── Coherencia aritmética tras corregir ──────────────────────────────────────

def test_corregir_la_cantidad_recalcula_el_importe_de_la_linea():
    """Cambiar 15,9 por 15,4 y dejar el importe en 43,73 dejaría la línea
    contradiciéndose consigo misma y bloquearía el albarán por un descuadre que
    provoca la propia corrección."""
    candidato = _albaran_intesa()
    resumen = _set_correction(candidato, ["linea", "1", "cantidad", "15,4"], 7)

    assert candidato["lines"][0]["cantidad"] == 15.4
    assert candidato["lines"][0]["importe_neto"] == 42.35   # 15,4 × 2,75
    assert "42,35" in resumen and "43,73" in resumen


def test_corregir_el_precio_tambien_recalcula_el_importe():
    candidato = _albaran_intesa()
    _set_correction(candidato, ["linea", "2", "precio", "5,80"], 7)

    assert candidato["lines"][1]["importe_neto"] == 18.56   # 3,2 × 5,80


def test_corregir_el_importe_recalcula_el_precio_unitario():
    """De las tres cifras de una línea, la cantidad y el importe son las que
    vienen impresas; el precio por unidad es el que se deduce. Así que al
    corregir el importe se recalcula el precio, no la cantidad.

    Y se dice en voz alta: un precio raro (2,6635 €/kg de pollo) es la pista de
    que lo que estaba mal era la cantidad, no el importe."""
    candidato = _albaran_intesa()
    resumen = _set_correction(candidato, ["linea", "1", "importe", "42,35"], 7)

    assert candidato["lines"][0]["importe_neto"] == 42.35
    assert candidato["lines"][0]["cantidad"] == 15.9          # lo escrito no se toca
    assert candidato["lines"][0]["precio_unitario"] == pytest.approx(42.35 / 15.9, abs=0.0001)
    assert "el precio pasa a" in resumen


def test_un_total_impreso_en_el_papel_no_se_recalcula():
    """La base 133,67 está impresa. Si tras corregir deja de cuadrar con las
    líneas, eso es justo lo que una persona tiene que ver: recalcularla en
    silencio taparía que alguna de las dos lecturas sigue mal."""
    candidato = _albaran_intesa()
    _set_correction(candidato, ["linea", "1", "cantidad", "15,4"], 7)

    assert candidato["header"]["base_imponible"] == 133.67
    assert candidato["header"]["total"] == 133.67


def test_un_total_que_calculamos_nosotros_sigue_a_las_lineas():
    """Cuando el albarán no imprime totales los sumamos nosotros. Ese número es
    nuestro, no un hecho del papel, así que tras corregir una línea tiene que
    volver a cuadrar solo en vez de pedir una segunda corrección a mano."""
    candidato = _albaran_intesa()
    candidato["header"]["decisiones"] = {"totales": {
        "rule": "sumado-de-lineas", "motivo": "el albarán no imprime base ni total",
    }}
    resumen = _set_correction(candidato, ["linea", "1", "cantidad", "15,4"], 7)

    assert candidato["header"]["base_imponible"] == 132.29
    assert candidato["header"]["total"] == 132.29
    assert "132,29" in resumen


def test_el_resumen_del_cambio_se_lee_sin_saber_nombres_de_campo():
    candidato = _albaran_intesa()
    resumen = _set_correction(candidato, ["linea", "1", "cantidad", "15,4"], 7)

    assert "Pollos 1ª" in resumen and "cantidad" in resumen
    assert "15,9" in resumen and "15,4" in resumen
    assert "precio_unitario" not in resumen and "importe_neto" not in resumen


def test_corregir_la_fecha_sigue_normalizando_a_iso():
    candidato = _albaran_intesa()
    _set_correction(candidato, ["fecha", "03/09/2026"], 7)

    assert candidato["header"]["fecha"] == "2026-09-03"


# ── Material para preguntarlo a botones ──────────────────────────────────────

def test_los_botones_ofrecen_las_lineas_por_su_nombre():
    opciones = lineas_corregibles(_vista(_albaran_intesa()))

    assert opciones[0] == (1, "1. Pollos 1ª")
    assert [numero for numero, _ in opciones] == [1, 2, 3, 4]


def test_los_nombres_largos_se_recortan_para_que_quepan_en_el_boton():
    candidato = _albaran_intesa()
    candidato["lines"][0]["descripcion_limpia"] = "Pollo entero de corral extra grande"
    _, etiqueta = lineas_corregibles(_vista(candidato))[0]

    assert len(etiqueta) <= 26 and etiqueta.endswith("…")


def test_cada_boton_de_campo_muestra_lo_que_vale_ahora():
    """Sin el valor actual habría que volver al mensaje anterior para saber si el
    dato que se va a tocar es el que está mal."""
    opciones = campos_de_linea(_vista(_albaran_intesa()), 1)
    etiquetas = [etiqueta for _, etiqueta in opciones]

    assert "Cantidad: 15,9 kg" in etiquetas
    assert "Precio: 2,75€" in etiquetas
    assert "Importe: 43,73€" in etiquetas


def test_la_cabecera_ofrece_fecha_numero_proveedor_y_totales():
    etiquetas = [etiqueta for _, etiqueta in campos_de_cabecera(_vista(_albaran_intesa()))]

    assert etiquetas[0] == "Fecha: 2026-09-02"
    assert "Nº albarán: 0734079" in etiquetas
    assert "Total: 133,67€" in etiquetas


def test_el_destino_de_cada_boton_es_lo_que_entiende_la_correccion():
    """Los botones y el comando escrito comparten formato: lo que se pulsa es
    exactamente lo que se teclearía, así que no hay dos caminos que mantener."""
    candidato = _albaran_intesa()
    destino, _ = campos_de_linea(_vista(candidato), 1)[0]

    assert destino == ["linea", "1", "cantidad"]
    _set_correction(candidato, destino + ["15,4"], 7)
    assert candidato["lines"][0]["cantidad"] == 15.4


def test_la_pregunta_dice_que_hay_ahora_y_pide_solo_el_dato():
    pregunta = pregunta_de_correccion(_vista(_albaran_intesa()), ["linea", "1", "cantidad"])

    assert "Pollos 1ª" in pregunta
    assert "15,9 kg" in pregunta          # se compara con el papel sin volver atrás
    assert "solo con el dato" in pregunta
    assert "15,4" in pregunta             # el ejemplo aclara coma decimal


def test_valor_actual_no_revienta_con_una_linea_inexistente():
    assert valor_actual(_albaran_intesa(), ["linea", "9", "cantidad"]) == "—"


# ── Los valores observados no se desalinean al descartar filas ───────────────

def test_las_filas_de_catalogo_descartadas_no_desplazan_lo_observado():
    """Bug real detectado en producción: tras quitar 17 filas impresas vacías,
    cada línea quedó emparejada con los `valores_observados` de otra. «Sangre
    cocida» arrastraba la tarifa de «Pechugas», y la revisión mostraba precios
    que no eran de ese producto."""
    crudo = {"lineas": [
        {"nombre_producto": "Pollos 1ª", "cantidad": 15.9, "precio_unitario": 2.75},
        {"nombre_producto": "Pollos 2ª"},
        {"nombre_producto": "Gallinas Pesadas"},
        {"nombre_producto": "Pechugas", "cantidad": 3.2, "precio_unitario": 5.7},
        {"nombre_producto": "Sangre cocida", "cantidad": 4, "precio_unitario": 2},
    ]}
    candidato = _candidate_payload(AlbaranLLM.model_validate(copy.deepcopy(crudo)), crudo)

    for linea in candidato["lines"]:
        observado = linea["valores_observados"]
        assert observado.get("nombre_producto") == linea["descripcion_limpia"]


# ── El recorrido completo por Telegram ───────────────────────────────────────

def _update(chat_id: int = 991, texto: str = "", data: str | None = None):
    message = SimpleNamespace(reply_text=AsyncMock(), text=texto)
    query = SimpleNamespace(
        data=data, answer=AsyncMock(), edit_message_text=AsyncMock()
    ) if data else None
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=settings.allowed_users[0], username="owner"),
        effective_chat=SimpleNamespace(id=chat_id),
        message=message, callback_query=query,
    )


def _context():
    return SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock()))


@pytest.fixture
def revision(monkeypatch):
    ingestion_id = "2" * 36
    view = _vista(_albaran_intesa())
    monkeypatch.setattr(
        bot.db, "buscar_ingestion_por_referencia", AsyncMock(return_value={"id": ingestion_id})
    )
    monkeypatch.setattr(bot, "build_review_view", AsyncMock(return_value=view))
    monkeypatch.setattr(bot, "_send_review_callback", AsyncMock())
    bot._CORRECCIONES_EN_CURSO.clear()
    yield SimpleNamespace(id=ingestion_id, referencia=ingestion_id[:8], view=view)
    bot._CORRECCIONES_EN_CURSO.clear()


@pytest.mark.asyncio
async def test_tres_toques_llevan_de_la_revision_a_la_pregunta(revision):
    """El recorrido completo: qué producto, qué dato, y a esperar el número. En
    ningún paso hay que saber la referencia ni el nombre del campo."""
    ref = revision.referencia
    contexto = _context()

    menu = _update(data=f"ed:menu:{ref}")
    await bot._manejar_correccion_guiada(menu, contexto, menu.callback_query.data)
    botones = menu.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
    etiquetas = [b.text for fila in botones.inline_keyboard for b in fila]
    assert "1. Pollos 1ª" in etiquetas
    assert "📄 Fecha, nº, proveedor, totales" in etiquetas

    linea = _update(data=f"ed:l:{ref}:1")
    await bot._manejar_correccion_guiada(linea, contexto, linea.callback_query.data)
    botones = linea.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
    campos = {b.text: b.callback_data for fila in botones.inline_keyboard for b in fila}
    assert campos["Cantidad: 15,9 kg"] == f"ed:go:{ref}:linea|1|cantidad"

    campo = _update(data=campos["Cantidad: 15,9 kg"])
    await bot._manejar_correccion_guiada(campo, contexto, campo.callback_query.data)
    pregunta = campo.callback_query.edit_message_text.await_args.args[0]
    assert "Pollos 1ª" in pregunta and "15,9 kg" in pregunta
    assert bot._CORRECCIONES_EN_CURSO[991]["destino"] == ["linea", "1", "cantidad"]


@pytest.mark.asyncio
async def test_el_callback_cabe_en_los_64_bytes_de_telegram(revision):
    """Telegram rechaza callback_data de más de 64 bytes, y lo hace en tiempo de
    ejecución: un producto con nombre largo tumbaría el teclado en producción."""
    contexto = _context()
    for accion in ("menu", "h"):
        update = _update(data=f"ed:{accion}:{revision.referencia}")
        await bot._manejar_correccion_guiada(update, contexto, update.callback_query.data)
        markup = update.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
        for fila in markup.inline_keyboard:
            for boton in fila:
                assert len(boton.callback_data.encode()) <= 64, boton.callback_data
    update = _update(data=f"ed:l:{revision.referencia}:1")
    await bot._manejar_correccion_guiada(update, contexto, update.callback_query.data)
    markup = update.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
    for fila in markup.inline_keyboard:
        for boton in fila:
            assert len(boton.callback_data.encode()) <= 64, boton.callback_data


@pytest.mark.asyncio
async def test_el_numero_suelto_se_aplica_y_no_va_al_motor_de_consultas(revision, monkeypatch):
    """Con una corrección esperando, «15,4» es un valor, no una pregunta sobre
    compras. Si acabara en el motor de consultas la corrección se perdería y el
    bot respondería algo sin sentido."""
    consultar = AsyncMock(return_value="respuesta")
    monkeypatch.setattr(bot, "consultar", consultar)
    corregir = AsyncMock(return_value=revision.view)
    monkeypatch.setattr(bot, "correct_candidate", corregir)
    bot._CORRECCIONES_EN_CURSO[991] = {
        "ingestion_id": revision.id, "referencia": revision.referencia,
        "destino": ["linea", "1", "cantidad"], "desde": datetime.now(timezone.utc),
    }

    update = _update(texto="15,4")
    await bot.handle_text(update, _context())

    consultar.assert_not_awaited()
    corregir.assert_awaited_once()
    assert corregir.await_args.args[2] == ["linea", "1", "cantidad", "15,4"]
    assert 991 not in bot._CORRECCIONES_EN_CURSO   # la siguiente frase vuelve a ser consulta


@pytest.mark.asyncio
async def test_un_valor_invalido_deja_la_pregunta_abierta(revision, monkeypatch):
    """Equivocarse tecleando no debe obligar a repetir los tres toques: se
    reintenta escribiendo otra vez."""
    monkeypatch.setattr(
        bot, "correct_candidate", AsyncMock(side_effect=ValueError("No entiendo esa fecha"))
    )
    bot._CORRECCIONES_EN_CURSO[991] = {
        "ingestion_id": revision.id, "referencia": revision.referencia, "destino": ["fecha"],
        "desde": datetime.now(timezone.utc),
    }

    update = _update(texto="el martes")
    await bot.handle_text(update, _context())

    assert bot._CORRECCIONES_EN_CURSO.get(991) is not None
    respuesta = update.message.reply_text.await_args.args[0]
    assert "No entiendo esa fecha" in respuesta and "cancelar" in respuesta


@pytest.mark.asyncio
async def test_cancelar_devuelve_la_revision_sin_tocar_nada(revision, monkeypatch):
    corregir = AsyncMock()
    monkeypatch.setattr(bot, "correct_candidate", corregir)
    bot._CORRECCIONES_EN_CURSO[991] = {
        "ingestion_id": revision.id, "referencia": revision.referencia,
        "destino": ["linea", "1", "cantidad"], "desde": datetime.now(timezone.utc),
    }

    await bot.handle_text(_update(texto="cancelar"), _context())

    corregir.assert_not_awaited()
    assert 991 not in bot._CORRECCIONES_EN_CURSO


@pytest.mark.asyncio
async def test_abrir_otro_menu_olvida_la_correccion_a_medias(revision):
    """Si se deja una pregunta a medias y se vuelve al menú, el siguiente texto
    no debe aplicarse a un campo que ya nadie está mirando."""
    bot._CORRECCIONES_EN_CURSO[991] = {
        "ingestion_id": revision.id, "referencia": revision.referencia, "destino": ["total"],
        "desde": datetime.now(timezone.utc),
    }
    update = _update(data=f"ed:menu:{revision.referencia}")
    await bot._manejar_correccion_guiada(update, _context(), update.callback_query.data)

    assert 991 not in bot._CORRECCIONES_EN_CURSO


@pytest.mark.asyncio
async def test_una_pregunta_olvidada_caduca_y_no_se_traga_una_consulta(revision, monkeypatch):
    """Si se abre una corrección y media hora después se pregunta «cuánto gasté
    en junio», esa frase es una consulta. Tratarla como el valor de un campo
    perdería la pregunta e intentaría meterla dentro del albarán."""
    consultar = AsyncMock(return_value="respuesta")
    monkeypatch.setattr(bot, "consultar", consultar)
    monkeypatch.setattr(bot, "obtener_historial", lambda chat_id: [])
    monkeypatch.setattr(bot, "agregar_turno", lambda *a: None)
    corregir = AsyncMock()
    monkeypatch.setattr(bot, "correct_candidate", corregir)
    bot._CORRECCIONES_EN_CURSO[991] = {
        "ingestion_id": revision.id, "referencia": revision.referencia,
        "destino": ["linea", "1", "cantidad"],
        "desde": datetime.now(timezone.utc) - timedelta(minutes=30),
    }

    await bot.handle_text(_update(texto="cuántos kilos de pollo compré en junio"), _context())

    corregir.assert_not_awaited()
    consultar.assert_awaited_once()
    assert 991 not in bot._CORRECCIONES_EN_CURSO


@pytest.mark.asyncio
async def test_un_albaran_ya_guardado_no_se_puede_corregir_desde_la_revision(monkeypatch):
    """Editar el candidato de un albarán ya confirmado dejaba la ingesta abierta
    con una versión que nunca podría confirmarse (la clave de idempotencia ya
    está usada), mientras la contabilidad seguía con los datos viejos."""
    monkeypatch.setattr(
        bot.db, "buscar_ingestion_por_referencia",
        AsyncMock(return_value={"id": "3" * 36, "status": "confirmed"}),
    )
    update = _update(data="ed:menu:33333333")
    await bot._manejar_correccion_guiada(update, _context(), update.callback_query.data)

    aviso = update.callback_query.edit_message_text.await_args.args[0]
    assert "ya está guardado" in aviso and "/anular" in aviso


@pytest.mark.asyncio
async def test_correct_candidate_tambien_lo_rechaza_por_su_cuenta(monkeypatch):
    """La guardia no puede vivir solo en el botón: /corregir escrito a mano
    llega por otro camino al mismo sitio."""
    monkeypatch.setattr(
        review_service.db, "buscar_ingestion_por_referencia",
        AsyncMock(return_value={"id": "3" * 36, "status": "confirmed"}),
    )
    with pytest.raises(ValueError, match="cerrado"):
        await review_service.correct_candidate("33333333", settings.allowed_users[0], ["total", "10"])


@pytest.mark.asyncio
async def test_mandar_otra_foto_abandona_la_correccion_a_medias(monkeypatch):
    monkeypatch.setattr(bot, "_handle_image_file", AsyncMock())
    bot._CORRECCIONES_EN_CURSO[991] = {
        "ingestion_id": "2" * 36, "referencia": "22222222", "destino": ["total"],
        "desde": datetime.now(timezone.utc),
    }
    update = _update()
    update.message.photo = [SimpleNamespace(file_id="f", file_unique_id="u")]
    await bot.handle_photo(update, _context())

    assert 991 not in bot._CORRECCIONES_EN_CURSO


def test_sin_iva_corregir_el_total_arrastra_la_base():
    """En este albarán base y total son el mismo número. Corregir uno y dejar el
    otro convertiría un descuadre en dos y obligaría a teclearlo dos veces."""
    candidato = _albaran_intesa()
    resumen = _set_correction(candidato, ["total", "132,29"], 7)

    assert candidato["header"]["total"] == 132.29
    assert candidato["header"]["base_imponible"] == 132.29
    assert "igualados" in resumen


def test_con_iva_corregir_el_total_no_toca_la_base():
    """Con IVA de por medio base y total son cifras distintas del papel, y
    moverlas juntas borraría un dato real."""
    candidato = _albaran_intesa()
    candidato["header"].update({"base_imponible": 100.0, "total_iva": 10.0, "total": 110.0})
    _set_correction(candidato, ["total", "111,00"], 7)

    assert candidato["header"]["base_imponible"] == 100.0


def test_tras_corregir_a_mano_no_se_ofrece_inventar_un_cargo():
    """El hueco lo abrió la corrección, no un porte que falte. Convertirlo en
    «Cargo sin identificar» fosilizaría el error en la contabilidad."""
    candidato = _albaran_intesa()
    _set_correction(candidato, ["linea", "1", "cantidad", "15,4"], 7)
    reviews = [{"reason_code": "base_lines_mismatch"}]

    assert review_service._acciones_para(reviews, candidato) == []


def test_tras_corregir_se_ofrece_atajo_a_la_otra_cifra_de_esa_linea():
    """Si la cantidad nueva es buena, solo pueden fallar el precio de esa línea
    o el total: se ofrecen los dos sin volver a recorrer el menú."""
    candidato = _albaran_intesa()
    _set_correction(candidato, ["linea", "1", "cantidad", "15,4"], 7)
    vista = ReviewView("2" * 36, "", False, False, candidate=candidato,
                       open_reviews=[{"reason_code": "base_lines_mismatch"}])

    atajos = review_service.atajos_de_correccion(vista)
    assert ("✏️ Precio de Pollos 1ª", ["linea", "1", "precio"]) in atajos
    assert ("✏️ Total del albarán", ["total"]) in atajos


def test_sin_correcciones_a_mano_no_hay_atajos():
    vista = ReviewView("2" * 36, "", False, False, candidate=_albaran_intesa(),
                       open_reviews=[{"reason_code": "base_lines_mismatch"}])
    assert review_service.atajos_de_correccion(vista) == []


# ── Procedencia de los totales: calculado por nosotros ≠ impreso en el papel ──

def test_un_total_que_no_esta_en_el_papel_se_marca_como_calculado():
    """Caso real de INTESA. Su casilla de totales viene EN BLANCO y la columna
    IMPORTE también: los 133,67€ los calculó el modelo sumando kilos × precio.

    El sistema lo daba por impreso, porque solo dejaba constancia de la
    procedencia cuando había que CAMBIAR el valor; si el número calculado
    coincidía con la suma de las líneas, se colaba como si fuera un hecho del
    documento. `_candidate_payload` construye una cabecera nueva, así que la
    constancia que sí dejaba el paso anterior tampoco sobrevivía hasta aquí.
    """
    ocr = (
        "| LOTE | ARTICULO | KILOS | PRECIO € | IMPORTE € |\\n"
        "| 26150 | Pollos 1ª | 15,9 | 2,75 | |\\n"
        "| 26152 | Pechugas | 3,2 | 5,70 | |\\n"
        "| SUMA | I.V.A. ___ % | R.E. ___ % | TOTAL € |\\n| | | | |"
    )
    header = {"base_imponible": 61.97, "total": 61.97}     # 43,73 + 18,24
    lineas = [{"importe_neto": 43.73}, {"importe_neto": 18.24}]

    _derivar_totales_ausentes(header, lineas, ocr)

    totales = header["decisiones"]["totales"]
    assert totales["rule"] == "sumado-de-lineas"
    assert header["base_imponible"] == 61.97      # el número era correcto: no se toca
    # No se ha descartado nada, así que tampoco puede arrastrar el IVA consigo.
    assert "base_descartada" not in totales


def test_un_total_que_si_esta_impreso_sigue_siendo_un_hecho_del_papel():
    """La contrapartida: si la cifra aparece en el documento no es un cálculo
    nuestro, y corregir una línea después no debe moverla."""
    ocr = "| Pollos | 15,9 | 2,75 | 43,73 |\\nSUMA 61,97 TOTAL 61,97"
    header = {"base_imponible": 61.97, "total": 61.97}

    _derivar_totales_ausentes(header, [{"importe_neto": 43.73}, {"importe_neto": 18.24}], ocr)

    assert "totales" not in header.get("decisiones", {})


def test_con_la_procedencia_bien_puesta_corregir_un_kilo_no_bloquea_el_albaran():
    """El recorrido completo del caso real: como los totales los sumamos
    nosotros, bajar los pollos a 15,4 arrastra la base y el total, y el albarán
    queda cuadrado en un solo paso en vez de acusar al documento de declarar una
    cifra que nunca declaró."""
    candidato = _albaran_intesa()
    candidato["header"]["decisiones"] = {"totales": {
        "rule": "sumado-de-lineas",
        "motivo": "el albarán no imprime los totales; la cifra coincide con la suma",
    }}

    _set_correction(candidato, ["linea", "1", "cantidad", "15,4"], 7)

    suma = round(sum(linea["importe_neto"] for linea in candidato["lines"]), 2)
    assert candidato["header"]["base_imponible"] == suma == 132.29
    assert candidato["header"]["total"] == 132.29
