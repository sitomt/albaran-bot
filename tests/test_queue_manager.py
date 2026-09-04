from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import queue_manager
from src.accounting_validation import ValidationReport
from src.ingestion_service import (
    CandidateResult,
    Classification,
    TerminalDocumentRejected,
    Usage,
)


def _candidate(*, open_review_count: int) -> CandidateResult:
    return CandidateResult(
        ingestion_id="a" * 36,
        candidate_artifact_id="artifact",
        candidate={"header": {}, "lines": []},
        validation=ValidationReport(issues=(), line_sum=None, auto_confirmable=True),
        classification=Classification(
            document_type="delivery_note",
            handwritten=False,
            quality=100,
            confidence=100,
            reason="",
            raw={},
            usage=Usage(),
            duration_ms=1,
        ),
        probable_duplicate=None,
        open_review_count=open_review_count,
    )


def _job() -> dict:
    return {
        "id": "job-1",
        "ingestion_id": "a" * 36,
        "attempts": 1,
        "max_attempts": 3,
    }


def _stop_after_one_job(monkeypatch, job: dict) -> None:
    monkeypatch.setattr(
        queue_manager.db,
        "reclamar_siguiente_job",
        AsyncMock(side_effect=[job, asyncio.CancelledError()]),
    )


def test_candidato_limpio_con_revision_humana_nunca_es_autoconfirmable():
    assert _candidate(open_review_count=1).needs_review is True
    assert _candidate(open_review_count=0).needs_review is False


@pytest.mark.asyncio
async def test_worker_deja_candidato_limpio_en_revision_sin_autoconfirmar(monkeypatch):
    job = _job()
    _stop_after_one_job(monkeypatch, job)
    monkeypatch.setattr(
        queue_manager.db,
        "obtener_ingestion",
        AsyncMock(return_value={"id": job["ingestion_id"], "telegram_chat_id": 123}),
    )
    finalize_job = AsyncMock(return_value=True)
    update_ingestion = AsyncMock()
    monkeypatch.setattr(queue_manager.db, "finalizar_job_con_lease", finalize_job)
    monkeypatch.setattr(queue_manager.db, "actualizar_ingestion", update_ingestion)
    monkeypatch.setattr(queue_manager, "process_ingestion", AsyncMock(return_value=_candidate(open_review_count=1)))
    confirm = AsyncMock()
    monkeypatch.setattr(queue_manager, "confirm_candidate", confirm)
    monkeypatch.setattr(queue_manager, "format_candidate_summary", lambda _result: "Revisión requerida")
    telegram = SimpleNamespace(send_message=AsyncMock())

    with pytest.raises(asyncio.CancelledError):
        await queue_manager.durable_worker(telegram, "worker-test")

    confirm.assert_not_awaited()
    update_ingestion.assert_not_awaited()
    finalize_job.assert_awaited_once_with(
        job["id"], "worker-test", estado="completado", stage="needs_review",
    )
    assert telegram.send_message.await_count == 2
    assert telegram.send_message.await_args_list[-1].kwargs["text"] == "Revisión requerida"


@pytest.mark.asyncio
async def test_worker_solo_confirma_candidato_sin_revisiones(monkeypatch):
    job = _job()
    _stop_after_one_job(monkeypatch, job)
    monkeypatch.setattr(
        queue_manager.db,
        "obtener_ingestion",
        AsyncMock(return_value={"id": job["ingestion_id"], "telegram_chat_id": 123}),
    )
    finalize_job = AsyncMock(return_value=True)
    monkeypatch.setattr(queue_manager.db, "finalizar_job_con_lease", finalize_job)
    renew = AsyncMock(return_value=True)
    monkeypatch.setattr(queue_manager.db, "renovar_lease_job", renew)
    monkeypatch.setattr(queue_manager, "process_ingestion", AsyncMock(return_value=_candidate(open_review_count=0)))
    confirm = AsyncMock(return_value={"albaran_id": "deadbeef" + "0" * 28})
    monkeypatch.setattr(queue_manager, "confirm_candidate", confirm)
    telegram = SimpleNamespace(send_message=AsyncMock())

    with pytest.raises(asyncio.CancelledError):
        await queue_manager.durable_worker(telegram, "worker-test")

    confirm.assert_awaited_once_with(
        job["ingestion_id"], actor_id="worker-test", actor_type="system"
    )
    renew.assert_awaited_once_with(job["id"], "worker-test", lease_seconds=300)
    finalize_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_marca_failed_solo_ante_un_fallo_real_de_procesamiento(monkeypatch):
    job = _job()
    _stop_after_one_job(monkeypatch, job)
    ingestion = {"id": job["ingestion_id"], "telegram_chat_id": 123}
    monkeypatch.setattr(queue_manager.db, "obtener_ingestion", AsyncMock(return_value=ingestion))
    finalize_job = AsyncMock(return_value=True)
    update_ingestion = AsyncMock()
    audit = AsyncMock()
    monkeypatch.setattr(queue_manager.db, "finalizar_job_con_lease", finalize_job)
    monkeypatch.setattr(queue_manager.db, "actualizar_ingestion", update_ingestion)
    monkeypatch.setattr(queue_manager.db, "registrar_evento_auditoria", audit)
    monkeypatch.setattr(
        queue_manager, "process_ingestion", AsyncMock(side_effect=ValueError("OCR inválido"))
    )
    confirm = AsyncMock()
    monkeypatch.setattr(queue_manager, "confirm_candidate", confirm)
    telegram = SimpleNamespace(send_message=AsyncMock())

    with pytest.raises(asyncio.CancelledError):
        await queue_manager.durable_worker(telegram, "worker-test")

    confirm.assert_not_awaited()
    assert finalize_job.await_args.kwargs["estado"] == "error"
    assert finalize_job.await_args.kwargs["stage"] == "failed"
    assert update_ingestion.await_args.kwargs["status"] == "failed"
    audit.assert_awaited_once()
    assert "No pude procesar" in telegram.send_message.await_args_list[-1].kwargs["text"]


@pytest.mark.asyncio
async def test_heartbeat_renueva_el_lease_mientras_procesa(monkeypatch):
    release = asyncio.Event()

    async def slow_process(_ingestion_id, *, attempt):
        await release.wait()
        return _candidate(open_review_count=1)

    renew = AsyncMock(return_value=True)
    monkeypatch.setattr(queue_manager, "process_ingestion", slow_process)
    monkeypatch.setattr(queue_manager.db, "renovar_lease_job", renew)

    task = asyncio.create_task(queue_manager._process_with_lease_heartbeat(
        ingestion_id="a" * 36, attempt=1, job_id="job-1", worker_id="worker-test",
        lease_seconds=1, interval_seconds=0.01,
    ))
    await asyncio.sleep(0.035)
    release.set()
    result = await task

    assert result.needs_review is True
    assert renew.await_count >= 2
    renew.assert_any_await("job-1", "worker-test", lease_seconds=1)


@pytest.mark.asyncio
async def test_heartbeat_cancela_proceso_al_perder_lease(monkeypatch):
    cancelled = asyncio.Event()

    async def never_finishes(_ingestion_id, *, attempt):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(queue_manager, "process_ingestion", never_finishes)
    monkeypatch.setattr(
        queue_manager.db, "renovar_lease_job", AsyncMock(return_value=False)
    )

    with pytest.raises(queue_manager.LeaseLostError):
        await queue_manager._process_with_lease_heartbeat(
            ingestion_id="a" * 36, attempt=1, job_id="job-1", worker_id="worker-test",
            lease_seconds=1, interval_seconds=0.01,
        )

    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_worker_vencido_no_sobrescribe_ni_notifica_resultado(monkeypatch):
    job = _job()
    _stop_after_one_job(monkeypatch, job)
    monkeypatch.setattr(
        queue_manager.db, "obtener_ingestion",
        AsyncMock(return_value={"id": job["ingestion_id"], "telegram_chat_id": 123}),
    )
    monkeypatch.setattr(
        queue_manager.db, "finalizar_job_con_lease", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        queue_manager, "process_ingestion",
        AsyncMock(return_value=_candidate(open_review_count=1)),
    )
    monkeypatch.setattr(queue_manager, "format_candidate_summary", lambda _result: "No enviar")
    telegram = SimpleNamespace(send_message=AsyncMock())

    with pytest.raises(asyncio.CancelledError):
        await queue_manager.durable_worker(telegram, "worker-test")

    assert telegram.send_message.await_count == 1
    assert "Procesando" in telegram.send_message.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_rechazo_terminal_preserva_rejected_y_no_se_convierte_en_failed(monkeypatch):
    job = _job()
    _stop_after_one_job(monkeypatch, job)
    ingestion = {"id": job["ingestion_id"], "telegram_chat_id": 123}
    monkeypatch.setattr(queue_manager.db, "obtener_ingestion", AsyncMock(return_value=ingestion))
    finalize = AsyncMock(return_value=True)
    update_ingestion = AsyncMock()
    audit = AsyncMock()
    monkeypatch.setattr(queue_manager.db, "finalizar_job_con_lease", finalize)
    monkeypatch.setattr(queue_manager.db, "actualizar_ingestion", update_ingestion)
    monkeypatch.setattr(queue_manager.db, "registrar_evento_auditoria", audit)
    monkeypatch.setattr(
        queue_manager, "process_ingestion",
        AsyncMock(side_effect=TerminalDocumentRejected(
            "No es un albarán", reason="document_type:receipt"
        )),
    )
    telegram = SimpleNamespace(send_message=AsyncMock())

    with pytest.raises(asyncio.CancelledError):
        await queue_manager.durable_worker(telegram, "worker-test")

    finalize.assert_awaited_once_with(
        job["id"], "worker-test", estado="completado", stage="rejected",
        error_code="document_rejected", error_detalle="No es un albarán",
    )
    update_ingestion.assert_awaited_once_with(
        job["ingestion_id"], status="rejected",
        duplicate_reason="document_type:receipt",
    )
    assert all(call.kwargs.get("status") != "failed" for call in update_ingestion.await_args_list)
    audit.assert_awaited_once()
    assert "Documento rechazado" in telegram.send_message.await_args_list[-1].kwargs["text"]


@pytest.mark.asyncio
async def test_fallo_transitorio_programa_espera_antes_de_reintentar(monkeypatch):
    """Un 429 reintentado en el mismo segundo solo garantiza otro 429."""
    job = _job()
    _stop_after_one_job(monkeypatch, job)
    ingestion = {"id": job["ingestion_id"], "telegram_chat_id": 123}
    monkeypatch.setattr(queue_manager.db, "obtener_ingestion", AsyncMock(return_value=ingestion))
    finalize_job = AsyncMock(return_value=True)
    audit = AsyncMock()
    monkeypatch.setattr(queue_manager.db, "finalizar_job_con_lease", finalize_job)
    monkeypatch.setattr(queue_manager.db, "actualizar_ingestion", AsyncMock())
    monkeypatch.setattr(queue_manager.db, "registrar_evento_auditoria", audit)
    monkeypatch.setattr(
        queue_manager, "process_ingestion",
        AsyncMock(side_effect=RuntimeError("Status 429. Rate limit exceeded")),
    )
    telegram = SimpleNamespace(send_message=AsyncMock())

    with pytest.raises(asyncio.CancelledError):
        await queue_manager.durable_worker(telegram, "worker-test")

    kwargs = finalize_job.await_args.kwargs
    assert kwargs["estado"] == "pendiente"
    assert kwargs["stage"] == "retry_wait"
    scheduled = datetime.fromisoformat(kwargs["available_at"])
    assert scheduled > datetime.now(timezone.utc) + timedelta(seconds=15)
    assert audit.await_args.kwargs["data"]["retry_in_seconds"] >= 20


@pytest.mark.asyncio
async def test_fallo_definitivo_no_programa_espera(monkeypatch):
    job = _job()
    _stop_after_one_job(monkeypatch, job)
    monkeypatch.setattr(
        queue_manager.db, "obtener_ingestion",
        AsyncMock(return_value={"id": job["ingestion_id"], "telegram_chat_id": 123}),
    )
    finalize_job = AsyncMock(return_value=True)
    monkeypatch.setattr(queue_manager.db, "finalizar_job_con_lease", finalize_job)
    monkeypatch.setattr(queue_manager.db, "actualizar_ingestion", AsyncMock())
    monkeypatch.setattr(queue_manager.db, "registrar_evento_auditoria", AsyncMock())
    monkeypatch.setattr(
        queue_manager, "process_ingestion", AsyncMock(side_effect=ValueError("OCR inválido")),
    )

    with pytest.raises(asyncio.CancelledError):
        await queue_manager.durable_worker(SimpleNamespace(send_message=AsyncMock()), "worker-test")

    assert "available_at" not in finalize_job.await_args.kwargs


def test_backoff_crece_con_los_intentos_y_esta_acotado():
    error = RuntimeError("Status 429")
    primero = queue_manager._retry_delay_seconds(error, 1)
    segundo = queue_manager._retry_delay_seconds(error, 2)
    assert 20 <= primero <= 25
    assert 40 <= segundo <= 45
    assert queue_manager._retry_delay_seconds(error, 99) <= queue_manager._RETRY_MAX_SECONDS


def test_backoff_respeta_el_retry_after_del_proveedor():
    assert queue_manager._retry_delay_seconds(
        RuntimeError('{"error":"rate_limited","retry-after": 42}'), 1
    ) == 42
    assert queue_manager._retry_delay_seconds(
        RuntimeError("Rate limit exceeded, try again in 7s"), 3
    ) == 7


@pytest.mark.asyncio
async def test_reintento_no_repite_el_aviso_de_procesando(monkeypatch):
    """El acuse de recibo es por documento; un intento no es noticia para nadie."""
    job = {**_job(), "attempts": 2}
    _stop_after_one_job(monkeypatch, job)
    monkeypatch.setattr(
        queue_manager.db, "obtener_ingestion",
        AsyncMock(return_value={"id": job["ingestion_id"], "telegram_chat_id": 123}),
    )
    monkeypatch.setattr(queue_manager.db, "finalizar_job_con_lease", AsyncMock(return_value=True))
    monkeypatch.setattr(queue_manager.db, "actualizar_ingestion", AsyncMock())
    monkeypatch.setattr(
        queue_manager, "process_ingestion",
        AsyncMock(return_value=_candidate(open_review_count=1)),
    )
    monkeypatch.setattr(queue_manager, "format_candidate_summary", lambda _r: "Revisión requerida")
    telegram = SimpleNamespace(send_message=AsyncMock())

    with pytest.raises(asyncio.CancelledError):
        await queue_manager.durable_worker(telegram, "worker-test")

    enviados = [call.kwargs["text"] for call in telegram.send_message.await_args_list]
    assert not any(texto.startswith("Procesando") for texto in enviados)
    assert enviados == ["Revisión requerida"]
