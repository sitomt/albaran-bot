from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import bot, manual_albaran, review_service
from src.config import settings


def _update(user_id: int | None = None, chat_id: int = 991) -> SimpleNamespace:
    user_id = user_id or settings.allowed_users[0]
    message = SimpleNamespace(reply_text=AsyncMock(), text="")
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username="owner"),
        effective_chat=SimpleNamespace(id=chat_id),
        message=message,
        callback_query=None,
    )


@pytest.mark.asyncio
async def test_revision_explica_tarifa_descuento_neto_e_iva_en_espanol(monkeypatch):
    ingestion_id = "a" * 36
    candidate = {
        "header": {
            "proveedor_nombre": "Proveedor Uno", "fecha": "2026-08-01",
            "numero_albaran": "A-10", "base_imponible": 90, "total_iva": 9,
            "total": 99, "detalle_iva": [{"tipo": 10, "base": 90, "cuota": 9}],
        },
        "lines": [{
            "descripcion_limpia": "Tomate", "cantidad": 10, "unidad": "kg",
            "precio_unitario": 9, "descuento_pct": 10, "importe_neto": 90,
            "valores_observados": {"precio_tarifa": 10},
        }],
    }
    reviews = [{
        "reason_code": "handwritten_document", "field_name": "documento",
        "entity_type": "document", "entity_key": "header",
        "observed_value": None, "calculated_value": None,
    }]
    monkeypatch.setattr(review_service.db, "obtener_ingestion", AsyncMock(return_value={"id": ingestion_id}))
    monkeypatch.setattr(review_service, "load_candidate", AsyncMock(return_value=(candidate, "artifact")))
    monkeypatch.setattr(review_service.db, "listar_revisiones_ingestion", AsyncMock(return_value=reviews))

    view = await review_service.build_review_view(ingestion_id, settings.allowed_users[0])

    assert view.can_approve is True
    assert "tarifa 10€ − 10% → neto 9€/u" in view.text
    assert "Base 90€ + IVA 9€ = TOTAL 99€" in view.text
    assert "texto escrito a mano" in view.text
    assert "handwritten_document" not in view.text


@pytest.mark.asyncio
async def test_revision_con_desajuste_bloquea_confirmacion_y_da_comando(monkeypatch):
    ingestion_id = "b" * 36
    candidate = {
        "header": {"proveedor_nombre": "P", "fecha": "2026-08-01", "total": 50},
        "lines": [{
            "descripcion_limpia": "Queso", "cantidad": 2, "unidad": "ud",
            "precio_unitario": 10, "importe_neto": 50, "valores_observados": {},
        }],
    }
    reviews = [{
        "reason_code": "line_amount_mismatch", "field_name": "importe_neto",
        "entity_type": "line", "entity_key": "1", "observed_value": 50,
        "calculated_value": 20,
    }]
    monkeypatch.setattr(review_service.db, "obtener_ingestion", AsyncMock(return_value={"id": ingestion_id}))
    monkeypatch.setattr(review_service, "load_candidate", AsyncMock(return_value=(candidate, "artifact")))
    monkeypatch.setattr(review_service.db, "listar_revisiones_ingestion", AsyncMock(return_value=reviews))

    view = await review_service.build_review_view(ingestion_id, settings.allowed_users[0])

    assert view.can_approve is False
    assert "Cantidad × precio neto no coincide con el importe [línea 1]" in view.text
    assert f"/corregir {ingestion_id[:8]} linea 1 importe VALOR_CORRECTO" in view.text


@pytest.mark.asyncio
async def test_revision_incompleta_ofrece_reintento_o_manual_con_misma_foto(monkeypatch):
    ingestion_id = "e" * 36
    candidate = {"header": {"proveedor_nombre": "P", "fecha": "2026-08-01"}, "lines": []}
    reviews = [{
        "reason_code": "lines_missing", "field_name": "lines", "entity_type": "document",
        "entity_key": "header", "observed_value": None, "calculated_value": None,
    }]
    monkeypatch.setattr(review_service.db, "obtener_ingestion", AsyncMock(return_value={"id": ingestion_id}))
    monkeypatch.setattr(review_service, "load_candidate", AsyncMock(return_value=(candidate, "artifact")))
    monkeypatch.setattr(review_service.db, "listar_revisiones_ingestion", AsyncMock(return_value=reviews))

    view = await review_service.build_review_view(ingestion_id, settings.allowed_users[0])

    assert view.can_approve is False
    assert "Introducir a mano" in view.text
    assert "misma foto" in view.text

    markup = bot._review_markup(view, ingestion_id)
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert "✍️ Introducir a mano" in labels
    assert "🚫 Rechazar" in labels
    assert "✅ Confirmar definitivamente" not in labels


@pytest.mark.asyncio
async def test_revisar_y_corregir_comparten_todas_las_acciones(monkeypatch):
    ingestion_id = "f" * 36
    view = review_service.ReviewView(
        ingestion_id=ingestion_id,
        text="Revisión",
        can_approve=True,
        probable_duplicate=True,
    )
    expected_labels = {
        "✅ Confirmar definitivamente", "Es duplicado", "No es duplicado",
        "✏️ Corregir un dato", "✍️ Introducir a mano", "🚫 Rechazar",
    }

    review_update = _update()
    monkeypatch.setattr(
        bot.db, "buscar_ingestion_por_referencia",
        AsyncMock(return_value={"id": ingestion_id}),
    )
    monkeypatch.setattr(bot, "build_review_view", AsyncMock(return_value=view))
    await bot.cmd_revisar(review_update, SimpleNamespace(args=[ingestion_id[:8]]))

    review_markup = review_update.message.reply_text.await_args.kwargs["reply_markup"]
    review_labels = {
        button.text for row in review_markup.inline_keyboard for button in row
    }
    assert review_labels == expected_labels

    correction_update = _update()
    monkeypatch.setattr(bot, "correct_candidate", AsyncMock(return_value=view))
    await bot.cmd_editar(
        correction_update,
        SimpleNamespace(args=[ingestion_id[:8], "total", "12,50"]),
    )

    correction_markup = correction_update.message.reply_text.await_args.kwargs["reply_markup"]
    correction_labels = {
        button.text for row in correction_markup.inline_keyboard for button in row
    }
    assert correction_labels == expected_labels


def test_revision_larga_se_pagina_sin_perder_contenido():
    text = "\n".join(f"Línea {index}: " + "x" * 180 for index in range(60))
    pages = bot._split_telegram_text(text, limit=500)
    assert len(pages) > 1
    assert all(len(page) <= 500 for page in pages)
    assert "\n".join(pages) == text


@pytest.mark.asyncio
async def test_abrir_revision_reenvia_el_original_privado(monkeypatch):
    ingestion_id = "9" * 36
    monkeypatch.setattr(bot.db, "obtener_ingestion", AsyncMock(return_value={
        "storage_bucket": "albaranes", "storage_path": "intake/original.jpg",
    }))
    monkeypatch.setattr(bot.db, "descargar_original_privado", AsyncMock(return_value=b"image"))
    telegram = SimpleNamespace(send_document=AsyncMock())

    await bot._send_review_original(telegram, 123, ingestion_id)

    telegram.send_document.assert_awaited_once()
    assert ingestion_id[:8] in telegram.send_document.await_args.kwargs["caption"]


def test_botones_de_revision_llevan_version_cas_y_caben_en_telegram():
    ingestion_id = "1" * 36
    artifact_id = "2" * 36
    view = review_service.ReviewView(
        ingestion_id=ingestion_id, text="Revisión", can_approve=True,
        probable_duplicate=True, candidate_artifact_id=artifact_id,
    )

    markup = bot._review_markup(view, ingestion_id)
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]

    versioned = [value for value in callbacks if value and value.startswith("rv:")]
    assert versioned
    assert all(value.endswith(artifact_id[:12]) for value in versioned)
    assert all(len(value.encode("utf-8")) <= 64 for value in callbacks if value)


@pytest.mark.asyncio
async def test_modo_manual_reutiliza_original_de_ocr_fallido(monkeypatch):
    manual_albaran._manual_flows.clear()
    monkeypatch.setattr(
        manual_albaran.db, "obtener_ingestion",
        AsyncMock(return_value={
            "id": "c" * 36, "status": "failed", "storage_bucket": "albaranes",
            "storage_path": "intake/original.jpg",
        }),
    )
    monkeypatch.setattr(manual_albaran.db, "listar_todos_proveedores", AsyncMock(return_value=[]))

    text = await manual_albaran.iniciar_desde_ingestion(77, settings.allowed_users[0], "c" * 36)

    flow = manual_albaran._manual_flows[77]
    assert flow["_durable_ingestion_id"] == "c" * 36
    assert flow["_existing_evidence"] is True
    assert "foto que ya está guardada" in text


@pytest.mark.asyncio
async def test_comando_manual_con_referencia_reutiliza_ingesta(monkeypatch):
    update = _update()
    context = SimpleNamespace(args=["abcd1234"])
    ingestion_id = "a" * 36
    monkeypatch.setattr(bot.db, "buscar_ingestion_por_referencia", AsyncMock(return_value={"id": ingestion_id}))
    start = AsyncMock(return_value="modo manual con original")
    monkeypatch.setattr(manual_albaran, "iniciar_desde_ingestion", start)

    await bot.cmd_manual(update, context)

    start.assert_awaited_once_with(update.effective_chat.id, update.effective_user.id, ingestion_id)
    assert update.message.reply_text.await_args.args[0] == "modo manual con original"


@pytest.mark.asyncio
async def test_corregir_es_alias_seguro_de_editar(monkeypatch):
    update = _update()
    context = SimpleNamespace(args=["abcd1234", "linea", "1", "importe", "12,50"])
    delegated = AsyncMock()
    monkeypatch.setattr(bot, "cmd_editar", delegated)

    await bot.cmd_corregir(update, context)

    delegated.assert_awaited_once_with(update, context)


@pytest.mark.asyncio
async def test_ultimos_da_referencias_utilizables(monkeypatch):
    update = _update()
    context = SimpleNamespace(args=[])
    monkeypatch.setattr(bot.db, "listar_albaranes_recientes", AsyncMock(return_value=[{
        "id": "deadbeef" + "0" * 28, "fecha": "2026-08-01", "total": 123.45,
        "status": "confirmed", "proveedores": {"nombre": "Proveedor Uno"},
    }]))

    await bot.cmd_ultimos(update, context)

    answer = update.message.reply_text.await_args.args[0]
    assert "deadbeef" in answer
    assert "Proveedor Uno" in answer
    assert "/detalle REFERENCIA" in answer


@pytest.mark.asyncio
async def test_feedback_se_audita_y_se_notifica(monkeypatch):
    update = _update(chat_id=123456789)
    context = SimpleNamespace(
        args=["El", "importe", "salió", "mal"],
        bot=SimpleNamespace(send_message=AsyncMock()),
    )
    audit = AsyncMock()
    monkeypatch.setattr(bot.db, "registrar_evento_auditoria", audit)
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_CHAT_ID", "987654321")

    await bot.cmd_feedback(update, context)

    audit.assert_awaited_once()
    assert audit.await_args.kwargs["ingestion_id"] is None
    assert audit.await_args.kwargs["data"] == {"message": "El importe salió mal"}
    assert "chat_id" not in audit.await_args.kwargs["data"]
    context.bot.send_message.assert_awaited_once()
    assert "Feedback general guardado" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_feedback_con_referencia_se_asocia_a_ingestion_sin_chat_id(monkeypatch):
    update = _update(chat_id=123456789)
    ingestion_id = "a" * 36
    context = SimpleNamespace(
        args=[ingestion_id[:8], "El", "importe", "de", "la", "línea", "2", "está", "mal"],
        bot=SimpleNamespace(send_message=AsyncMock()),
    )
    monkeypatch.setattr(
        bot.db, "buscar_ingestion_por_referencia",
        AsyncMock(return_value={"id": ingestion_id}),
    )
    audit = AsyncMock()
    monkeypatch.setattr(bot.db, "registrar_evento_auditoria", audit)
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_CHAT_ID", "987654321")

    await bot.cmd_feedback(update, context)

    audit.assert_awaited_once()
    assert audit.await_args.kwargs["ingestion_id"] == ingestion_id
    assert audit.await_args.kwargs["data"] == {
        "message": "El importe de la línea 2 está mal"
    }
    assert "chat_id" not in audit.await_args.kwargs["data"]
    assert "asociado al documento" in update.message.reply_text.await_args.args[0]
    assert ingestion_id[:8] in context.bot.send_message.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_feedback_con_referencia_inexistente_o_ambigua_no_se_guarda(monkeypatch):
    update = _update()
    context = SimpleNamespace(
        args=["deadbeef", "No", "cuadra"],
        bot=SimpleNamespace(send_message=AsyncMock()),
    )
    monkeypatch.setattr(
        bot.db, "buscar_ingestion_por_referencia", AsyncMock(return_value=None)
    )
    audit = AsyncMock()
    monkeypatch.setattr(bot.db, "registrar_evento_auditoria", audit)

    await bot.cmd_feedback(update, context)

    audit.assert_not_awaited()
    context.bot.send_message.assert_not_awaited()
    assert "no existe o es ambigua" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_foto_como_archivo_conserva_calidad_y_usa_intake(monkeypatch):
    update = _update()
    update.message.document = SimpleNamespace(
        mime_type="image/jpeg", file_id="file-id", file_unique_id="unique-id"
    )
    downloaded = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(b"jpeg")))
    context = SimpleNamespace(bot=SimpleNamespace(get_file=AsyncMock(return_value=downloaded)))
    intake = SimpleNamespace(duplicate=False, queue_position=1, ingestion_id="d" * 36, status="queued")
    monkeypatch.setattr(bot, "receive_image", AsyncMock(return_value=intake))
    monkeypatch.setattr(manual_albaran, "flujo_activo", lambda _chat_id: False)

    await bot.handle_image_document(update, context)

    bot.receive_image.assert_awaited_once()
    assert "Recibido y guardado" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_reenvio_fallido_ofrece_reintento_y_manual(monkeypatch):
    update = _update()
    update.message.document = SimpleNamespace(
        mime_type="image/jpeg", file_id="file-id", file_unique_id="unique-id"
    )
    downloaded = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(b"jpeg")))
    context = SimpleNamespace(bot=SimpleNamespace(get_file=AsyncMock(return_value=downloaded)))
    intake = SimpleNamespace(
        duplicate=True, queue_position=0, ingestion_id="d" * 36, status="failed"
    )
    monkeypatch.setattr(bot, "receive_image", AsyncMock(return_value=intake))
    monkeypatch.setattr(manual_albaran, "flujo_activo", lambda _chat_id: False)

    await bot.handle_image_document(update, context)

    markup = update.message.reply_text.await_args.kwargs["reply_markup"]
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert labels == ["🔄 Reintentar OCR", "✍️ Introducir a mano"]
