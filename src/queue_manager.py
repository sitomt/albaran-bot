"""Workers durables: PostgreSQL es la cola; nunca se encolan fotos en RAM."""
from __future__ import annotations

import asyncio
import logging
import socket
import uuid
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from . import supabase_client as db
from .ingestion_service import (
    TerminalDocumentRejected,
    confirm_candidate,
    format_candidate_summary,
    process_ingestion,
)

if TYPE_CHECKING:
    from telegram import Bot

logger = logging.getLogger(__name__)

_LEASE_SECONDS = 300
_HEARTBEAT_INTERVAL_SECONDS = 90


class LeaseLostError(RuntimeError):
    """El job ya no pertenece a este worker; no puede publicar ningún estado."""


async def _finalize_owned_job(job_id: str, worker_id: str, **fields) -> bool:
    """Un fallo de red equivale a no poder demostrar propiedad; nunca mata el worker."""
    try:
        return await db.finalizar_job_con_lease(job_id, worker_id, **fields)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("No se pudo finalizar con CAS el job %s", job_id)
        return False


async def _lease_heartbeat(
    job_id: str, worker_id: str, *, lease_seconds: int, interval_seconds: float,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            renewed = await db.renovar_lease_job(
                job_id, worker_id, lease_seconds=lease_seconds
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise LeaseLostError("No se pudo demostrar la propiedad del lease") from exc
        if not renewed:
            raise LeaseLostError("El lease venció o fue reclamado por otro worker")


async def _process_with_lease_heartbeat(
    *, ingestion_id: str, attempt: int, job_id: str, worker_id: str,
    lease_seconds: int = _LEASE_SECONDS,
    interval_seconds: float = _HEARTBEAT_INTERVAL_SECONDS,
):
    processing = asyncio.create_task(process_ingestion(ingestion_id, attempt=attempt))
    heartbeat = asyncio.create_task(_lease_heartbeat(
        job_id, worker_id, lease_seconds=lease_seconds,
        interval_seconds=interval_seconds,
    ))
    try:
        done, _ = await asyncio.wait(
            {processing, heartbeat}, return_when=asyncio.FIRST_COMPLETED
        )
        # Si ambos terminan a la vez, perder el lease prevalece sobre el resultado:
        # un worker sin propiedad nunca debe continuar hacia una escritura final.
        if heartbeat in done:
            await heartbeat
        return await processing
    finally:
        for task in (processing, heartbeat):
            if not task.done():
                task.cancel()
        await asyncio.gather(processing, heartbeat, return_exceptions=True)


def _retryable_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(token in text for token in (
        "timeout", "timed out", "429", "capacity", "temporarily", "connection",
        "502", "503", "504", "rate limit",
    ))


async def durable_worker(bot: "Bot", worker_id: str) -> None:
    """Reclama mediante lease; un reinicio recupera el job y su original privado."""
    logger.info("Worker durable iniciado: %s", worker_id)
    idle_seconds = 0.5
    while True:
        job = None
        try:
            job = await db.reclamar_siguiente_job(worker_id, lease_seconds=_LEASE_SECONDS)
            if not job:
                await asyncio.sleep(idle_seconds)
                idle_seconds = min(idle_seconds * 1.5, 5.0)
                continue
            idle_seconds = 0.5
            ingestion_id = job.get("ingestion_id")
            if not ingestion_id:
                await _finalize_owned_job(
                    job["id"], worker_id, estado="error", stage="failed",
                    error_code="missing_ingestion", error_detalle="Job sin ingestion_id",
                )
                continue
            ingestion = await db.obtener_ingestion(ingestion_id)
            if not ingestion:
                raise RuntimeError("ingesta durable no encontrada")
            chat_id = int(ingestion["telegram_chat_id"])
            await bot.send_message(chat_id=chat_id, text=f"Procesando {ingestion_id[:8]}…")
            result = await _process_with_lease_heartbeat(
                ingestion_id=ingestion_id, attempt=int(job.get("attempts") or 1),
                job_id=job["id"], worker_id=worker_id,
            )

            if not result.needs_review:
                # `confirm_albaran_v1` publica el albarán y completa su job en una
                # sola transacción. Verificamos/extendemos la propiedad justo antes;
                # no intentamos un segundo cierre CAS después de que la RPC libere el lease.
                renewed = await db.renovar_lease_job(
                    job["id"], worker_id, lease_seconds=_LEASE_SECONDS
                )
                if not renewed:
                    raise LeaseLostError("El lease se perdió antes de la publicación")
                published = await confirm_candidate(
                    ingestion_id, actor_id=worker_id, actor_type="system"
                )
                await bot.send_message(
                    chat_id=chat_id,
                    text=("Albarán confirmado automáticamente. Referencia: "
                          f"{str(published.get('albaran_id'))[:8]}"),
                )
            else:
                finalized = await _finalize_owned_job(
                    job["id"], worker_id, estado="completado", stage="needs_review",
                )
                if not finalized:
                    raise LeaseLostError("El lease se perdió antes de finalizar el job")
                markup = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "✅ Revisar / confirmar", callback_data=f"rv:open:{ingestion_id}"
                    ),
                    InlineKeyboardButton(
                        "🚫 Rechazar", callback_data=f"rv:reject:{ingestion_id}"
                    ),
                ]])
                await bot.send_message(
                    chat_id=chat_id, text=format_candidate_summary(result),
                    reply_markup=markup,
                )
        except asyncio.CancelledError:
            raise
        except LeaseLostError as exc:
            logger.warning("Worker %s abandonó el job por pérdida de lease: %s", worker_id, exc)
            continue
        except TerminalDocumentRejected as exc:
            if not job:
                continue
            ingestion_id = job.get("ingestion_id")
            finalized = await _finalize_owned_job(
                job["id"], worker_id, estado="completado", stage="rejected",
                error_code="document_rejected", error_detalle=str(exc)[:1000],
            )
            if not finalized:
                logger.warning("No se publicó el rechazo: lease perdido para job %s", job["id"])
                continue
            if ingestion_id:
                await db.actualizar_ingestion(
                    ingestion_id, status="rejected", duplicate_reason=exc.reason
                )
                await db.registrar_evento_auditoria(
                    "ingestion.rejected", ingestion_id=ingestion_id, job_id=job["id"],
                    data={"reason": exc.reason},
                )
                rejected = await db.obtener_ingestion(ingestion_id)
                chat_id = int((rejected or {}).get("telegram_chat_id") or 0)
                if chat_id:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=("Documento rechazado: no parece un albarán de proveedor. "
                              f"Referencia: {str(ingestion_id)[:8]}."),
                    )
        except Exception as exc:
            logger.error("Fallo de worker durable %s: %s", worker_id, exc, exc_info=True)
            if not job:
                await asyncio.sleep(2)
                continue
            ingestion_id = job.get("ingestion_id")
            attempts = int(job.get("attempts") or 1)
            max_attempts = int(job.get("max_attempts") or 3)
            retryable = _retryable_error(exc) and attempts < max_attempts
            finalized = await _finalize_owned_job(
                job["id"], worker_id,
                estado="pendiente" if retryable else "error",
                stage="retry_wait" if retryable else "failed",
                error_code="provider_transient" if retryable else "processing_failed",
                error_detalle=str(exc)[:1000],
            )
            if not finalized:
                logger.warning("No se publicó el fallo: lease perdido para job %s", job["id"])
                continue
            if ingestion_id:
                from datetime import datetime, timezone
                fields = {"status": "queued" if retryable else "failed"}
                if not retryable:
                    fields["failed_at"] = datetime.now(timezone.utc).isoformat()
                await db.actualizar_ingestion(ingestion_id, **fields)
                await db.registrar_evento_auditoria(
                    "ingestion.retry_scheduled" if retryable else "ingestion.failed",
                    ingestion_id=ingestion_id, job_id=job["id"],
                    data={"attempt": attempts, "error": str(exc)[:500]},
                )
            try:
                failed = await db.obtener_ingestion(ingestion_id) if ingestion_id else None
                chat_id = int((failed or {}).get("telegram_chat_id") or 0)
                if chat_id and not retryable:
                    markup = InlineKeyboardMarkup([
                        [InlineKeyboardButton(
                            "🔄 Reintentar OCR", callback_data=f"job:retry:{ingestion_id}"
                        )],
                        [InlineKeyboardButton(
                            "✍️ Introducir a mano", callback_data=f"manual:start:{ingestion_id}"
                        )],
                    ])
                    await bot.send_message(
                        chat_id=chat_id,
                        text=("No pude procesar el documento. Se conserva de forma segura. "
                              f"Referencia: {str(ingestion_id)[:8]}. Puedes reintentarlo o "
                              "registrarlo a mano usando la misma foto."),
                        reply_markup=markup,
                    )
            except Exception:
                logger.exception("No se pudo notificar el fallo del job %s", job.get("id"))


async def start_durable_workers(bot: "Bot", n: int = 2) -> None:
    """Los leases hacen seguros reinicios; long polling exige una sola instancia."""
    host = socket.gethostname()
    workers = [
        durable_worker(bot, f"{host}:{uuid.uuid4().hex[:8]}:{index}")
        for index in range(max(1, n))
    ]
    await asyncio.gather(*workers)
