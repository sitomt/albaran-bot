from __future__ import annotations

import logging
import re
from typing import Any

from supabase import acreate_client, AsyncClient

from .config import settings

logger = logging.getLogger(__name__)

_client: AsyncClient | None = None


async def get_client() -> AsyncClient:
    global _client
    if _client is None:
        _client = await acreate_client(settings.SUPABASE_URL, settings.SUPABASE_ANON_KEY)
    return _client


def _safe_data(res: Any, many: bool = False) -> Any:
    """Extrae .data de una respuesta supabase-py de forma segura."""
    if res is None:
        return [] if many else None
    data = getattr(res, "data", None)
    if many:
        return data or []
    return data


def _nif_norm(nif: str) -> str:
    """Normaliza NIF: mayúsculas, sin guiones ni espacios."""
    return re.sub(r'[^A-Z0-9]', '', nif.upper().strip())


# ── Proveedores ───────────────────────────────────────────────────────────────

async def buscar_proveedor_por_nif(nif: str) -> dict | None:
    client = await get_client()
    nif_n = _nif_norm(nif)
    res = (
        await client.table("proveedores")
        .select("*")
        .eq("nif_normalizado", nif_n)
        .limit(1)
        .execute()
    )
    data = _safe_data(res, many=True)
    return data[0] if data else None


async def buscar_proveedor_por_nombre(nombre: str) -> dict | None:
    """Busca proveedor por nombre exacto (case-insensitive)."""
    client = await get_client()
    nombre_n = nombre.strip().lower()
    res = (
        await client.table("proveedores")
        .select("*")
        .eq("nombre_normalizado", nombre_n)
        .limit(1)
        .execute()
    )
    data = _safe_data(res, many=True)
    return data[0] if data else None


async def buscar_o_crear_proveedor(
    nombre: str,
    nif: str | None,
    direccion: str | None = None,
    telefono: str | None = None,
    email: str | None = None,
    forma_pago_habitual: str | None = None,
) -> tuple[dict, bool]:
    """
    Devuelve (proveedor, es_nuevo).
    - El NIF almacenado nunca se modifica.
    - Si el proveedor ya existe pero le faltan campos de contacto o forma_pago_habitual, se rellenan.
    """
    from .config import settings

    # Si el NIF extraído pertenece al propio restaurante (destinatario), ignorarlo.
    # Algunos proveedores imprimen el CIF del cliente en el albarán y el LLM lo extrae como proveedor_nif.
    nif_para_busqueda = nif
    if nif and _nif_norm(nif) in settings.customer_nifs_set:
        logger.info("NIF %s es del restaurante — ignorado para búsqueda de proveedor '%s'", nif, nombre)
        nif_para_busqueda = None

    row = None
    if nif_para_busqueda:
        row = await buscar_proveedor_por_nif(nif_para_busqueda)
    if row is None:
        row = await buscar_proveedor_por_nombre(nombre)

    if row:
        updates = {
            k: v for k, v in [
                ("direccion", direccion),
                ("telefono", telefono),
                ("email", email),
                ("forma_pago_habitual", forma_pago_habitual),
            ]
            if v and not row.get(k)
        }
        # Backfill de NIF: si el proveedor se creó con un NIF placeholder (porque el primer
        # albarán traía el NIF del cliente o ninguno) y ahora llega un NIF real (no del
        # cliente), se rellena. Solo se sobreescribe el placeholder, nunca un NIF real.
        if (
            nif_para_busqueda
            and str(row.get("nif", "")).startswith("DESCONOCIDO")
            and not str(nif_para_busqueda).startswith("DESCONOCIDO")
        ):
            updates["nif"] = nif_para_busqueda
        if updates:
            client = await get_client()
            res = await client.table("proveedores").update(updates).eq("id", row["id"]).execute()
            data = _safe_data(res, many=True)
            if data:
                row = data[0]
        return row, False

    nuevo = await insertar_proveedor(
        nombre=nombre, nif=nif_para_busqueda, direccion=direccion,
        telefono=telefono, email=email, forma_pago_habitual=forma_pago_habitual,
    )
    return nuevo, True


async def insertar_proveedor(
    nombre: str,
    nif: str | None,
    direccion: str | None = None,
    telefono: str | None = None,
    email: str | None = None,
    forma_pago_habitual: str | None = None,
) -> dict:
    client = await get_client()
    nif_final = nif or f"DESCONOCIDO-{__import__('uuid').uuid4().hex[:8].upper()}"
    payload = {
        "nombre": nombre,
        "nif": nif_final,
        "direccion": direccion,
        "telefono": telefono,
        "email": email,
        "forma_pago_habitual": forma_pago_habitual,
    }
    res = await client.table("proveedores").insert(payload).execute()
    data = _safe_data(res, many=True)
    logger.info("Proveedor insertado: %s (NIF: %s)", nombre, nif_final)
    return data[0]


# ── Productos catálogo ────────────────────────────────────────────────────────

async def buscar_productos_por_proveedor(proveedor_id: str) -> list[dict]:
    client = await get_client()
    res = (
        await client.table("productos_catalogo")
        .select("id, nombre_normalizado, variantes, precio_ultima_compra")
        .eq("proveedor_id", proveedor_id)
        .execute()
    )
    return _safe_data(res, many=True)


async def buscar_o_crear_producto_catalogo(
    proveedor_id: str,
    nombre_normalizado: str,
    unidad_base: str | None = None,
    formato_habitual: str | None = None,
) -> dict:
    client = await get_client()
    payload = {
        "proveedor_id": proveedor_id,
        "nombre_normalizado": nombre_normalizado,
        "unidad_base": unidad_base,
        "formato_habitual": formato_habitual,
    }
    res = (
        await client.table("productos_catalogo")
        .upsert(payload, on_conflict="nombre_normalizado,proveedor_id")
        .execute()
    )
    data = _safe_data(res, many=True)
    if not data:
        # upsert devolvió vacío (ya existía y no hubo cambio) → buscar manualmente
        res2 = (
            await client.table("productos_catalogo")
            .select("*")
            .eq("proveedor_id", proveedor_id)
            .eq("nombre_normalizado", nombre_normalizado)
            .limit(1)
            .execute()
        )
        data2 = _safe_data(res2, many=True)
        if not data2:
            raise RuntimeError(f"No se pudo crear/encontrar producto: {nombre_normalizado}")
        return data2[0]
    return data[0]


async def actualizar_variantes_producto(producto_id: str, nueva_variante: str) -> None:
    client = await get_client()
    res = (
        await client.table("productos_catalogo")
        .select("variantes")
        .eq("id", producto_id)
        .limit(1)
        .execute()
    )
    data = _safe_data(res, many=True)
    variantes: list = (data[0].get("variantes") if data else None) or []
    if nueva_variante not in variantes:
        variantes.append(nueva_variante)
        await client.table("productos_catalogo").update({"variantes": variantes}).eq("id", producto_id).execute()


async def actualizar_precio_catalogo(producto_id: str, nuevo_precio: float) -> tuple[float | None, bool]:
    """Actualiza precios y retorna (precio_anterior, alerta_subida_>10%)."""
    client = await get_client()
    res = (
        await client.table("productos_catalogo")
        .select("precio_ultima_compra, precio_medio_historico")
        .eq("id", producto_id)
        .limit(1)
        .execute()
    )
    data = _safe_data(res, many=True)
    row = data[0] if data else {}
    anterior = row.get("precio_ultima_compra")
    medio = row.get("precio_medio_historico")

    alerta = anterior is not None and float(anterior) > 0 and (nuevo_precio - float(anterior)) / float(anterior) > 0.10

    nuevo_medio = nuevo_precio if medio is None else (float(medio) + nuevo_precio) / 2

    from datetime import date as _date
    await client.table("productos_catalogo").update({
        "precio_ultima_compra": nuevo_precio,
        "precio_medio_historico": nuevo_medio,
        "ultima_compra_fecha": _date.today().isoformat(),
    }).eq("id", producto_id).execute()

    return anterior, alerta


# ── Albaranes ─────────────────────────────────────────────────────────────────

async def buscar_albaran_duplicado(numero_albaran: str, proveedor_id: str) -> dict | None:
    if not numero_albaran:
        return None
    client = await get_client()
    res = (
        await client.table("albaranes")
        .select("id, numero_albaran, fecha, creado_en")
        .eq("numero_albaran", numero_albaran)
        .eq("proveedor_id", proveedor_id)
        .limit(1)
        .execute()
    )
    data = _safe_data(res, many=True)
    return data[0] if data else None


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


async def buscar_albaran_duplicado_combinacion(
    proveedor_id: str, fecha: str, total: float
) -> dict | None:
    """Duplicado por proveedor_id + fecha + total (±0.50€)."""
    client = await get_client()
    res = (
        await client.table("albaranes")
        .select("id, numero_albaran, fecha, creado_en, total")
        .eq("proveedor_id", proveedor_id)
        .eq("fecha", fecha)
        .gte("total", total - 0.50)
        .lte("total", total + 0.50)
        .limit(1)
        .execute()
    )
    data = _safe_data(res, many=True)
    return data[0] if data else None


async def buscar_albaran_duplicado_por_nombre_proveedor(
    nombre_proveedor: str, fecha: str, total: float
) -> dict | None:
    """
    Duplicado por nombre de proveedor + fecha + total (±0.50€).
    Usa SQL directo vía RPC para hacer el JOIN aunque el proveedor_id sea distinto.
    """
    try:
        sql = (
            f"SELECT a.id, a.numero_albaran, a.fecha, a.creado_en, a.total"
            f" FROM albaranes a"
            f" JOIN proveedores p ON a.proveedor_id = p.id"
            f" WHERE LOWER(p.nombre) = LOWER('{nombre_proveedor.replace(chr(39), chr(39)*2)}')"
            f" AND a.fecha = '{fecha}'"
            f" AND a.total BETWEEN {total - 0.50} AND {total + 0.50}"
            f" LIMIT 1"
        )
        client = await get_client()
        res = await client.rpc("execute_select", {"query": sql}).execute()
        data = _safe_data(res)
        if isinstance(data, list) and data:
            return data[0]
        return None
    except Exception as e:
        logger.warning("buscar_albaran_duplicado_por_nombre_proveedor falló: %s", e)
        return None


async def buscar_albaran_por_hash(imagen_hash: str) -> dict | None:
    """Busca albarán por hash SHA-256 de la imagen. Detecta reenvío de la misma foto."""
    client = await get_client()
    res = (
        await client.table("albaranes")
        .select("id, numero_albaran, fecha, creado_en, total")
        .eq("imagen_hash", imagen_hash)
        .limit(1)
        .execute()
    )
    data = _safe_data(res, many=True)
    return data[0] if data else None


async def insertar_albaran(
    proveedor_id: str,
    numero_albaran: str | None,
    fecha: str,
    forma_pago: str | None,
    base_imponible: float | None,
    total_iva: float | None,
    total: float | None,
    imagen_url: str | None,
    detalle_iva: list[dict] | None = None,
    imagen_hash: str | None = None,
    origen: str = "ocr",
) -> dict:
    client = await get_client()
    payload = {
        "proveedor_id": proveedor_id,
        "numero_albaran": numero_albaran,
        "fecha": fecha,
        "forma_pago": forma_pago,
        "base_imponible": base_imponible,
        "total_iva": total_iva,
        "total": total,
        "imagen_url": imagen_url,
        "detalle_iva": detalle_iva,
        "imagen_hash": imagen_hash,
        "origen": origen,
    }
    res = await client.table("albaranes").insert(payload).execute()
    data = _safe_data(res, many=True)
    logger.info("Albarán insertado: Nº %s proveedor=%s total=%s", numero_albaran, proveedor_id, total)
    return data[0]


async def insertar_lineas(lineas: list[dict]) -> list[dict]:
    if not lineas:
        return []
    client = await get_client()
    res = await client.table("lineas_albaran").insert(lineas).execute()
    return _safe_data(res, many=True)


async def actualizar_campo_albaran(albaran_id: str, **campos: Any) -> dict:
    client = await get_client()
    res = await client.table("albaranes").update(campos).eq("id", albaran_id).execute()
    data = _safe_data(res, many=True)
    return data[0] if data else {}


async def obtener_resumen_semana(fecha_inicio: str, fecha_fin: str) -> list[dict]:
    client = await get_client()
    res = (
        await client.table("albaranes")
        .select("id, fecha, total, proveedor_id, proveedores(nombre)")
        .gte("fecha", fecha_inicio)
        .lte("fecha", fecha_fin)
        .order("fecha", desc=True)
        .execute()
    )
    return _safe_data(res, many=True)


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

async def subir_imagen(bucket: str, path: str, data: bytes, content_type: str = "image/jpeg") -> str:
    """Sube imagen a Supabase Storage y retorna la URL pública."""
    client = await get_client()
    await client.storage.from_(bucket).upload(
        path=path,
        file=data,
        file_options={"content-type": content_type, "upsert": "true"},
    )
    return await client.storage.from_(bucket).get_public_url(path)


async def listar_archivos_storage(bucket: str, prefix: str = "") -> list[str]:
    """
    Lista recursivamente todas las rutas de fichero dentro del bucket.
    Las carpetas (item['id'] is None) se recorren; los ficheros se acumulan.
    """
    client = await get_client()
    items = await client.storage.from_(bucket).list(prefix)
    paths: list[str] = []
    for it in items or []:
        nombre = it.get("name")
        if not nombre:
            continue
        ruta = f"{prefix}/{nombre}" if prefix else nombre
        if it.get("id") is None:  # es carpeta
            paths.extend(await listar_archivos_storage(bucket, ruta))
        else:
            paths.append(ruta)
    return paths


async def borrar_archivos_storage(bucket: str, paths: list[str]) -> int:
    """Borra las rutas indicadas del bucket. Retorna cuántas se pidieron borrar."""
    if not paths:
        return 0
    client = await get_client()
    await client.storage.from_(bucket).remove(paths)
    return len(paths)


# ── Jobs ──────────────────────────────────────────────────────────────────────

async def crear_job(telegram_user_id: int, imagen_url: str | None = None) -> dict:
    client = await get_client()
    payload = {
        "telegram_user_id": telegram_user_id,
        "imagen_url": imagen_url,
        "estado": "pendiente",
        "intentos": 0,
    }
    res = await client.table("jobs").insert(payload).execute()
    data = _safe_data(res, many=True)
    return data[0]


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


async def listar_jobs_pendientes() -> list[dict]:
    client = await get_client()
    res = (
        await client.table("jobs")
        .select("*")
        .in_("estado", ["pendiente", "procesando"])
        .order("creado_en")
        .execute()
    )
    return _safe_data(res, many=True)


async def limpiar_jobs_antiguos(dias: int = 7) -> int:
    """Borra jobs completados o con error con más de `dias` días. Retorna cuántos se borraron."""
    from datetime import datetime, timezone, timedelta
    client = await get_client()
    corte = (datetime.now(timezone.utc) - timedelta(days=dias)).isoformat()
    res = (
        await client.table("jobs")
        .delete()
        .in_("estado", ["completado", "error"])
        .lt("creado_en", corte)
        .execute()
    )
    data = _safe_data(res, many=True)
    return len(data) if data else 0


async def contar_jobs_por_estado() -> dict[str, int]:
    client = await get_client()
    res = await client.table("jobs").select("estado").execute()
    conteo: dict[str, int] = {"pendiente": 0, "procesando": 0, "completado": 0, "error": 0}
    for row in _safe_data(res, many=True):
        estado = row.get("estado", "")
        if estado in conteo:
            conteo[estado] += 1
    return conteo


# ── Auditoría ─────────────────────────────────────────────────────────────────

async def registrar_auditoria(
    tipo: str,
    resultado: str,
    telegram_user_id: int | None = None,
    imagen_url: str | None = None,
    modelo_ocr: str | None = None,
    modelo_llm: str | None = None,
    tokens_consumidos: int | None = None,
    coste_estimado_usd: float | None = None,
    detalle: dict | None = None,
    albaran_id: str | None = None,
) -> None:
    try:
        client = await get_client()
        payload = {
            "tipo": tipo,
            "resultado": resultado,
            "telegram_user_id": telegram_user_id,
            "imagen_url": imagen_url,
            "modelo_ocr": modelo_ocr,
            "modelo_llm": modelo_llm,
            "tokens_consumidos": tokens_consumidos,
            "coste_estimado_usd": coste_estimado_usd,
            "detalle": detalle,
            "albaran_id": albaran_id,
        }
        await client.table("auditoria").insert(payload).execute()
    except Exception as e:
        logger.warning("No se pudo registrar auditoría: %s", e)


# ── Revisiones y correcciones ─────────────────────────────────────────────────

async def listar_lineas_pendientes_revision() -> list[dict]:
    """Retorna líneas marcadas como requiere_revision=true."""
    client = await get_client()
    res = await (
        client.table("lineas_albaran")
        .select("id, descripcion_limpia, cantidad, precio_unitario, unidad, albaran_id, albaranes(fecha, numero_albaran, proveedores(nombre))")
        .eq("requiere_revision", True)
        .order("albaran_id")
        .execute()
    )
    return _safe_data(res, many=True)


async def registrar_correccion(
    linea_id: str,
    campo: str,
    valor_original: str | None,
    valor_corregido: str,
    corregido_por: str = "usuario",
) -> None:
    client = await get_client()
    payload = {
        "linea_albaran_id": linea_id,
        "campo": campo,
        "valor_original": valor_original,
        "valor_corregido": valor_corregido,
        "corregido_por": corregido_por,
    }
    await client.table("correcciones").insert(payload).execute()


async def actualizar_linea_albaran(linea_id: str, **campos) -> dict:
    client = await get_client()
    res = await client.table("lineas_albaran").update(campos).eq("id", linea_id).execute()
    data = _safe_data(res, many=True)
    return data[0] if data else {}


# ── SQL dinámico (para query_engine) ──────────────────────────────────────────

async def ejecutar_sql(sql: str) -> list[dict]:
    client = await get_client()
    res = await client.rpc("execute_select", {"query": sql}).execute()
    data = _safe_data(res)
    if data is None:
        return []
    if isinstance(data, list):
        return data
    # La RPC devuelve JSON como string o como lista según la versión
    if isinstance(data, str):
        import json
        try:
            parsed = json.loads(data)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []


# ── Corrección de proveedor ───────────────────────────────────────────────────

async def buscar_proveedores_similares(texto: str) -> list[dict]:
    """Devuelve proveedores cuyo nombre contenga 'texto' (case-insensitive)."""
    client = await get_client()
    res = await client.table("proveedores").select("id, nombre").ilike("nombre", f"%{texto}%").execute()
    return _safe_data(res, many=True)


async def listar_todos_proveedores() -> list[dict]:
    """Devuelve todos los proveedores ordenados por nombre."""
    client = await get_client()
    res = await client.table("proveedores").select("id, nombre").order("nombre").execute()
    return _safe_data(res, many=True)


async def actualizar_nombre_proveedor(proveedor_id: str, nuevo_nombre: str) -> dict:
    """Renombra un proveedor existente."""
    client = await get_client()
    res = await client.table("proveedores").update({"nombre": nuevo_nombre}).eq("id", proveedor_id).execute()
    data = _safe_data(res, many=True)
    return data[0] if data else {}


async def reasignar_proveedor_albaran(albaran_id: str, nuevo_proveedor_id: str, proveedor_anterior_id: str) -> None:
    """
    Reasigna un albarán a otro proveedor existente.
    Si el proveedor anterior queda sin albaranes, lo elimina.
    """
    client = await get_client()
    await client.table("albaranes").update({"proveedor_id": nuevo_proveedor_id}).eq("id", albaran_id).execute()
    # Limpiar proveedor huérfano
    check = await client.table("albaranes").select("id").eq("proveedor_id", proveedor_anterior_id).limit(1).execute()
    if not _safe_data(check, many=True):
        await client.table("productos_catalogo").delete().eq("proveedor_id", proveedor_anterior_id).execute()
        await client.table("proveedores").delete().eq("id", proveedor_anterior_id).execute()
        logger.info("Proveedor huérfano %s eliminado tras reasignación", proveedor_anterior_id)
    return []
