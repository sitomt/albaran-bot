"""Recepción durable y acotada de documentos antes de cualquier ACK."""
from __future__ import annotations

import asyncio
import hashlib
import io
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from PIL import Image, ImageOps, UnidentifiedImageError

from . import supabase_client as db
from .config import settings

Image.MAX_IMAGE_PIXELS = 40_000_000
_intake_lock = asyncio.Lock()


@dataclass(frozen=True)
class IntakeResult:
    ingestion_id: str
    duplicate: bool
    status: str
    queue_position: int


class IntakeQueueFullError(ValueError):
    """Backpressure esperado; el usuario debe esperar, no reintentar en bucle."""


def _validate_image(data: bytes) -> tuple[str, str]:
    max_bytes = int(getattr(settings, "MAX_DOCUMENT_BYTES", 15 * 1024 * 1024))
    if not data or len(data) > max_bytes:
        raise ValueError(f"La imagen debe ocupar entre 1 byte y {max_bytes // (1024 * 1024)} MB")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
            image_format = (image.format or "").upper()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("El archivo no es una imagen válida o segura") from exc
    content_types = {"JPEG": ("image/jpeg", "jpg"), "PNG": ("image/png", "png")}
    if image_format not in content_types:
        raise ValueError("Solo se admiten imágenes JPEG o PNG")
    return content_types[image_format]


def perceptual_hash(data: bytes) -> str:
    """dHash canónico de 64 bits, tolerante a orientación; nunca es identidad."""
    with Image.open(io.BytesIO(data)) as source:
        original = ImageOps.exif_transpose(source).convert("L")
        hashes: list[int] = []
        for angle in (0, 90, 180, 270):
            image = original.rotate(angle, expand=True).resize((9, 8))
            pixels = image.tobytes()
            bits = 0
            for row in range(8):
                for column in range(8):
                    left = pixels[row * 9 + column]
                    right = pixels[row * 9 + column + 1]
                    bits = (bits << 1) | int(left > right)
            hashes.append(bits)
    return f"{min(hashes):016x}"


async def receive_image(
    *, data: bytes, telegram_user_id: int, telegram_chat_id: int,
    file_unique_id: str | None,
) -> IntakeResult:
    content_type, extension = _validate_image(data)
    image_hash = hashlib.sha256(data).hexdigest()
    # El despliegue admite una única réplica. Serializar comprobación de límites +
    # alta evita que una ráfaga de updates lea el mismo contador y sobrepase el
    # backpressure antes de que PostgreSQL vea las nuevas ingestas.
    async with _intake_lock:
        return await _receive_image_locked(
            data=data,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            file_unique_id=file_unique_id,
            content_type=content_type,
            extension=extension,
            image_hash=image_hash,
        )


async def _receive_image_locked(
    *, data: bytes, telegram_user_id: int, telegram_chat_id: int,
    file_unique_id: str | None, content_type: str, extension: str,
    image_hash: str,
) -> IntakeResult:

    existing = await db.buscar_ingestion_exacta(
        telegram_user_id=telegram_user_id,
        file_unique_id=file_unique_id,
    ) if file_unique_id else None
    if existing is None:
        existing = await db.buscar_ingestion_exacta(image_hash=image_hash)
    if existing:
        return IntakeResult(existing["id"], True, existing["status"], 0)

    per_user_limit = int(getattr(settings, "MAX_PENDING_PER_USER", 20))
    global_limit = int(getattr(settings, "MAX_PENDING_GLOBAL", 100))
    user_pending = await db.contar_ingestions_abiertas(telegram_user_id)
    global_pending = await db.contar_ingestions_abiertas()
    if user_pending >= per_user_limit:
        raise ValueError(f"Ya tienes {user_pending} documentos pendientes; espera a que terminen")
    if global_pending >= global_limit:
        raise IntakeQueueFullError(
            "La cola está completa temporalmente. Esta foto no se ha guardado; "
            "vuelve a enviarla cuando terminen algunos documentos."
        )

    ingestion_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    bucket = str(getattr(settings, "STORAGE_BUCKET", "albaranes"))
    storage_path = f"intake/{now:%Y/%m}/{telegram_user_id}/{ingestion_id}.{extension}"
    idempotency_key = f"telegram:{telegram_user_id}:{file_unique_id or image_hash}"
    p_hash = perceptual_hash(data)
    await db.subir_original_privado(bucket, storage_path, data, content_type)
    durable_created = False
    try:
        await db.crear_ingestion_durable(
            ingestion_id=ingestion_id,
            idempotency_key=idempotency_key,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            telegram_file_unique_id=file_unique_id,
            storage_bucket=bucket,
            storage_path=storage_path,
            content_type=content_type,
            byte_size=len(data),
            image_hash=image_hash,
            metadata={"perceptual_hash": p_hash},
        )
        durable_created = True
        await db.actualizar_ingestion(ingestion_id, perceptual_hash=p_hash)
        await db.crear_job_ingestion(
            ingestion_id=ingestion_id,
            telegram_user_id=telegram_user_id,
            storage_path=storage_path,
            image_hash=image_hash,
            telegram_file_unique_id=file_unique_id,
        )
    except Exception as exc:
        if durable_created:
            # Conserva la evidencia durable: un operador puede recrear el job.
            await db.actualizar_ingestion(
                ingestion_id, status="failed", failed_at=now.isoformat(),
                metadata={"perceptual_hash": p_hash, "intake_error": str(exc)[:500]},
            )
        else:
            try:
                await db.borrar_original_privado(bucket, storage_path)
            except Exception:
                pass
            # Otra recepción concurrente puede haber ganado el índice único
            # después de nuestra lectura inicial. Se devuelve su fila como el mismo
            # resultado idempotente, en lugar de mostrar un error falso al usuario.
            concurrent = await db.buscar_ingestion_exacta(image_hash=image_hash)
            if concurrent:
                return IntakeResult(concurrent["id"], True, concurrent["status"], 0)
        raise

    position = await db.contar_ingestions_abiertas(telegram_user_id)
    await db.registrar_evento_auditoria(
        "ingestion.received", ingestion_id=ingestion_id,
        actor_type="telegram_user", actor_id=str(telegram_user_id),
        data={"byte_size": len(data), "content_type": content_type, "queue_position": position},
    )
    return IntakeResult(ingestion_id, False, "queued", position)
