from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from typing import Any

import httpx
from postgrest.exceptions import APIError
from supabase import acreate_client, AsyncClient

from .config import settings
from . import cost_ledger

logger = logging.getLogger(__name__)

_client: AsyncClient | None = None
_usage_ledger_lock = asyncio.Lock()
_USAGE_PAGE_SIZE = 1_000

_REQUIRED_PRODUCTION_TABLES = {
    "ingestions", "extraction_artifacts", "review_items", "audit_events", "ai_usage_events",
}
_REQUIRED_PRODUCTION_RPCS = {
    "claim_ingestion_job_v1", "confirm_albaran_v1", "archive_albaran_v1",
    "append_ai_usage_event_v1",
    "accept_confirm_candidate_v1", "reject_ingestion_v1", "retry_ingestion_v1",
    "resolve_ingestion_reference_v1", "resolve_albaran_reference_v1",
    "dashboard_snapshot_v1",
}


async def get_client() -> AsyncClient:
    global _client
    if _client is None:
        _client = await acreate_client(settings.SUPABASE_URL, settings.database_key)
    return _client


async def verificar_contrato_produccion() -> list[str]:
    """Comprueba esquema y privacidad antes de aceptar mensajes de Telegram."""
    headers = {
        "apikey": settings.SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}",
    }
    issues: list[str] = []
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(settings.SUPABASE_URL.rstrip("/") + "/rest/v1/", headers=headers)
        response.raise_for_status()
        paths = set((response.json().get("paths") or {}).keys())
        missing_tables = sorted(
            table for table in _REQUIRED_PRODUCTION_TABLES if f"/{table}" not in paths
        )
        missing_rpcs = sorted(
            rpc for rpc in _REQUIRED_PRODUCTION_RPCS if f"/rpc/{rpc}" not in paths
        )
        if missing_tables:
            issues.append("faltan tablas: " + ", ".join(missing_tables))
        if missing_rpcs:
            issues.append("faltan RPC: " + ", ".join(missing_rpcs))

        storage = await client.get(
            settings.SUPABASE_URL.rstrip("/") + "/storage/v1/bucket", headers=headers
        )
        storage.raise_for_status()
        bucket = next(
            (item for item in storage.json() if item.get("name") == settings.STORAGE_BUCKET), None
        )
        if not bucket:
            issues.append(f"falta el bucket {settings.STORAGE_BUCKET}")
        elif bucket.get("public") is not False:
            issues.append(f"el bucket {settings.STORAGE_BUCKET} es público")
    return issues


async def obtener_dashboard_snapshot() -> dict[str, Any]:
    """Lee el contrato agregado; solo un backend con service_role puede invocarlo."""
    client = await get_client()
    response = await client.rpc("dashboard_snapshot_v1").execute()
    data = _safe_data(response)
    return data if isinstance(data, dict) else {}


def _safe_data(res: Any, many: bool = False) -> Any:
    """Extrae .data de una respuesta supabase-py de forma segura."""
    if res is None:
        return [] if many else None
    data = getattr(res, "data", None)
    if many:
        return data or []
    return data


# ── Albaranes ─────────────────────────────────────────────────────────────────

async def buscar_albaran_duplicado_norm(numero_norm: str, proveedor_id: str) -> dict | None:
    """Busca duplicado por número normalizado (sin puntos, barras, espacios, en minúsculas)."""
    if not numero_norm:
        return None
    client = await get_client()
    res = (
        await client.table("albaranes")
        .select("id, numero_albaran, fecha, creado_en, total")
        .eq("numero_albaran_norm", numero_norm)
        .eq("proveedor_id", proveedor_id)
        .limit(1)
        .execute()
    )
    data = _safe_data(res, many=True)
    return data[0] if data else None


async def buscar_albaran_duplicado_por_nombre_proveedor(
    nombre_proveedor: str, fecha: str, total: float
) -> dict | None:
    """Señal de similitud por nombre, fecha y total, sin SQL dinámico."""
    client = await get_client()
    providers_res = await (
        client.table("proveedores").select("id,nombre")
        .ilike("nombre", nombre_proveedor.strip()).limit(20).execute()
    )
    provider_ids = [row["id"] for row in _safe_data(providers_res, many=True)]
    if not provider_ids:
        return None
    result = await (
        client.table("albaranes")
        .select("id,numero_albaran,fecha,creado_en,total")
        .in_("proveedor_id", provider_ids).eq("fecha", fecha)
        .eq("status", "confirmed").gte("total", total - 0.50)
        .lte("total", total + 0.50).limit(1).execute()
    )
    rows = _safe_data(result, many=True)
    return rows[0] if rows else None


async def buscar_albaran_por_referencia(reference: str) -> dict | None:
    if not re.fullmatch(r"[0-9a-fA-F-]{6,36}", reference or ""):
        return None
    client = await get_client()
    response = await client.rpc(
        "resolve_albaran_reference_v1", {"p_reference": reference}
    ).execute()
    data = _safe_data(response)
    return data if isinstance(data, dict) else None


async def listar_albaranes_recientes(limit: int = 10) -> list[dict]:
    """Lista acotada para que los propietarios encuentren referencias fácilmente."""
    client = await get_client()
    safe_limit = max(1, min(int(limit), 25))
    res = await (
        client.table("albaranes")
        .select("id,numero_albaran,fecha,total,status,origen,creado_en,proveedores(nombre)")
        .order("creado_en", desc=True)
        .limit(safe_limit)
        .execute()
    )
    return _safe_data(res, many=True)


async def obtener_detalle_albaran(reference: str) -> dict | None:
    albaran = await buscar_albaran_por_referencia(reference)
    if not albaran:
        return None
    client = await get_client()
    res = await (
        client.table("lineas_albaran")
        .select(
            "id,line_no,descripcion_limpia,cantidad,unidad,precio_unitario,"
            "descuento_pct,importe_neto,confianza,valores_observados"
        )
        .eq("albaran_id", albaran["id"])
        .order("line_no")
        .execute()
    )
    return {**albaran, "lineas": _safe_data(res, many=True)}


async def archivar_albaran(albaran_id: str, actor_id: int, reason: str) -> dict:
    client = await get_client()
    response = await client.rpc("archive_albaran_v1", {
        "p_albaran_id": albaran_id,
        "p_actor_id": str(actor_id),
        "p_reason": reason[:500],
    }).execute()
    return _safe_data(response) or {}


async def listar_proveedores() -> list[dict]:
    client = await get_client()
    res = (
        await client.table("proveedores")
        .select("nombre, nif, telefono, email, forma_pago_habitual")
        .order("nombre")
        .execute()
    )
    return _safe_data(res, many=True)


# ── Storage ───────────────────────────────────────────────────────────────────

async def subir_original_privado(
    bucket: str, path: str, data: bytes, content_type: str = "image/jpeg"
) -> str:
    """Persiste el original en un bucket privado y devuelve solo su ruta interna."""
    client = await get_client()
    await client.storage.from_(bucket).upload(
        path=path,
        file=data,
        file_options={"content-type": content_type, "upsert": "false"},
    )
    return path


async def descargar_original_privado(bucket: str, path: str) -> bytes:
    """Descarga un original usando las credenciales backend; nunca genera URL pública."""
    client = await get_client()
    data = await client.storage.from_(bucket).download(path)
    return bytes(data)


async def borrar_original_privado(bucket: str, path: str) -> None:
    client = await get_client()
    await client.storage.from_(bucket).remove([path])


# ── Ingesta durable (modelo production v1) ───────────────────────────────────

async def buscar_ingestion_exacta(
    *, image_hash: str | None = None, telegram_user_id: int | None = None,
    file_unique_id: str | None = None,
) -> dict | None:
    client = await get_client()
    query = client.table("ingestions").select(
        "id,status,duplicate_of,storage_bucket,storage_path,image_hash,received_at"
    )
    if file_unique_id and telegram_user_id:
        query = query.eq("telegram_user_id", telegram_user_id).eq(
            "telegram_file_unique_id", file_unique_id
        )
    elif image_hash:
        query = query.eq("image_hash", image_hash)
    else:
        return None
    res = await query.limit(1).execute()
    rows = _safe_data(res, many=True)
    return rows[0] if rows else None


async def crear_ingestion_durable(
    *, ingestion_id: str, idempotency_key: str, telegram_user_id: int,
    telegram_chat_id: int, storage_bucket: str, storage_path: str,
    content_type: str, byte_size: int, image_hash: str,
    telegram_file_unique_id: str | None = None, metadata: dict | None = None,
) -> dict:
    client = await get_client()
    payload = {
        "id": ingestion_id,
        "idempotency_key": idempotency_key,
        "telegram_user_id": telegram_user_id,
        "telegram_chat_id": telegram_chat_id,
        "telegram_file_unique_id": telegram_file_unique_id,
        "storage_bucket": storage_bucket,
        "storage_path": storage_path,
        "content_type": content_type,
        "byte_size": byte_size,
        "image_hash": image_hash,
        "status": "queued",
        "metadata": metadata or {},
    }
    res = await client.table("ingestions").insert(payload).execute()
    rows = _safe_data(res, many=True)
    return rows[0]


async def crear_ingestion_manual(
    *, ingestion_id: str, idempotency_key: str, telegram_user_id: int,
    telegram_chat_id: int, storage_bucket: str | None = None,
    storage_path: str | None = None, content_type: str | None = None,
    byte_size: int = 0, image_hash: str | None = None,
    metadata: dict | None = None,
) -> dict:
    client = await get_client()
    payload = {
        "id": ingestion_id,
        "idempotency_key": idempotency_key,
        "telegram_user_id": telegram_user_id,
        "telegram_chat_id": telegram_chat_id,
        "source_type": "manual",
        "storage_bucket": storage_bucket,
        "storage_path": storage_path,
        "content_type": content_type,
        "byte_size": byte_size,
        "image_hash": image_hash,
        "status": "extracted",
        "metadata": metadata or {},
    }
    res = await client.table("ingestions").insert(payload).execute()
    rows = _safe_data(res, many=True)
    return rows[0]


async def crear_job_ingestion(
    *, ingestion_id: str, telegram_user_id: int, storage_path: str,
    image_hash: str, telegram_file_unique_id: str | None = None,
) -> dict:
    client = await get_client()
    payload = {
        "telegram_user_id": telegram_user_id,
        "ingestion_id": ingestion_id,
        "storage_path": storage_path,
        "image_hash": image_hash,
        "telegram_file_unique_id": telegram_file_unique_id,
        "estado": "pendiente",
        "stage": "queued",
        "intentos": 0,
        "attempts": 0,
        "max_attempts": 3,
    }
    res = await client.table("jobs").insert(payload).execute()
    rows = _safe_data(res, many=True)
    return rows[0]


async def obtener_ingestion(ingestion_id: str) -> dict | None:
    client = await get_client()
    res = await client.table("ingestions").select("*").eq("id", ingestion_id).limit(1).execute()
    rows = _safe_data(res, many=True)
    return rows[0] if rows else None


async def buscar_ingestion_por_referencia(reference: str, telegram_user_id: int) -> dict | None:
    """Resuelve una referencia compartida solo para propietarios autorizados."""
    if not re.fullmatch(r"[0-9a-fA-F-]{6,36}", reference or ""):
        return None
    from .config import settings
    if telegram_user_id not in settings.allowed_users:
        return None
    client = await get_client()
    response = await client.rpc(
        "resolve_ingestion_reference_v1", {"p_reference": reference}
    ).execute()
    data = _safe_data(response)
    return data if isinstance(data, dict) else None


async def actualizar_ingestion(ingestion_id: str, **fields: Any) -> dict:
    client = await get_client()
    res = await client.table("ingestions").update(fields).eq("id", ingestion_id).execute()
    rows = _safe_data(res, many=True)
    return rows[0] if rows else {}


async def registrar_artefacto_extraccion(
    *, ingestion_id: str, attempt: int, artifact_type: str, payload: Any,
    model_name: str | None = None, model_version: str | None = None,
    prompt_version: str | None = None, input_tokens: int | None = None,
    output_tokens: int | None = None, pages: int | None = None,
    cost_usd: float | None = None, duration_ms: int | None = None,
    complete: bool = True,
) -> dict:
    client = await get_client()
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    row = {
        "ingestion_id": ingestion_id,
        "attempt": attempt,
        "artifact_type": artifact_type,
        "model_name": model_name,
        "model_version": model_version,
        "prompt_version": prompt_version,
        "payload": payload,
        "payload_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "pages": pages,
        "cost_usd": cost_usd,
        "duration_ms": duration_ms,
        "complete": complete,
    }
    try:
        res = await client.table("extraction_artifacts").insert(row).execute()
    except APIError as exc:
        # Un reintento (503 de la API de extracción, botón "Reintentar", lease perdido...)
        # puede repetir la misma llamada con el mismo `attempt` y volver a producir el
        # mismo payload. La restricción única existe justo para detectar eso: no es un
        # fallo, es la prueba de que ya quedó guardado. Reutilizamos la fila existente
        # en vez de tumbar el job y quemar su último intento.
        if exc.code == "23505":
            existing = await (
                client.table("extraction_artifacts").select("*")
                .eq("ingestion_id", ingestion_id).eq("attempt", attempt)
                .eq("artifact_type", artifact_type).eq("payload_sha256", row["payload_sha256"])
                .limit(1).execute()
            )
            rows = _safe_data(existing, many=True)
            if rows:
                return rows[0]
        raise
    rows = _safe_data(res, many=True)
    return rows[0]


async def reemplazar_revisiones_abiertas(ingestion_id: str, items: list[dict]) -> list[dict]:
    """Crea revisiones de un candidato nuevo; no borra decisiones ya resueltas."""
    if not items:
        return []
    client = await get_client()
    payload = [{"ingestion_id": ingestion_id, **item} for item in items]
    try:
        res = await client.table("review_items").insert(payload).execute()
    except APIError as exc:
        # Mismo caso que en registrar_artefacto_extraccion: un reintento (503,
        # botón "Reintentar", lease perdido...) puede repetir process_ingestion
        # sobre el mismo candidato y generar el mismo lote de revisiones. La
        # restricción única sobre (extraction_artifact_id, entity_type, entity_key,
        # field_name) existe para detectar justo eso: no es un fallo, es la prueba
        # de que ya se insertó. Reutilizamos las filas existentes de ese candidato
        # en vez de tumbar el job y quemar su último intento.
        if exc.code == "23505":
            artifact_ids = {
                str(item["extraction_artifact_id"])
                for item in items if item.get("extraction_artifact_id")
            }
            if artifact_ids:
                existing = await (
                    client.table("review_items").select("*")
                    .eq("ingestion_id", ingestion_id)
                    .in_("extraction_artifact_id", list(artifact_ids))
                    .execute()
                )
                rows = _safe_data(existing, many=True)
                if rows:
                    return rows
        raise
    return _safe_data(res, many=True)


async def listar_revisiones_ingestion(ingestion_id: str, *, only_open: bool = False) -> list[dict]:
    client = await get_client()
    query = client.table("review_items").select("*").eq("ingestion_id", ingestion_id)
    if only_open:
        query = query.eq("status", "open")
    res = await query.order("creado_en").execute()
    return _safe_data(res, many=True)


async def listar_ingestions_pendientes_revision(telegram_user_id: int) -> list[dict]:
    from .config import settings
    if telegram_user_id not in settings.allowed_users:
        return []
    client = await get_client()
    res = await (
        client.table("ingestions")
        .select("id,status,received_at,metadata,review_items(id,status,reason_code,field_name)")
        .eq("status", "needs_review")
        .order("received_at")
        .execute()
    )
    return _safe_data(res, many=True)


async def resolver_revision(
    review_id: str, *, status: str, accepted_value: Any,
    resolved_by: str, note: str | None = None,
) -> dict:
    from datetime import datetime, timezone
    if status not in {"accepted", "corrected", "rejected"}:
        raise ValueError("estado de revisión inválido")
    client = await get_client()
    res = await client.table("review_items").update({
        "status": status,
        "accepted_value": accepted_value,
        "resolved_by": resolved_by,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "resolution_note": note,
    }).eq("id", review_id).eq("status", "open").execute()
    rows = _safe_data(res, many=True)
    return rows[0] if rows else {}


async def resolver_revisiones_abiertas(
    ingestion_id: str, *, status: str, resolved_by: str, note: str | None = None,
) -> int:
    reviews = await listar_revisiones_ingestion(ingestion_id, only_open=True)
    count = 0
    for review in reviews:
        value = review.get("proposed_value")
        if value is None:
            value = review.get("observed_value")
        if await resolver_revision(
            review["id"], status=status, accepted_value=value,
            resolved_by=resolved_by, note=note,
        ):
            count += 1
    return count


async def buscar_artefacto_ocr_reutilizable(ingestion_id: str) -> dict | None:
    """Devuelve el OCR ya pagado de un intento anterior, si lo hay.

    El proveedor cobra por página cada vez que se llama, así que repetir el OCR
    en un reintento cuesta dinero para obtener exactamente el mismo texto: la
    foto original es inmutable. Solo se reutiliza un artefacto completo y con
    texto; uno vacío no ahorra nada y sí impediría detectar un OCR fallido.
    """
    client = await get_client()
    res = await (
        client.table("extraction_artifacts").select("*")
        .eq("ingestion_id", ingestion_id)
        .eq("artifact_type", "ocr_raw")
        .eq("complete", True)
        .order("attempt", desc=True)
        .limit(1)
        .execute()
    )
    rows = _safe_data(res, many=True)
    if not rows:
        return None
    payload = rows[0].get("payload")
    if not isinstance(payload, dict) or not str(payload.get("text") or "").strip():
        return None
    return rows[0]


async def siguiente_intento_extraccion(ingestion_id: str) -> int:
    client = await get_client()
    res = await client.table("extraction_artifacts").select("attempt").eq(
        "ingestion_id", ingestion_id
    ).order("attempt", desc=True).limit(1).execute()
    rows = _safe_data(res, many=True)
    return int(rows[0]["attempt"]) + 1 if rows else 1


async def confirmar_albaran_atomico(
    *, ingestion_id: str, idempotency_key: str, actor_type: str,
    actor_id: str, albaran: dict, lineas: list[dict],
    extraction_artifact_id: str | None = None,
) -> dict:
    client = await get_client()
    res = await client.rpc("confirm_albaran_v1", {
        "p_ingestion_id": ingestion_id,
        "p_idempotency_key": idempotency_key,
        "p_actor_type": actor_type,
        "p_actor_id": actor_id,
        "p_albaran": albaran,
        "p_lineas": lineas,
        "p_extraction_artifact_id": extraction_artifact_id,
    }).execute()
    return _safe_data(res) or {}


async def aceptar_y_confirmar_candidato_atomico(
    *, ingestion_id: str, candidate_artifact_id: str, idempotency_key: str,
    actor_id: str, albaran: dict, lineas: list[dict],
) -> dict:
    client = await get_client()
    response = await client.rpc("accept_confirm_candidate_v1", {
        "p_ingestion_id": ingestion_id,
        "p_candidate_artifact_id": candidate_artifact_id,
        "p_idempotency_key": idempotency_key,
        "p_actor_id": actor_id,
        "p_albaran": albaran,
        "p_lineas": lineas,
    }).execute()
    return _safe_data(response) or {}


async def rechazar_ingestion_atomico(
    *, ingestion_id: str, candidate_artifact_id: str, actor_id: str,
    as_duplicate: bool = False,
) -> dict:
    client = await get_client()
    response = await client.rpc("reject_ingestion_v1", {
        "p_ingestion_id": ingestion_id,
        "p_candidate_artifact_id": candidate_artifact_id,
        "p_actor_id": actor_id,
        "p_as_duplicate": as_duplicate,
    }).execute()
    return _safe_data(response) or {}


async def registrar_evento_auditoria(
    event_type: str, *, actor_type: str = "system", actor_id: str | None = None,
    ingestion_id: str | None = None, albaran_id: str | None = None,
    job_id: str | None = None, correlation_id: str | None = None,
    data: dict | None = None,
) -> None:
    client = await get_client()
    await client.table("audit_events").insert({
        "ingestion_id": ingestion_id,
        "albaran_id": albaran_id,
        "job_id": job_id,
        "correlation_id": correlation_id,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "event_type": event_type,
        "data": data or {},
    }).execute()


async def reclamar_siguiente_job(worker_id: str, lease_seconds: int = 300) -> dict | None:
    client = await get_client()
    res = await client.rpc("claim_ingestion_job_v1", {
        "p_worker_id": worker_id,
        "p_lease_seconds": lease_seconds,
    }).execute()
    rows = _safe_data(res, many=True)
    return rows[0] if rows else None


async def renovar_lease_job(
    job_id: str, worker_id: str, *, lease_seconds: int = 300,
) -> bool:
    """Renueva solo un lease vigente que todavía pertenece al worker."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=max(1, int(lease_seconds)))
    client = await get_client()
    response = await (
        client.table("jobs")
        .update({
            "lease_expires_at": expires_at.isoformat(),
            "actualizado_en": now.isoformat(),
        })
        .eq("id", job_id)
        .eq("estado", "procesando")
        .eq("lease_owner", worker_id)
        .gte("lease_expires_at", now.isoformat())
        .execute()
    )
    return bool(_safe_data(response, many=True))


async def finalizar_job_con_lease(
    job_id: str, worker_id: str, **campos: Any,
) -> bool:
    """CAS terminal: un worker vencido o reemplazado no puede sobrescribir el job."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    payload = {
        **campos,
        "lease_owner": None,
        "lease_expires_at": None,
        "actualizado_en": now.isoformat(),
    }
    client = await get_client()
    response = await (
        client.table("jobs")
        .update(payload)
        .eq("id", job_id)
        .eq("estado", "procesando")
        .eq("lease_owner", worker_id)
        .gte("lease_expires_at", now.isoformat())
        .execute()
    )
    return bool(_safe_data(response, many=True))


async def contar_ingestions_abiertas(telegram_user_id: int | None = None) -> int:
    """Incluye cola, procesamiento y revisiones para aplicar backpressure real."""
    client = await get_client()
    query = client.table("ingestions").select("id", count="exact").in_(
        "status", ["received", "queued", "processing", "extracted", "needs_review"]
    )
    if telegram_user_id is not None:
        query = query.eq("telegram_user_id", telegram_user_id)
    result = await query.execute()
    count = getattr(result, "count", None)
    return int(count if count is not None else len(_safe_data(result, many=True)))


async def buscar_ingestion_similar_perceptual(
    perceptual_hash: str, *, exclude_ingestion_id: str, max_distance: int = 5,
) -> dict | None:
    """Devuelve la ingesta activa/confirmada visualmente más próxima; nunca decide identidad."""
    if not re.fullmatch(r"[0-9a-f]{16}", perceptual_hash or ""):
        return None
    client = await get_client()
    response = await (
        client.table("ingestions")
        .select("id,status,perceptual_hash,metadata,received_at")
        .in_("status", ["received", "queued", "processing", "extracted", "needs_review", "confirmed"])
        .neq("id", exclude_ingestion_id)
        .order("received_at", desc=True)
        .limit(500)
        .execute()
    )
    target = int(perceptual_hash, 16)
    candidates: list[tuple[int, dict]] = []
    for row in _safe_data(response, many=True):
        value = str(row.get("perceptual_hash") or "")
        if not re.fullmatch(r"[0-9a-f]{16}", value):
            continue
        distance = (target ^ int(value, 16)).bit_count()
        if distance <= max_distance:
            candidates.append((distance, row))
    if not candidates:
        return None
    distance, row = min(candidates, key=lambda item: (item[0], str(item[1].get("received_at") or "")))
    return {**row, "perceptual_distance": distance}


async def registrar_uso_ai(
    *, operation: str, model: str, cost_usd: float,
    ingestion_id: str | None = None, user_id: int | None = None,
    request_id: str | None = None,
    input_tokens: int | None = None, output_tokens: int | None = None,
    pages: int | None = None, retries: int = 0,
    input_unit_price: float | None = None, output_unit_price: float | None = None,
    page_unit_price: float | None = None, metadata: dict | None = None,
) -> None:
    from datetime import datetime, timezone
    import uuid
    row = {
        "id": str(uuid.uuid4()),
        "ingestion_id": ingestion_id,
        "request_id": request_id,
        "user_id": user_id,
        "operation": operation,
        "provider": "mistral",
        "model": model,
        "input_tokens": input_tokens or 0,
        "output_tokens": output_tokens or 0,
        "pages": pages or 0,
        "retries": retries,
        "input_unit_price_usd": input_unit_price or 0,
        "output_unit_price_usd": output_unit_price or 0,
        "page_unit_price_usd": page_unit_price or 0,
        "cost_usd": cost_usd,
        "metadata": {"pricing_source": "configured-unit-rates", **(metadata or {})},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    async with _usage_ledger_lock:
        try:
            client = await get_client()
            await client.rpc("append_ai_usage_event_v1", {"p_event": row}).execute()
        except Exception:
            # La llamada del proveedor ya ha generado coste. Se conserva localmente
            # con fsync para que el documento pueda continuar sin ocultar el gasto.
            cost_ledger.append(row)
            logger.warning("Uso de IA guardado en spool local; Supabase no estaba disponible")
            return
        await _reconciliar_spool_costes_locked(client)


async def _reconciliar_spool_costes_locked(client: Any) -> int:
    """Vacía el spool con el lock ya adquirido; los UUID hacen seguro reintentar."""
    pending_rows = cost_ledger.pending()
    if not pending_rows:
        return 0
    try:
        for pending_row in pending_rows:
            await client.rpc(
                "append_ai_usage_event_v1", {"p_event": pending_row}
            ).execute()
    except Exception:
        logger.exception("El spool de costes sigue pendiente de reconciliación")
        return 0
    cost_ledger.clear()
    return len(pending_rows)


async def reconciliar_spool_costes() -> int:
    """Sincroniza consumo facturable pendiente, también cuando no hay llamadas nuevas."""
    async with _usage_ledger_lock:
        client = await get_client()
        return await _reconciliar_spool_costes_locked(client)


async def _listar_eventos_uso_desde(
    client: AsyncClient, *, columns: str, since: str, descending: bool = False,
) -> list[dict[str, Any]]:
    """Pagina todo el ledger desde ``since`` sin truncarlo al máximo de PostgREST.

    El conteo exacto de la primera página permite continuar incluso si el proyecto
    configura un ``max_rows`` menor que nuestro tamaño de página. El ledger es
    append-only y el orden compuesto hace determinista cada recorrido.
    """
    rows: list[dict[str, Any]] = []
    offset = 0
    expected_count: int | None = None
    while True:
        query = client.table("ai_usage_events").select(
            columns, count="exact" if offset == 0 else None
        ).gte("created_at", since).order("created_at", desc=descending).order(
            "id", desc=descending
        ).range(offset, offset + _USAGE_PAGE_SIZE - 1)
        response = await query.execute()
        page = _safe_data(response, many=True)
        if offset == 0:
            count = getattr(response, "count", None)
            if isinstance(count, int) and count >= 0:
                expected_count = count
        rows.extend(page)

        if expected_count is not None:
            if len(rows) >= expected_count:
                return rows
            if not page:
                raise RuntimeError(
                    "PostgREST devolvió una página vacía antes de completar "
                    f"ai_usage_events ({len(rows)}/{expected_count})"
                )
        elif len(page) < _USAGE_PAGE_SIZE:
            return rows
        offset += len(page)


async def coste_ai_mes_actual() -> float:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    client = await get_client()
    rows = await _listar_eventos_uso_desde(
        client, columns="id,cost_usd,created_at", since=month_start
    )
    pending_rows = [
        row for row in cost_ledger.pending()
        if str(row.get("created_at") or "") >= month_start
    ]
    # El mismo UUID puede estar ya en PostgreSQL y seguir en el spool si la
    # reconciliación se interrumpió después de la RPC pero antes del clear.
    unique_rows = {
        str(row.get("id") or f"legacy-{index}"): row
        for index, row in enumerate(rows + pending_rows)
    }
    return round(sum(float(row.get("cost_usd") or 0) for row in unique_rows.values()), 6)


async def desglose_costes_ai() -> dict[str, Any]:
    """Snapshot del ledger append-only: hoy, mes, operación, modelo y llamadas."""
    import calendar
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from .config import settings

    now_utc = datetime.now(timezone.utc)
    local_zone = ZoneInfo("Europe/Madrid")
    now_local = now_utc.astimezone(local_zone)
    month_start_local = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    today_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start_utc = month_start_local.astimezone(timezone.utc)
    today_start_utc = today_start_local.astimezone(timezone.utc)

    client = await get_client()
    rows = await _listar_eventos_uso_desde(
        client,
        columns=(
            "id,ingestion_id,user_id,operation,provider,model,input_tokens,output_tokens,"
            "pages,retries,cost_usd,metadata,created_at"
        ),
        since=month_start_utc.isoformat(),
        descending=True,
    )
    pending_rows = [
        row for row in cost_ledger.pending()
        if str(row.get("created_at") or "") >= month_start_utc.isoformat()
    ]
    unique_rows = {
        str(row.get("id") or f"legacy-{index}"): row
        for index, row in enumerate(rows + pending_rows)
    }
    rows = sorted(
        unique_rows.values(), key=lambda row: str(row.get("created_at") or ""), reverse=True
    )
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    total_month = total_today = document_cost_month = 0.0
    ingestions: set[str] = set()
    users: dict[str, dict[str, Any]] = {}
    latest: list[dict[str, Any]] = []
    for row in rows:
        cost = float(row.get("cost_usd") or 0)
        total_month += cost
        try:
            created = datetime.fromisoformat(str(row.get("created_at", "")).replace("Z", "+00:00"))
        except ValueError:
            created = month_start_utc
        is_today = created >= today_start_utc
        if is_today:
            total_today += cost
        if row.get("ingestion_id"):
            ingestions.add(str(row["ingestion_id"]))
            document_cost_month += cost
        user_key = str(row.get("user_id") or "system")
        user_group = users.setdefault(user_key, {
            "user_id": user_key, "calls": 0, "cost_month_usd": 0.0,
            "input_tokens": 0, "output_tokens": 0, "pages": 0,
        })
        user_group["calls"] += 1
        user_group["cost_month_usd"] += cost
        user_group["input_tokens"] += int(row.get("input_tokens") or 0)
        user_group["output_tokens"] += int(row.get("output_tokens") or 0)
        user_group["pages"] += int(row.get("pages") or 0)
        key = (str(row.get("operation") or "unknown"), str(row.get("model") or "unknown"))
        group = groups.setdefault(key, {
            "operation": key[0], "model": key[1], "calls": 0,
            "calls_today": 0, "input_tokens": 0, "output_tokens": 0,
            "input_tokens_today": 0, "output_tokens_today": 0,
            "pages": 0, "pages_today": 0, "retries": 0, "cost_month_usd": 0.0,
            "cost_today_usd": 0.0, "invalid_responses": 0,
        })
        group["calls"] += 1
        group["calls_today"] += int(is_today)
        group["input_tokens"] += int(row.get("input_tokens") or 0)
        group["output_tokens"] += int(row.get("output_tokens") or 0)
        group["pages"] += int(row.get("pages") or 0)
        if is_today:
            group["input_tokens_today"] += int(row.get("input_tokens") or 0)
            group["output_tokens_today"] += int(row.get("output_tokens") or 0)
            group["pages_today"] += int(row.get("pages") or 0)
        group["retries"] += int(row.get("retries") or 0)
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        group["invalid_responses"] += int(metadata.get("outcome") == "parse_error")
        group["cost_month_usd"] += cost
        group["cost_today_usd"] += cost if is_today else 0
        if len(latest) < 8:
            latest.append({
                "operation": key[0], "model": key[1], "cost_usd": round(cost, 8),
                "created_at": created.astimezone(local_zone).isoformat(timespec="seconds"),
                "ingestion_id": row.get("ingestion_id"),
                "input_tokens": int(row.get("input_tokens") or 0),
                "output_tokens": int(row.get("output_tokens") or 0),
                "pages": int(row.get("pages") or 0),
                "outcome": metadata.get("outcome", "success"),
            })

    breakdown = sorted(groups.values(), key=lambda item: item["cost_month_usd"], reverse=True)
    for group in breakdown:
        group["cost_month_usd"] = round(group["cost_month_usd"], 8)
        group["cost_today_usd"] = round(group["cost_today_usd"], 8)
        group["average_cost_usd"] = round(
            group["cost_month_usd"] / group["calls"], 8
        )
    by_user = sorted(users.values(), key=lambda item: item["cost_month_usd"], reverse=True)
    for item in by_user:
        item["cost_month_usd"] = round(item["cost_month_usd"], 8)

    fixed_monthly = round(
        settings.HOSTING_MONTHLY_COST_USD
        + settings.SUPABASE_MONTHLY_COST_USD
        + settings.OTHER_MONTHLY_COST_USD,
        4,
    )
    days_in_month = calendar.monthrange(now_local.year, now_local.month)[1]
    elapsed_fraction = min(1.0, (
        (now_local - month_start_local).total_seconds() / (days_in_month * 86400)
    ))
    fixed_accrued = round(fixed_monthly * elapsed_fraction, 6)
    # Evita una proyección absurda durante las primeras horas del día 1.
    projection_fraction = max(elapsed_fraction, 1 / days_in_month)
    ai_run_rate_projection = (
        round(total_month / projection_fraction, 8) if total_month else 0.0
    )
    projected_committed = round(total_month + fixed_monthly, 8)
    total_budget = settings.MONTHLY_TOTAL_BUDGET_USD
    return {
        "as_of": now_local.isoformat(timespec="seconds"),
        "currency": "USD",
        "ai_today_usd": round(total_today, 8),
        "ai_month_usd": round(total_month, 8),
        "ai_budget_usd": settings.MONTHLY_AI_BUDGET_USD,
        "budget_pct": round(total_month / settings.MONTHLY_AI_BUDGET_USD * 100, 2),
        "fixed_monthly_usd": fixed_monthly,
        "fixed_accrued_usd": fixed_accrued,
        "total_accrued_usd": round(total_month + fixed_accrued, 8),
        "projected_committed_usd": projected_committed,
        "ai_run_rate_projection_usd": ai_run_rate_projection,
        "total_run_rate_projection_usd": round(ai_run_rate_projection + fixed_monthly, 8),
        "total_budget_usd": total_budget,
        "total_budget_pct": (
            round(projected_committed / total_budget * 100, 2) if total_budget else None
        ),
        "documents_with_usage": len(ingestions),
        "average_ai_per_document_usd": (
            round(document_cost_month / len(ingestions), 8) if ingestions else 0.0
        ),
        "breakdown": breakdown,
        "by_user": by_user,
        "latest": latest,
        "unpersisted_events": len(pending_rows),
        "fixed": {
            "hosting": settings.HOSTING_MONTHLY_COST_USD,
            "supabase": settings.SUPABASE_MONTHLY_COST_USD,
            "other": settings.OTHER_MONTHLY_COST_USD,
        },
    }


async def actualizar_job(job_id: str, **campos: Any) -> dict:
    from datetime import datetime, timezone
    client = await get_client()
    campos["actualizado_en"] = datetime.now(timezone.utc).isoformat()
    if "intentos" in campos:
        res_actual = (
            await client.table("jobs").select("intentos").eq("id", job_id).limit(1).execute()
        )
        actual_data = _safe_data(res_actual, many=True)
        actual = (actual_data[0].get("intentos") if actual_data else 0) or 0
        campos["intentos"] = actual + 1
    res = await client.table("jobs").update(campos).eq("id", job_id).execute()
    data = _safe_data(res, many=True)
    return data[0] if data else {}


async def contar_jobs_por_estado() -> dict[str, int]:
    client = await get_client()
    res = await client.table("jobs").select("estado").execute()
    conteo: dict[str, int] = {"pendiente": 0, "procesando": 0, "completado": 0, "error": 0}
    for row in _safe_data(res, many=True):
        estado = row.get("estado", "")
        if estado in conteo:
            conteo[estado] += 1
    return conteo


async def reintentar_ingestion_fallida(ingestion_id: str, telegram_user_id: int) -> bool:
    """Reactiva una ingesta fallida conservando original y trazabilidad."""
    from .config import settings
    if telegram_user_id not in settings.allowed_users:
        return False
    client = await get_client()
    try:
        response = await client.rpc("retry_ingestion_v1", {
            "p_ingestion_id": ingestion_id,
            "p_actor_id": str(telegram_user_id),
        }).execute()
    except Exception as exc:
        if str(getattr(exc, "code", "")) in {"40001", "P0002"}:
            logger.info("La ingesta %s ya no admite reintento", ingestion_id)
            return False
        raise
    return bool(_safe_data(response))


async def metricas_operativas() -> dict[str, Any]:
    """Resumen acotado sin exponer contenido de documentos."""
    from datetime import datetime, timezone
    client = await get_client()
    rows = _safe_data(
        await client.table("ingestions").select("status,received_at").limit(10_000).execute(), many=True
    )
    states: dict[str, int] = {}
    for row in rows:
        state = str(row.get("status") or "unknown")
        states[state] = states.get(state, 0) + 1
    review_ages: list[float] = []
    now = datetime.now(timezone.utc)
    for row in rows:
        if row.get("status") != "needs_review" or not row.get("received_at"):
            continue
        try:
            received = datetime.fromisoformat(str(row["received_at"]).replace("Z", "+00:00"))
            review_ages.append((now - received).total_seconds() / 3600)
        except ValueError:
            continue
    return {
        "ingestions": states,
        "jobs": await contar_jobs_por_estado(),
        "monthly_ai_cost_usd": await coste_ai_mes_actual(),
        "oldest_review_hours": round(max(review_ages), 1) if review_ages else 0.0,
    }


# ── Catálogo para entrada manual ─────────────────────────────────────────────

async def listar_todos_proveedores() -> list[dict]:
    """Proveedores ordenados por nombre, con su forma de pago habitual.

    La forma de pago viaja con el listado para que la entrada manual no tenga que
    volver a preguntarla en cada albarán de un proveedor ya conocido.
    """
    client = await get_client()
    res = await client.table("proveedores").select(
        "id, nombre, forma_pago_habitual"
    ).order("nombre").execute()
    return _safe_data(res, many=True)
