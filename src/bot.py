"""
Bot de Telegram — Gestor de Compras.
Punto de entrada del sistema. Arranca con: python src/bot.py
"""
from __future__ import annotations

import logging
import io
import re
from datetime import date, time, timedelta

import pytz
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from .config import settings
from . import supabase_client as db
from . import manual_albaran
from .query_engine import consultar
from .queue_manager import start_durable_workers
from .conversation_history import agregar_turno, obtener_historial, limpiar_historial
from .intake_service import receive_image
from .review_service import (
    approve_all,
    build_review_view,
    correct_candidate,
    reject_ingestion,
)

logging.basicConfig(
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
    level=logging.INFO,
)


def _usuario_autorizado(update: Update) -> bool:
    """La configuración ya valida una whitelist no vacía; nunca falla abierto."""
    return bool(update.effective_user and update.effective_user.id in settings.allowed_users)


async def _rechazar(update: Update) -> None:
    await update.message.reply_text("No tienes acceso a este sistema.")
    logger.warning("Acceso denegado a user_id=%s username=%s", update.effective_user.id, update.effective_user.username)
logger = logging.getLogger(__name__)

_ZONA_HORARIA = pytz.timezone("Europe/Madrid")
_last_alert_signature: tuple[str, ...] = ()

_PRESENTACION = """\
Gestor de Compras

Mándame fotos de albaranes para registrarlos. También puedo responder cualquier pregunta sobre gastos, precios y proveedores.

/manual — Registrar un albarán a mano (manuscritos, OCR fallido)
/estado — Cola de procesamiento
/resumen — Resumen de la semana
/proveedores — Proveedores registrados
/ultimos — Últimos albaranes y sus referencias
/detalle — Ver cifras y productos de un albarán
/revisiones — Albaranes pendientes de revisión
/revisar — Revisar un albarán pendiente
/corregir — Corregir un candidato pendiente
/anular — Archivar un albarán confirmado incorrecto
/reintentar — Reprocesar un documento fallido
/metricas — Estado operativo y coste mensual
/costes — Desglose en tiempo real de todos los consumos
/feedback [REFERENCIA] texto — Enviar una observación general o sobre un documento
/ayuda — Ejemplos de consultas
"""

_AYUDA = """\
Preguntas que puedes hacerme:

¿Cuánto me cuesta el tomate?
¿Cuánto llevo gastado este mes con Lucas Caballero?
¿Cuántos kilos de anchoa he comprado este mes?
¿A cómo sale el aceite Frimasol por litro?
Total gastado por proveedor este mes
¿Cuál es la forma de pago de Lucas Caballero?
Últimas 3 compras de queso cremette

Para corregir datos:
/corregir REFERENCIA total 370,38
/corregir REFERENCIA linea 2 cantidad 3
/corregir REFERENCIA linea 2 precio 4,84
/corregir REFERENCIA linea 2 importe 14,52

Usa /ultimos para encontrar referencias y /detalle REFERENCIA para comprobar cifras.

Para enviar feedback:
/feedback El flujo manual tarda demasiado
/feedback REFERENCIA El importe de la línea 2 salió mal
"""


# ── Comandos ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: CallbackContext) -> None:
    if not _usuario_autorizado(update):
        await _rechazar(update)
        return
    limpiar_historial(update.effective_chat.id)
    await update.message.reply_text(_PRESENTACION)


async def cmd_estado(update: Update, context: CallbackContext) -> None:
    if not _usuario_autorizado(update):
        await _rechazar(update)
        return
    try:
        conteo = await db.contar_jobs_por_estado()
        texto = (
            f"Cola de procesamiento:\n"
            f"Procesados: {conteo['completado']} | "
            f"En espera: {conteo['pendiente'] + conteo['procesando']} | "
            f"Con error: {conteo['error']}"
        )
    except Exception as e:
        logger.error("No se pudo obtener el estado: %s", e, exc_info=True)
        texto = "No se pudo obtener el estado en este momento."
    await update.message.reply_text(texto)


async def cmd_resumen(update: Update, context: CallbackContext) -> None:
    if not _usuario_autorizado(update):
        await _rechazar(update)
        return
    hoy = date.today()
    lunes = hoy - timedelta(days=hoy.weekday())
    respuesta = await consultar(
        f"Total gastado por proveedor entre {lunes.strftime('%d/%m/%Y')} y {hoy.strftime('%d/%m/%Y')}, "
        f"ordenado por total descendente", user_id=update.effective_user.id
    )
    await update.message.reply_text(respuesta)


async def cmd_proveedores(update: Update, context: CallbackContext) -> None:
    if not _usuario_autorizado(update):
        await _rechazar(update)
        return
    try:
        proveedores = await db.listar_proveedores()
        if not proveedores:
            await update.message.reply_text("No hay proveedores registrados aún.")
            return
        lineas = ["Proveedores registrados:\n"]
        for p in proveedores:
            linea = p['nombre']
            if p.get("nif"):
                linea += f" ({p['nif']})"
            if p.get("forma_pago_habitual"):
                linea += f" — {p['forma_pago_habitual']}"
            lineas.append(linea)
        await update.message.reply_text("\n".join(lineas))
    except Exception as e:
        logger.error("Error al obtener proveedores: %s", e, exc_info=True)
        await update.message.reply_text("No pude obtener los proveedores en este momento.")


def _money(value: object) -> str:
    if value in (None, ""):
        return "—"
    try:
        return f"{float(value):.2f}".replace(".", ",")
    except (TypeError, ValueError):
        return str(value)


async def cmd_ultimos(update: Update, context: CallbackContext) -> None:
    if not _usuario_autorizado(update):
        await _rechazar(update)
        return
    try:
        rows = await db.listar_albaranes_recientes(10)
        if not rows:
            await update.message.reply_text("Todavía no hay albaranes registrados.")
            return
        lines = ["Últimos albaranes:"]
        for row in rows:
            provider = (row.get("proveedores") or {}).get("nombre") or "Proveedor desconocido"
            status = "archivado" if row.get("status") == "archived" else "confirmado"
            lines.append(
                f"• {str(row['id'])[:8]} — {provider} — {row.get('fecha') or 'sin fecha'} — "
                f"{_money(row.get('total'))}€ ({status})"
            )
        lines.append("\nUsa /detalle REFERENCIA para ver productos y cifras.")
        await update.message.reply_text("\n".join(lines)[:4000])
    except Exception as exc:
        logger.error("No se pudieron listar albaranes: %s", exc, exc_info=True)
        await update.message.reply_text("No pude obtener los últimos albaranes.")


async def cmd_detalle(update: Update, context: CallbackContext) -> None:
    if not _usuario_autorizado(update):
        await _rechazar(update)
        return
    if not context.args:
        await update.message.reply_text("Uso: /detalle REFERENCIA. Encuéntrala con /ultimos.")
        return
    try:
        row = await db.obtener_detalle_albaran(context.args[0])
        if not row:
            await update.message.reply_text("Referencia no encontrada o ambigua.")
            return
        provider = (row.get("proveedores") or {}).get("nombre") or "Proveedor desconocido"
        lines = [
            f"Albarán {str(row['id'])[:8]} — {provider}",
            f"Nº {row.get('numero_albaran') or '—'} | {row.get('fecha') or 'sin fecha'}",
            f"Base {_money(row.get('base_imponible'))}€ + IVA {_money(row.get('total_iva'))}€ "
            f"= TOTAL {_money(row.get('total'))}€",
            f"Estado: {row.get('status') or 'confirmado'} | Origen: {row.get('origen') or '—'}",
            "",
            "Productos:",
        ]
        for index, item in enumerate(row.get("lineas") or [], start=1):
            line_no = item.get("line_no") or index
            lines.append(
                f"{line_no}. {item.get('descripcion_limpia') or 'Sin nombre'} — "
                f"{item.get('cantidad')} {item.get('unidad') or 'ud'} × "
                f"{_money(item.get('precio_unitario'))}€ = {_money(item.get('importe_neto'))}€"
            )
        lines.append(
            f"\nSi algo está mal y aún está pendiente: /corregir {str(row['id'])[:8]} …\n"
            f"Si ya está confirmado: /anular {str(row['id'])[:8]} motivo y vuelve a crearlo con /manual."
        )
        await update.message.reply_text("\n".join(lines)[:4000])
    except Exception as exc:
        logger.error("No se pudo cargar el detalle: %s", exc, exc_info=True)
        await update.message.reply_text("No pude cargar ese albarán.")


async def cmd_ayuda(update: Update, context: CallbackContext) -> None:
    if not _usuario_autorizado(update):
        await _rechazar(update)
        return
    await update.message.reply_text(_AYUDA)


async def cmd_revisiones(update: Update, context: CallbackContext) -> None:
    if not _usuario_autorizado(update):
        await _rechazar(update)
        return
    try:
        pendientes = await db.listar_ingestions_pendientes_revision(update.effective_user.id)
        if not pendientes:
            await update.message.reply_text("No hay albaranes pendientes de revisión.")
            return
        rows = ["Albaranes pendientes de revisión:"]
        for item in pendientes:
            meta = item.get("metadata") or {}
            open_reviews = [
                review for review in (item.get("review_items") or [])
                if review.get("status") == "open"
            ]
            rows.append(
                f"{item['id'][:8]} — {meta.get('provider', '?')} — "
                f"{meta.get('total', '—')}€ — {len(open_reviews)} avisos"
            )
        rows.append("\nUsa /revisar REFERENCIA, por ejemplo /revisar " + pendientes[0]["id"][:8])
        await update.message.reply_text("\n".join(rows))
    except Exception as e:
        logger.error("Error al obtener revisiones: %s", e, exc_info=True)
        await update.message.reply_text("No pude obtener las revisiones en este momento.")


async def cmd_revisar(update: Update, context: CallbackContext) -> None:
    if not _usuario_autorizado(update):
        await _rechazar(update)
        return
    if not context.args:
        await cmd_revisiones(update, context)
        return
    ingestion = await db.buscar_ingestion_por_referencia(
        context.args[0], update.effective_user.id
    )
    if not ingestion:
        await update.message.reply_text("Referencia no encontrada o ambigua.")
        return
    try:
        view = await build_review_view(ingestion["id"], update.effective_user.id)
        await _reply_review_command(update, context, view, ingestion["id"])
    except Exception as exc:
        logger.error("No se pudo abrir la revisión: %s", exc, exc_info=True)
        await update.message.reply_text("No pude abrir esa revisión. Consulta /revisiones.")


async def cmd_editar(update: Update, context: CallbackContext) -> None:
    if not _usuario_autorizado(update):
        await _rechazar(update)
        return
    if len(context.args) < 3:
        await update.message.reply_text(
            "Uso:\n/editar REFERENCIA total 123,45\n"
            "/editar REFERENCIA linea 2 importe 47,25\n"
            "/editar REFERENCIA linea 2 nombre Sardina"
        )
        return
    try:
        view = await correct_candidate(
            context.args[0], update.effective_user.id, context.args[1:]
        )
        await _reply_review_command(update, context, view, view.ingestion_id)
    except Exception as exc:
        logger.error("No se pudo corregir el candidato: %s", exc, exc_info=True)
        await update.message.reply_text("No pude aplicar esa corrección. Revisa el formato con /revisar.")


async def cmd_reintentar(update: Update, context: CallbackContext) -> None:
    if not _usuario_autorizado(update):
        await _rechazar(update)
        return
    if not context.args:
        await update.message.reply_text("Uso: /reintentar REFERENCIA")
        return
    ingestion = await db.buscar_ingestion_por_referencia(context.args[0], update.effective_user.id)
    if not ingestion:
        await update.message.reply_text("Referencia no encontrada o ambigua.")
        return
    if await db.reintentar_ingestion_fallida(ingestion["id"], update.effective_user.id):
        await update.message.reply_text(
            f"Documento puesto de nuevo en cola. Referencia: {ingestion['id'][:8]}"
        )
    else:
        await update.message.reply_text("Ese documento no está fallido o ya se está procesando.")


async def cmd_metricas(update: Update, context: CallbackContext) -> None:
    if not _usuario_autorizado(update):
        await _rechazar(update)
        return
    try:
        metrics = await db.metricas_operativas()
        ing = metrics["ingestions"]
        await update.message.reply_text(
            "Estado operativo\n"
            f"Pendientes: {ing.get('queued', 0) + ing.get('processing', 0)}\n"
            f"Por revisar: {ing.get('needs_review', 0)}\n"
            f"Confirmados: {ing.get('confirmed', 0)}\n"
            f"Fallidos: {ing.get('failed', 0)}\n"
            f"Coste IA del mes: ${metrics['monthly_ai_cost_usd']:.4f} / "
            f"${settings.MONTHLY_AI_BUDGET_USD:.2f}\n"
            "Usa /costes para ver OCR, modelos, tokens y llamadas."
        )
    except Exception as exc:
        logger.error("No se pudieron cargar métricas: %s", exc, exc_info=True)
        await update.message.reply_text("No pude obtener las métricas en este momento.")


_COST_OPERATION_LABELS = {
    "ocr": "OCR",
    "classification": "Clasificación visual",
    "extraction": "Extracción del albarán",
    "query_classification": "Clasificación de consulta",
    "query_response": "Respuesta de consulta",
}


def _format_cost(value: float) -> str:
    decimals = 6 if abs(value) < 0.01 else 4
    return f"${value:.{decimals}f}"


def _format_cost_report(report: dict) -> str:
    lines = [
        "Costes en tiempo real",
        f"Actualizado: {str(report['as_of'])[11:19]} (Madrid)",
        "",
        f"IA hoy: {_format_cost(report['ai_today_usd'])}",
        f"IA este mes: {_format_cost(report['ai_month_usd'])} / "
        f"{_format_cost(report['ai_budget_usd'])} ({report['budget_pct']:.1f}%)",
        f"Fijos devengados: {_format_cost(report['fixed_accrued_usd'])}",
        f"Total devengado estimado: {_format_cost(report['total_accrued_usd'])}",
        f"Consumo actual + fijos del mes: {_format_cost(report['projected_committed_usd'])}",
        f"Proyección al ritmo actual: {_format_cost(report['total_run_rate_projection_usd'])}",
        "",
        "Desglose medido del mes:",
    ]
    if not report["breakdown"]:
        lines.append("• Todavía no hay llamadas registradas.")
    for item in report["breakdown"][:12]:
        label = _COST_OPERATION_LABELS.get(item["operation"], item["operation"])
        usage = []
        if item["pages"]:
            usage.append(f"{item['pages']} pág.")
        if item["input_tokens"] or item["output_tokens"]:
            usage.append(f"{item['input_tokens']:,} in / {item['output_tokens']:,} out tok")
        if item.get("retries"):
            usage.append(f"{item['retries']} llamadas de reintento")
        if item.get("invalid_responses"):
            usage.append(f"{item['invalid_responses']} respuestas inválidas facturadas")
        usage_text = " | " + " | ".join(usage) if usage else ""
        lines.append(
            f"• {label} [{item['model']}]: {_format_cost(item['cost_month_usd'])} "
            f"({item['calls']} llamadas; media {_format_cost(item['average_cost_usd'])}; "
            f"hoy {item['calls_today']}){usage_text}"
        )
    fixed = report["fixed"]
    if report.get("by_user"):
        lines.extend(["", "Por usuario este mes:"])
        for item in report["by_user"]:
            label = "Sistema" if item["user_id"] == "system" else f"Telegram {item['user_id']}"
            lines.append(
                f"• {label}: {_format_cost(item['cost_month_usd'])} "
                f"({item['calls']} llamadas)"
            )
    lines.extend([
        "",
        "Costes fijos configurados:",
        f"• Hosting: {_format_cost(fixed['hosting'])}/mes",
        f"• Supabase: {_format_cost(fixed['supabase'])}/mes",
        f"• Otros: {_format_cost(fixed['other'])}/mes",
        "",
        f"Documentos con consumo: {report['documents_with_usage']} | "
        f"Media IA/documento: {_format_cost(report['average_ai_per_document_usd'])}",
    ])
    if report["latest"]:
        lines.extend(["", "Últimas llamadas:"])
        for item in report["latest"][:5]:
            label = _COST_OPERATION_LABELS.get(item["operation"], item["operation"])
            reference = (
                f" doc {str(item['ingestion_id'])[:8]}" if item.get("ingestion_id") else ""
            )
            lines.append(
                f"• {str(item['created_at'])[11:19]} {label}{reference}: "
                f"{_format_cost(item['cost_usd'])}"
            )
    if report["fixed_monthly_usd"] == 0:
        lines.extend([
            "",
            "Aviso: los costes fijos están a 0 hasta configurarlos en el entorno.",
        ])
    if report.get("unpersisted_events"):
        lines.extend([
            "",
            f"⚠️ {report['unpersisted_events']} consumos están protegidos localmente y pendientes "
            "de sincronizar con Supabase.",
        ])
    if report.get("total_budget_usd"):
        lines.append(
            f"Presupuesto total: {_format_cost(report['total_budget_usd'])} "
            f"({report['total_budget_pct']:.1f}% comprometido)"
        )
    return "\n".join(lines)[:4000]


async def cmd_costes(update: Update, context: CallbackContext) -> None:
    if not _usuario_autorizado(update):
        await _rechazar(update)
        return
    try:
        report = await db.desglose_costes_ai()
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Actualizar", callback_data="cost:refresh")
        ]])
        await update.message.reply_text(_format_cost_report(report), reply_markup=markup)
    except Exception as exc:
        logger.error("No se pudo cargar el desglose de costes: %s", exc, exc_info=True)
        await update.message.reply_text("No pude obtener el desglose de costes.")


async def cmd_feedback(update: Update, context: CallbackContext) -> None:
    if not _usuario_autorizado(update):
        await _rechazar(update)
        return
    args = list(context.args)
    if not args:
        await update.message.reply_text(
            "Uso: /feedback escribe aquí lo ocurrido, o "
            "/feedback REFERENCIA explica el problema del documento"
        )
        return
    ingestion_id = None
    reference = args[0]
    if re.fullmatch(r"[0-9a-fA-F]{6,36}", reference):
        ingestion = await db.buscar_ingestion_por_referencia(
            reference, update.effective_user.id
        )
        if not ingestion:
            await update.message.reply_text(
                "La referencia de feedback no existe o es ambigua. "
                "Compruébala con /revisiones."
            )
            return
        ingestion_id = ingestion["id"]
        args = args[1:]
        if not args:
            await update.message.reply_text(
                f"Escribe qué ocurrió después de la referencia {reference}."
            )
            return
    message = " ".join(args).strip()
    if not message:
        await update.message.reply_text("El texto del feedback no puede estar vacío.")
        return
    await db.registrar_evento_auditoria(
        "user.feedback", actor_type="telegram_user", actor_id=str(update.effective_user.id),
        ingestion_id=ingestion_id, data={"message": message[:1500]},
    )
    if settings.TELEGRAM_ADMIN_CHAT_ID and int(settings.TELEGRAM_ADMIN_CHAT_ID) != update.effective_chat.id:
        try:
            await context.bot.send_message(
                chat_id=int(settings.TELEGRAM_ADMIN_CHAT_ID),
                text=(
                    f"Nuevo feedback del usuario {update.effective_user.id}"
                    f"{f' sobre {reference}' if ingestion_id else ''}:\n{message[:1500]}"
                ),
            )
        except Exception as exc:
            logger.warning("Feedback guardado pero no notificado al administrador: %s", exc)
    await update.message.reply_text(
        "Feedback guardado y asociado al documento."
        if ingestion_id else "Feedback general guardado. Queda registrado para revisarlo."
    )


async def cmd_anular(update: Update, context: CallbackContext) -> None:
    if not _usuario_autorizado(update):
        await _rechazar(update)
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Uso: /anular REFERENCIA motivo. Después introduce la versión correcta con /manual."
        )
        return
    albaran = await db.buscar_albaran_por_referencia(context.args[0])
    if not albaran:
        await update.message.reply_text("Referencia no encontrada o ambigua.")
        return
    reason = " ".join(context.args[1:]).strip()
    try:
        await db.archivar_albaran(albaran["id"], update.effective_user.id, reason)
        await update.message.reply_text(
            "Albarán archivado sin borrar su trazabilidad. Usa /manual para registrar la versión correcta."
        )
    except Exception as exc:
        logger.error("No se pudo archivar el albarán: %s", exc, exc_info=True)
        await update.message.reply_text("No pude archivar ese albarán.")


# ── Entrada manual de albaranes (/manual) ───────────────────────────────────────

async def cmd_manual(update: Update, context: CallbackContext) -> None:
    if not _usuario_autorizado(update):
        await _rechazar(update)
        return
    chat_id = update.effective_chat.id
    if context.args:
        ingestion = await db.buscar_ingestion_por_referencia(
            context.args[0], update.effective_user.id
        )
        if not ingestion:
            await update.message.reply_text("Referencia no encontrada o ambigua.")
            return
        try:
            texto = await manual_albaran.iniciar_desde_ingestion(
                chat_id, update.effective_user.id, ingestion["id"]
            )
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
    else:
        texto = await manual_albaran.iniciar(chat_id, update.effective_user.id)
    await update.message.reply_text(texto)


async def cmd_cancelar(update: Update, context: CallbackContext) -> None:
    if not _usuario_autorizado(update):
        await _rechazar(update)
        return
    chat_id = update.effective_chat.id
    if manual_albaran.flujo_activo(chat_id):
        await update.message.reply_text(manual_albaran.cancelar(chat_id))
    else:
        await update.message.reply_text("No hay ninguna entrada manual en curso.")


async def cmd_corregir(update: Update, context: CallbackContext) -> None:
    if not _usuario_autorizado(update):
        await _rechazar(update)
        return
    chat_id = update.effective_chat.id
    if manual_albaran.flujo_activo(chat_id) and not context.args:
        await update.message.reply_text(manual_albaran.corregir_ultimo(chat_id))
        return
    if context.args:
        await cmd_editar(update, context)
        return
    await update.message.reply_text(
        "Para corregir un candidato pendiente:\n"
        "/corregir REFERENCIA total 123,45\n"
        "/corregir REFERENCIA linea 2 importe 47,25\n"
        "/corregir REFERENCIA linea 2 nombre Sardina\n\n"
        "Consulta /revisiones. Durante una entrada /manual, /corregir borra el último producto."
    )


def _review_markup(view, ingestion_id: str) -> InlineKeyboardMarkup:
    version = str(getattr(view, "candidate_artifact_id", ""))[:12]
    suffix = f":{version}" if version else ""
    buttons = []
    if view.can_approve:
        buttons.append([InlineKeyboardButton(
            "✅ Confirmar definitivamente",
            callback_data=f"rv:approve:{ingestion_id}{suffix}"
        )])
    if view.probable_duplicate:
        buttons.append([
            InlineKeyboardButton(
                "Es duplicado", callback_data=f"rv:duplicate:{ingestion_id}{suffix}"
            ),
            InlineKeyboardButton(
                "No es duplicado", callback_data=f"rv:notdup:{ingestion_id}{suffix}"
            ),
        ])
    buttons.extend([
        [InlineKeyboardButton(
            "✍️ Introducir a mano", callback_data=f"manual:start:{ingestion_id}"
        )],
        [InlineKeyboardButton(
            "🚫 Rechazar", callback_data=f"rv:reject:{ingestion_id}{suffix}"
        )],
    ])
    return InlineKeyboardMarkup(buttons)


def _split_telegram_text(text: str, limit: int = 3800) -> list[str]:
    """Pagina sin cortar líneas silenciosamente ni superar el límite de Telegram."""
    pages: list[str] = []
    current = ""
    for line in text.splitlines() or [""]:
        chunks = [line[index:index + limit] for index in range(0, len(line), limit)] or [""]
        for chunk in chunks:
            candidate = f"{current}\n{chunk}" if current else chunk
            if len(candidate) > limit:
                pages.append(current)
                current = chunk
            else:
                current = candidate
    if current or not pages:
        pages.append(current)
    return pages


async def _send_review_original(bot, chat_id: int, ingestion_id: str) -> None:
    try:
        ingestion = await db.obtener_ingestion(ingestion_id)
        bucket = (ingestion or {}).get("storage_bucket")
        path = (ingestion or {}).get("storage_path")
        if not bucket or not path:
            return
        content = await db.descargar_original_privado(bucket, path)
        stream = io.BytesIO(content)
        stream.name = str(path).rsplit("/", 1)[-1] or f"{ingestion_id[:8]}.jpg"
        await bot.send_document(
            chat_id=chat_id,
            document=InputFile(stream, filename=stream.name),
            caption=f"Original privado — referencia {ingestion_id[:8]}",
        )
    except Exception as exc:
        logger.warning("No se pudo adjuntar el original de %s: %s", ingestion_id, exc)


async def _reply_review_command(update, context, view, ingestion_id: str) -> None:
    bot_instance = getattr(context, "bot", None)
    if bot_instance is not None:
        await _send_review_original(bot_instance, update.effective_chat.id, ingestion_id)
    pages = _split_telegram_text(view.text)
    for index, page in enumerate(pages):
        await update.message.reply_text(
            page,
            reply_markup=_review_markup(view, ingestion_id) if index == len(pages) - 1 else None,
        )


async def _send_review_callback(context, chat_id: int, view, ingestion_id: str) -> None:
    await _send_review_original(context.bot, chat_id, ingestion_id)
    pages = _split_telegram_text(view.text)
    for index, page in enumerate(pages):
        await context.bot.send_message(
            chat_id=chat_id,
            text=page,
            reply_markup=_review_markup(view, ingestion_id) if index == len(pages) - 1 else None,
        )


async def handle_callback(update: Update, context: CallbackContext) -> None:
    """Maneja los botones inline. Por ahora: 'Introducir a mano' tras un OCR fallido."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not _usuario_autorizado(update):
        return
    chat_id = update.effective_chat.id
    if query.data == "cost:refresh":
        try:
            report = await db.desglose_costes_ai()
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Actualizar", callback_data="cost:refresh")
            ]])
            await query.edit_message_text(_format_cost_report(report), reply_markup=markup)
        except Exception as exc:
            logger.error("No se pudo refrescar el coste: %s", exc, exc_info=True)
            await context.bot.send_message(chat_id=chat_id, text="No pude actualizar los costes.")
        return
    if query.data and query.data.startswith("manual:start:"):
        ingestion_id = query.data.split(":", 2)[2]
        try:
            text = await manual_albaran.iniciar_desde_ingestion(
                chat_id, update.effective_user.id, ingestion_id
            )
            await query.edit_message_text(text)
        except Exception as exc:
            logger.error("No se pudo iniciar el modo manual: %s", exc, exc_info=True)
            await context.bot.send_message(chat_id=chat_id, text="No pude abrir ese documento en modo manual.")
        return
    if query.data and query.data.startswith("job:retry:"):
        ingestion_id = query.data.split(":", 2)[2]
        ok = await db.reintentar_ingestion_fallida(ingestion_id, update.effective_user.id)
        await query.edit_message_text(
            f"Documento puesto de nuevo en cola. Referencia: {ingestion_id[:8]}"
            if ok else "Ese documento ya no está disponible para reintentar."
        )
        return
    if query.data and query.data.startswith("rv:"):
        try:
            parts = query.data.split(":", 3)
            if len(parts) < 3:
                raise ValueError("Acción de revisión inválida")
            _, action, ingestion_id = parts[:3]
            expected_artifact = parts[3] if len(parts) == 4 else None
            user_id = update.effective_user.id
            if action == "open":
                view = await build_review_view(ingestion_id, user_id)
                await query.edit_message_text(f"Revisión {ingestion_id[:8]} abierta debajo.")
                await _send_review_callback(context, chat_id, view, ingestion_id)
            elif action == "approve":
                if not expected_artifact:
                    view = await build_review_view(ingestion_id, user_id)
                    await query.edit_message_text("La revisión cambió; usa los botones nuevos de abajo.")
                    await _send_review_callback(context, chat_id, view, ingestion_id)
                    return
                result = await approve_all(
                    ingestion_id, user_id, expected_artifact_prefix=expected_artifact
                )
                await query.edit_message_text(
                    f"✅ Albarán confirmado. Referencia: {str(result.get('albaran_id'))[:8]}"
                )
            elif action in {"reject", "duplicate"}:
                if not expected_artifact:
                    view = await build_review_view(ingestion_id, user_id)
                    await query.edit_message_text(
                        "Abre la revisión actual antes de rechazar el documento."
                    )
                    await _send_review_callback(context, chat_id, view, ingestion_id)
                    return
                await reject_ingestion(
                    ingestion_id, user_id, as_duplicate=action == "duplicate",
                    expected_artifact_prefix=expected_artifact,
                )
                await query.edit_message_text(
                    "Documento marcado como duplicado." if action == "duplicate" else "Documento rechazado."
                )
            elif action == "notdup":
                view = await build_review_view(ingestion_id, user_id)
                if not expected_artifact or not view.candidate_artifact_id.startswith(
                    expected_artifact.lower()
                ):
                    raise ValueError("La revisión ha cambiado; vuelve a abrir el documento")
                reviews = await db.listar_revisiones_ingestion(ingestion_id, only_open=True)
                duplicate_review = next(
                    (r for r in reviews if r.get("reason_code") == "probable_duplicate"), None
                )
                if duplicate_review:
                    await db.resolver_revision(
                        duplicate_review["id"], status="accepted", accepted_value=False,
                        resolved_by=f"telegram_user:{user_id}", note="Confirmado como documento nuevo",
                    )
                view = await build_review_view(ingestion_id, user_id)
                await query.edit_message_text("Marcado como documento nuevo. Revisión actualizada debajo.")
                await _send_review_callback(context, chat_id, view, ingestion_id)
        except Exception as exc:
            logger.error("Error en revisión durable: %s", exc, exc_info=True)
            await context.bot.send_message(
                chat_id=chat_id, text="No pude completar la revisión. Inténtalo de nuevo."
            )


# ── Manejador de fotos ────────────────────────────────────────────────────────

async def _handle_image_file(
    update: Update, context: CallbackContext, *, file_id: str, file_unique_id: str | None,
) -> None:
    chat_id = update.effective_chat.id
    try:
        file = await context.bot.get_file(file_id)
        imagen_bytes = bytes(await file.download_as_bytearray())
    except Exception as e:
        logger.error("Error descargando imagen: %s", e, exc_info=True)
        await update.message.reply_text("No pude descargar la imagen. Inténtalo de nuevo.")
        return

    # En una entrada manual la foto se archiva, pero nunca dispara OCR por sorpresa.
    if manual_albaran.flujo_activo(chat_id):
        respuesta = await manual_albaran.manejar_foto(chat_id, imagen_bytes)
        await update.message.reply_text(
            respuesta or "Estás en una entrada manual. Termina con FIN/OK o escribe /cancelar."
        )
        return

    try:
        intake = await receive_image(
            data=imagen_bytes,
            telegram_user_id=update.effective_user.id,
            telegram_chat_id=chat_id,
            file_unique_id=file_unique_id,
        )
        if intake.duplicate:
            markup = None
            guidance = ""
            if intake.status == "failed":
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "🔄 Reintentar OCR", callback_data=f"job:retry:{intake.ingestion_id}"
                    )],
                    [InlineKeyboardButton(
                        "✍️ Introducir a mano", callback_data=f"manual:start:{intake.ingestion_id}"
                    )],
                ])
                guidance = " Puedes reintentarla o introducirla a mano."
            elif intake.status == "rejected":
                markup = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "✍️ Introducir a mano", callback_data=f"manual:start:{intake.ingestion_id}"
                    )
                ]])
                guidance = " Puedes reutilizar la foto para introducirla a mano."
            await update.message.reply_text(
                f"Esta imagen ya estaba recibida ({intake.status}). "
                f"Referencia: {intake.ingestion_id[:8]}.{guidance}",
                reply_markup=markup,
            )
        elif intake.queue_position <= 1:
            await update.message.reply_text(
                f"Recibido y guardado de forma segura. Procesando… Referencia: {intake.ingestion_id[:8]}"
            )
        else:
            await update.message.reply_text(
                f"Recibido y guardado de forma segura. Posición aproximada: {intake.queue_position}. "
                f"Referencia: {intake.ingestion_id[:8]}"
            )
    except ValueError as e:
        await update.message.reply_text(str(e))
    except Exception as e:
        logger.error("Error recibiendo la imagen: %s", e, exc_info=True)
        await update.message.reply_text("No pude guardar la imagen. Inténtalo de nuevo.")


async def handle_photo(update: Update, context: CallbackContext) -> None:
    if not _usuario_autorizado(update):
        await _rechazar(update)
        return
    photo = update.message.photo[-1]
    await _handle_image_file(
        update, context, file_id=photo.file_id,
        file_unique_id=getattr(photo, "file_unique_id", None),
    )


async def handle_image_document(update: Update, context: CallbackContext) -> None:
    """Acepta la foto como archivo para evitar la compresión de Telegram."""
    if not _usuario_autorizado(update):
        await _rechazar(update)
        return
    document = update.message.document
    if not document or document.mime_type not in {"image/jpeg", "image/png"}:
        await update.message.reply_text("Envíame una imagen JPEG o PNG.")
        return
    await _handle_image_file(
        update, context, file_id=document.file_id,
        file_unique_id=getattr(document, "file_unique_id", None),
    )


# ── Manejador de texto ────────────────────────────────────────────────────────

async def handle_text(update: Update, context: CallbackContext) -> None:
    if not _usuario_autorizado(update):
        await _rechazar(update)
        return

    chat_id = update.effective_chat.id

    # Entrada manual en curso: tiene prioridad sobre todo lo demás.
    if manual_albaran.flujo_activo(chat_id):
        respuesta = await manual_albaran.manejar_texto(chat_id, update.message.text)
        await update.message.reply_text(respuesta)
        return

    texto = update.message.text.strip()

    # Consulta en lenguaje natural
    try:
        historial = obtener_historial(chat_id)
        respuesta = await consultar(texto, historial=historial, user_id=update.effective_user.id)
        await update.message.reply_text(respuesta)
        # Guardar turno solo si la respuesta no fue un error técnico del sistema
        if not respuesta.startswith(("No pude", "Solo puedo", "Sistema temporalmente")):
            agregar_turno(chat_id, texto, respuesta)
    except Exception as e:
        logger.error("Error en consulta: %s", e, exc_info=True)
        await update.message.reply_text("No pude procesar la consulta. Inténtalo de nuevo.")


# ── Resumen semanal ───────────────────────────────────────────────────────────

async def resumen_semanal(context: CallbackContext) -> None:
    if not settings.TELEGRAM_ADMIN_CHAT_ID:
        return

    hoy = date.today()
    lunes_pasado = hoy - timedelta(days=hoy.weekday() + 7)
    domingo_pasado = lunes_pasado + timedelta(days=6)

    try:
        respuesta = await consultar(
            f"Resumen de la semana del {lunes_pasado.strftime('%d/%m/%Y')} "
            f"al {domingo_pasado.strftime('%d/%m/%Y')}: "
            f"gasto total, top 3 proveedores por gasto, y productos con mayor variación de precio",
            user_id=int(settings.TELEGRAM_ADMIN_CHAT_ID),
        )
        conteo = await db.contar_jobs_por_estado()
        mensaje = (
            f"Resumen semana {lunes_pasado.strftime('%d/%m')} — {domingo_pasado.strftime('%d/%m/%Y')}\n\n"
            f"{respuesta}\n\n"
            f"Albaranes procesados esta semana: {conteo['completado']}"
        )
        await context.bot.send_message(
            chat_id=int(settings.TELEGRAM_ADMIN_CHAT_ID),
            text=mensaje,
        )
    except Exception as e:
        logger.error("Error enviando resumen semanal: %s", e)


async def monitor_operativo(context: CallbackContext) -> None:
    """Alerta al administrador solo cuando cambia el conjunto de incidencias."""
    global _last_alert_signature
    if not settings.TELEGRAM_ADMIN_CHAT_ID:
        return
    try:
        metrics = await db.metricas_operativas()
        ing = metrics["ingestions"]
        alerts: list[str] = []
        if ing.get("failed", 0):
            alerts.append(f"{ing['failed']} ingestas fallidas")
        open_count = ing.get("queued", 0) + ing.get("processing", 0) + ing.get("needs_review", 0)
        if open_count >= max(1, int(settings.MAX_PENDING_GLOBAL * 0.8)):
            alerts.append(f"cola/revisión al {open_count}/{settings.MAX_PENDING_GLOBAL}")
        if metrics.get("oldest_review_hours", 0) >= 24:
            alerts.append(f"revisión sin resolver desde hace {metrics['oldest_review_hours']:.1f} h")
        if metrics["monthly_ai_cost_usd"] >= settings.MONTHLY_AI_BUDGET_USD * 0.8:
            alerts.append(
                f"coste IA ${metrics['monthly_ai_cost_usd']:.2f} / "
                f"${settings.MONTHLY_AI_BUDGET_USD:.2f}"
            )
        if settings.MONTHLY_TOTAL_BUDGET_USD > 0:
            cost_report = await db.desglose_costes_ai()
            if cost_report["projected_committed_usd"] >= settings.MONTHLY_TOTAL_BUDGET_USD * 0.8:
                alerts.append(
                    f"coste total comprometido ${cost_report['projected_committed_usd']:.2f} / "
                    f"${settings.MONTHLY_TOTAL_BUDGET_USD:.2f}"
                )
        signature = tuple(sorted(alerts))
        if alerts and signature != _last_alert_signature:
            await context.bot.send_message(
                chat_id=int(settings.TELEGRAM_ADMIN_CHAT_ID),
                text="Alerta operativa\n• " + "\n• ".join(alerts),
            )
        _last_alert_signature = signature
    except Exception as exc:
        logger.error("Fallo del monitor operativo: %s", exc, exc_info=True)


# ── Arranque ──────────────────────────────────────────────────────────────────

async def post_init(application: Application) -> None:
    contract_issues = await db.verificar_contrato_produccion()
    if contract_issues:
        raise RuntimeError(
            "Supabase no cumple el contrato de producción: " + "; ".join(contract_issues)
        )
    reconciled_costs = await db.reconciliar_spool_costes()
    if reconciled_costs:
        logger.info("Reconciliados %s eventos pendientes de coste de IA", reconciled_costs)
    await application.bot.set_my_commands([
        BotCommand("start", "Inicio y comandos"),
        BotCommand("manual", "Registrar un albarán a mano"),
        BotCommand("revisiones", "Documentos que esperan revisión"),
        BotCommand("ultimos", "Últimos albaranes"),
        BotCommand("detalle", "Ver un albarán"),
        BotCommand("corregir", "Corregir un candidato"),
        BotCommand("estado", "Estado de la cola"),
        BotCommand("costes", "Costes en tiempo real"),
        BotCommand("feedback", "Feedback general o de un documento"),
        BotCommand("ayuda", "Ayuda y ejemplos"),
    ])
    # Application conserva y espera esta tarea durante el apagado. Así una
    # actualización no abandona silenciosamente una llamada OCR/LLM facturable.
    application.create_task(start_durable_workers(application.bot, n=2))
    logger.info("Pool durable de 2 workers iniciado")


def main() -> None:
    app = (
        ApplicationBuilder()
        .token(settings.TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("manual", cmd_manual))
    app.add_handler(CommandHandler("cancelar", cmd_cancelar))
    app.add_handler(CommandHandler("corregir", cmd_corregir))
    app.add_handler(CommandHandler("estado", cmd_estado))
    app.add_handler(CommandHandler("resumen", cmd_resumen))
    app.add_handler(CommandHandler("proveedores", cmd_proveedores))
    app.add_handler(CommandHandler("ultimos", cmd_ultimos))
    app.add_handler(CommandHandler("detalle", cmd_detalle))
    app.add_handler(CommandHandler("revisiones", cmd_revisiones))
    app.add_handler(CommandHandler("revisar", cmd_revisar))
    app.add_handler(CommandHandler("editar", cmd_editar))
    app.add_handler(CommandHandler("reintentar", cmd_reintentar))
    app.add_handler(CommandHandler("metricas", cmd_metricas))
    app.add_handler(CommandHandler("costes", cmd_costes))
    app.add_handler(CommandHandler("feedback", cmd_feedback))
    app.add_handler(CommandHandler("anular", cmd_anular))
    app.add_handler(CommandHandler("ayuda", cmd_ayuda))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_image_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    if settings.TELEGRAM_ADMIN_CHAT_ID:
        app.job_queue.run_repeating(monitor_operativo, interval=900, first=60)
        app.job_queue.run_daily(
            resumen_semanal,
            time=time(hour=9, minute=0, tzinfo=_ZONA_HORARIA),
            days=(0,),
        )
        logger.info("Resumen semanal programado para los lunes a las 9:00 (Madrid)")

    logger.info("Bot iniciado. Esperando mensajes...")
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
