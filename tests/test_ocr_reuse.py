"""El OCR de un intento anterior es reutilizable: la foto original no cambia."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src import ingestion_service
from src.ingestion_service import Classification, Usage, _ocr_desde_artefacto


class _Parada(RuntimeError):
    """Corta el pipeline justo después del tramo de OCR."""


def _artefacto(text: str = "ALBARAN 123", confidence: float = 0.91) -> dict:
    return {
        "attempt": 1,
        "payload": {"text": text, "response": {"pages": []}, "confidence": confidence},
    }


def _clasificacion() -> Classification:
    return Classification(
        document_type="delivery_note", handwritten=False, quality=90, confidence=90,
        reason="", raw={}, usage=Usage(input_tokens=10, output_tokens=5), duration_ms=1,
    )


def test_artefacto_guardado_reconstruye_el_ocr_sin_uso_facturable():
    ocr = _ocr_desde_artefacto(_artefacto())
    assert ocr.text == "ALBARAN 123"
    assert ocr.confidence == pytest.approx(0.91)
    # Sin llamada no hay páginas ni tokens que cobrar.
    assert ocr.usage == Usage()
    assert ocr.duration_ms == 0


def _preparar(monkeypatch, artefacto: dict | None):
    monkeypatch.setattr(ingestion_service.db, "obtener_ingestion", AsyncMock(return_value={
        "id": "a" * 36, "storage_bucket": "albaranes", "storage_path": "x.jpg",
        "telegram_user_id": 1, "content_type": "image/jpeg",
    }))
    monkeypatch.setattr(ingestion_service.db, "coste_ai_mes_actual", AsyncMock(return_value=0.0))
    monkeypatch.setattr(
        ingestion_service.db, "descargar_original_privado", AsyncMock(return_value=b"jpeg")
    )
    monkeypatch.setattr(
        ingestion_service.db, "buscar_artefacto_ocr_reutilizable",
        AsyncMock(return_value=artefacto),
    )
    monkeypatch.setattr(ingestion_service, "_classify", AsyncMock(return_value=_clasificacion()))
    monkeypatch.setattr(ingestion_service, "_extract", AsyncMock(side_effect=_Parada()))
    registrar_artefacto = AsyncMock(return_value={"id": "artifact"})
    monkeypatch.setattr(
        ingestion_service.db, "registrar_artefacto_extraccion", registrar_artefacto
    )
    usos: list[str] = []

    async def _registrar_uso(*, operation, **_kwargs):
        usos.append(operation)
        return 0.0

    monkeypatch.setattr(ingestion_service, "_record_usage_safely", _registrar_uso)
    llamadas_ocr = AsyncMock()
    monkeypatch.setattr(ingestion_service, "_ocr", llamadas_ocr)
    return llamadas_ocr, usos, registrar_artefacto


@pytest.mark.asyncio
async def test_reintento_no_vuelve_a_pagar_el_ocr(monkeypatch):
    llamadas_ocr, usos, registrar_artefacto = _preparar(monkeypatch, _artefacto())

    with pytest.raises(_Parada):
        await ingestion_service.process_ingestion("a" * 36, attempt=2)

    llamadas_ocr.assert_not_awaited()
    assert "ocr" not in usos, "un OCR no ejecutado no puede aparecer en el ledger"
    assert "classification" in usos
    tipos = [call.kwargs["artifact_type"] for call in registrar_artefacto.await_args_list]
    assert "ocr_raw" not in tipos, "el artefacto ya existía; duplicarlo provoca el 409"


@pytest.mark.asyncio
async def test_primer_intento_sigue_llamando_al_ocr(monkeypatch):
    llamadas_ocr, usos, registrar_artefacto = _preparar(monkeypatch, None)
    llamadas_ocr.return_value = ingestion_service.OCRResult(
        text="ALBARAN 123", raw={}, confidence=0.9,
        usage=Usage(pages=1, request_id="r1"), duration_ms=12,
    )

    with pytest.raises(_Parada):
        await ingestion_service.process_ingestion("a" * 36, attempt=1)

    llamadas_ocr.assert_awaited_once()
    assert "ocr" in usos
    tipos = [call.kwargs["artifact_type"] for call in registrar_artefacto.await_args_list]
    assert "ocr_raw" in tipos
