"""
Cola asyncio de procesamiento de albaranes.
Un único worker procesa los jobs en orden de llegada.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from . import supabase_client as db
from . import manual_albaran
from .albaran_processor import ResultadoProcesamiento, procesar_albaran

if TYPE_CHECKING:
    from telegram import Bot

logger = logging.getLogger(__name__)

_queue: asyncio.Queue[tuple[str, bytes, int, "Bot", str | None]] = asyncio.Queue()
_stats: dict[str, int] = {"procesados": 0, "correctos": 0, "revision": 0, "errores": 0}
_total_importe: float = 0.0
_fecha_min: str | None = None
_fecha_max: str | None = None
_pending_confirmations: dict[int, dict] = {}
_TIMEOUT_CONFIRMACION = timedelta(minutes=10)
_stats_lock = asyncio.Lock()


async def encolar_job(
    job_id: str, imagen_bytes: bytes, chat_id: int, bot: "Bot", imagen_hash: str | None = None
) -> int:
    """Encola un job y retorna la posición en cola (1 = procesando de inmediato)."""
    await _queue.put((job_id, imagen_bytes, chat_id, bot, imagen_hash))
    return _queue.qsize()


def _formatear_fecha(fecha_iso: str) -> str:
    """Convierte '2026-05-04' en '4 de mayo de 2026'."""
    meses = ["enero","febrero","marzo","abril","mayo","junio",
             "julio","agosto","septiembre","octubre","noviembre","diciembre"]
    try:
        from datetime import date as _date
        d = _date.fromisoformat(fecha_iso)
        return f"{d.day} de {meses[d.month - 1]} de {d.year}"
    except Exception:
        return fecha_iso


def _formatear_importe(valor: float) -> str:
    """1234.56 → '1.234,56€'"""
    return f"{valor:,.2f}€".replace(",", "X").replace(".", ",").replace("X", ".")


def _teclado_correccion_proveedor(resultado: ResultadoProcesamiento) -> InlineKeyboardMarkup:
    """Botón inline para corregir el proveedor de un albarán procesado."""
    albaran_short = resultado.albaran_id[:8]
    proveedor_short = resultado.proveedor_id[:8]
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🔧 Corregir proveedor",
            callback_data=f"corr_prov:{albaran_short}:{proveedor_short}",
        )
    ]])


def _formatear_respuesta(resultado: ResultadoProcesamiento) -> tuple[str, InlineKeyboardMarkup | None]:
    if resultado.es_duplicado:
        return (
            "Este albarán ya está registrado.\n"
            "Mismo proveedor, fecha y total que un albarán procesado anteriormente.",
            None,
        )

    total_str = _formatear_importe(resultado.total) if resultado.total else "—"
    fecha_str = _formatear_fecha(resultado.fecha) if resultado.fecha else "—"

    partes_cabecera = []
    if resultado.numero_albaran:
        partes_cabecera.append(f"Nº {resultado.numero_albaran}")
    partes_cabecera.append(fecha_str)
    if resultado.forma_pago:
        partes_cabecera.append(resultado.forma_pago)

    lineas = [
        f"✓ Albarán procesado — {resultado.proveedor_nombre}",
        " | ".join(partes_cabecera),
        f"{resultado.num_lineas} productos | Total: {total_str}",
    ]

    if resultado.es_proveedor_nuevo:
        lineas.append("Proveedor nuevo registrado en el sistema.")
    if resultado.lineas_con_revision > 0:
        lineas.append(f"{resultado.lineas_con_revision} línea(s) con datos incompletos.")
    for alerta in resultado.alertas_precio:
        anterior_str = _formatear_importe(alerta['anterior'])
        nuevo_str = _formatear_importe(alerta['nuevo'])
        lineas.append(
            f"Subida de precio: {alerta['producto']} +{alerta['pct']}% "
            f"({anterior_str} → {nuevo_str})"
        )

    return "\n".join(lineas), _teclado_correccion_proveedor(resultado)


def _formatear_cantidad(valor: float | None) -> str:
    """36.0 → '36' ; 5.74 → '5,74' (sin ceros sobrantes, con coma decimal)."""
    if valor is None:
        return "—"
    texto = f"{valor:.3f}".rstrip("0").rstrip(".")
    return texto.replace(".", ",")


def _formatear_confirmacion(resultado: ResultadoProcesamiento) -> tuple[str, InlineKeyboardMarkup]:
    """Mensaje con el resultado normal + solicitud de confirmación en lenguaje sencillo."""
    partes_cabecera = []
    if resultado.numero_albaran:
        partes_cabecera.append(f"Nº {resultado.numero_albaran}")
    fecha_str = _formatear_fecha(resultado.fecha) if resultado.fecha else "—"
    partes_cabecera.append(fecha_str)
    if resultado.forma_pago:
        partes_cabecera.append(resultado.forma_pago)

    total_str = _formatear_importe(resultado.total) if resultado.total else "—"
    n = len(resultado.lineas_para_confirmacion)
    encabezado_revision = (
        "Hay 1 producto que quiero que revises:" if n == 1
        else f"Hay {n} productos que quiero que revises:"
    )
    lineas = [
        f"✓ Albarán procesado — {resultado.proveedor_nombre}",
        " | ".join(partes_cabecera),
        f"{resultado.num_lineas} productos | Total: {total_str}",
        "",
        encabezado_revision,
    ]
    for item in resultado.lineas_para_confirmacion:
        unidad = item.get("unidad") or "ud"
        cant = _formatear_cantidad(item.get("cantidad"))
        lineas.append("")
        lineas.append(f"  {item['num']})  {item['descripcion']}")
        if item.get("precio") and item.get("importe"):
            lineas.append(
                f"      {cant} {unidad} a {_formatear_importe(item['precio'])}/{unidad}"
                f" = {_formatear_importe(item['importe'])}"
            )
        elif item.get("importe"):
            lineas.append(f"      {cant} {unidad} = {_formatear_importe(item['importe'])}")
        else:
            lineas.append(f"      {cant} {unidad}")
        if item.get("razon"):
            lineas.append(f"      ({item['razon']})")

    primer = resultado.lineas_para_confirmacion[0]["num"] if n else 1
    lineas.extend([
        "",
        f'Si algo está mal, contéstame copiando una de estas frases y cambiando solo el dato'
        f' (el "{primer}" es el número del producto):',
        "",
        f"  El precio del {primer} es 4,84",
        f"  El importe del {primer} es 27,76",
        f"  La cantidad del {primer} es 5,74",
        f"  El nombre del {primer} es Longaniza Blanca",
        "",
        'Si está todo bien, contéstame:  ok',
    ])
    return "\n".join(lineas), _teclado_correccion_proveedor(resultado)


def _formatear_resumen_cola() -> str:
    global _fecha_min, _fecha_max
    total_str = _formatear_importe(_total_importe)
    lineas = [
        f"Carga completada: {_stats['procesados']} albaranes procesados",
        f"{_stats['correctos']} correctos | {_stats['revision']} requieren revisión",
        f"Total registrado: {total_str}",
    ]
    if _fecha_min and _fecha_max:
        fecha_min_str = _formatear_fecha(_fecha_min)
        fecha_max_str = _formatear_fecha(_fecha_max)
        lineas.append(f"Período: {fecha_min_str} — {fecha_max_str}")
    return "\n".join(lineas)


async def _actualizar_stats(resultado: ResultadoProcesamiento) -> None:
    global _total_importe, _fecha_min, _fecha_max
    async with _stats_lock:
        _stats["procesados"] += 1
        if resultado.lineas_con_revision > 0:
            _stats["revision"] += 1
        else:
            _stats["correctos"] += 1
        if resultado.total:
            _total_importe += resultado.total
        if resultado.fecha:
            if _fecha_min is None or resultado.fecha < _fecha_min:
                _fecha_min = resultado.fecha
            if _fecha_max is None or resultado.fecha > _fecha_max:
                _fecha_max = resultado.fecha


async def _resetear_stats() -> None:
    global _total_importe, _fecha_min, _fecha_max
    async with _stats_lock:
        _stats.update({"procesados": 0, "correctos": 0, "revision": 0, "errores": 0})
        _total_importe = 0.0
        _fecha_min = None
        _fecha_max = None


# Contador de workers activos (procesando un job en este momento)
_workers_activos: int = 0
_workers_activos_lock = asyncio.Lock()


async def worker() -> None:
    """Worker individual. Lanzar varios con start_workers(n)."""
    global _workers_activos
    logger.info("Worker de cola iniciado")
    while True:
        job_id, imagen_bytes, chat_id, bot, imagen_hash = await _queue.get()

        async with _workers_activos_lock:
            _workers_activos += 1

        try:
            progress_msg_id: list[int | None] = [None]

            async def progress(msg: str) -> None:
                try:
                    if progress_msg_id[0] is not None:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=progress_msg_id[0],
                            text=msg,
                        )
                    else:
                        sent = await bot.send_message(chat_id=chat_id, text=msg)
                        progress_msg_id[0] = sent.message_id
                except Exception:
                    pass

            resultado = await procesar_albaran(job_id, imagen_bytes, chat_id, progress, imagen_hash=imagen_hash)
            await _actualizar_stats(resultado)

            if resultado.lineas_para_confirmacion:
                _pending_confirmations[chat_id] = {
                    "albaran_id": resultado.albaran_id,
                    "lineas": resultado.lineas_para_confirmacion,
                    "timestamp": datetime.now(),
                }
                respuesta, markup = _formatear_confirmacion(resultado)
            else:
                respuesta, markup = _formatear_respuesta(resultado)
                pendientes = _queue.qsize()
                if pendientes > 0:
                    async with _stats_lock:
                        procesados_total = _stats["procesados"]
                    total_lote = procesados_total + pendientes
                    respuesta += f"\n✅ {procesados_total} de {total_lote} albaranes procesados"

            # Eliminar el mensaje de progreso y enviar la respuesta final
            if progress_msg_id[0] is not None:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=progress_msg_id[0])
                except Exception:
                    pass
            await bot.send_message(chat_id=chat_id, text=respuesta, reply_markup=markup)

        except Exception as e:
            async with _stats_lock:
                _stats["errores"] += 1
            logger.error("Error procesando job %s: %s", job_id, e, exc_info=True)
            try:
                if progress_msg_id[0] is not None:
                    await bot.delete_message(chat_id=chat_id, message_id=progress_msg_id[0])
            except Exception:
                pass
            try:
                error_str = str(e)
                es_blacklist = "no es un albarán de compra" in error_str or "no se registrará" in error_str
                if es_blacklist:
                    # Documento que no es un albarán (nómina, factura de luz...): no ofrecer alta manual.
                    await bot.send_message(chat_id=chat_id, text=error_str)
                else:
                    # No se pudo leer (manuscrito, foto difícil): ofrecer meterlo a mano reaprovechando la foto.
                    manual_albaran.recordar_foto_fallida(chat_id, imagen_bytes)
                    markup = InlineKeyboardMarkup([[
                        InlineKeyboardButton("✍️ Introducir a mano", callback_data="manual_start")
                    ]])
                    await bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "No he podido leer este albarán automáticamente "
                            "(puede ser manuscrito o la foto es difícil de interpretar).\n\n"
                            "¿Quieres introducirlo a mano? Es rápido: yo te guío paso a paso y "
                            "uso esta misma foto como archivo.\n\n"
                            "Pulsa el botón o escribe /manual."
                        ),
                        reply_markup=markup,
                    )
            except Exception:
                pass
        finally:
            async with _workers_activos_lock:
                _workers_activos -= 1
                workers_libres = _workers_activos == 0

            _queue.task_done()

            # Enviar resumen cuando la cola está vacía y todos los workers terminaron
            if _queue.empty() and workers_libres:
                async with _stats_lock:
                    procesados = _stats["procesados"]
                if procesados > 1:
                    try:
                        await bot.send_message(chat_id=chat_id, text=_formatear_resumen_cola())
                    except Exception:
                        pass
                await _resetear_stats()


async def start_workers(n: int = 3) -> None:
    """Lanza un pool de N workers concurrentes."""
    logger.info("Iniciando pool de %d workers", n)
    await asyncio.gather(*[worker() for _ in range(n)])


async def recuperar_jobs_pendientes(bot: "Bot") -> int:
    """
    Al arrancar, re-encola jobs que quedaron en estado pendiente o procesando
    (por un reinicio del bot). Retorna el número de jobs recuperados.
    """
    jobs = await db.listar_jobs_pendientes()
    recuperados = 0
    for job in jobs:
        imagen_url = job.get("imagen_url")
        chat_id = job.get("telegram_user_id")
        if not chat_id:
            continue
        if imagen_url:
            try:
                import httpx
                async with httpx.AsyncClient() as http:
                    resp = await http.get(imagen_url, timeout=30)
                    if resp.status_code == 200:
                        await _queue.put((job["id"], resp.content, int(chat_id), bot, None))
                        recuperados += 1
                        continue
            except Exception as e:
                logger.warning("No se pudo recuperar imagen del job %s: %s", job["id"], e)
        await db.actualizar_job(job["id"], estado="error", error_detalle="Job no recuperado en reinicio")
    if recuperados:
        logger.info("%d jobs pendientes recuperados en cola", recuperados)
    return recuperados
