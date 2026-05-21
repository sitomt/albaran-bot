"""
Bot de Telegram — Gestor de Compras.
Punto de entrada del sistema. Arranca con: python src/bot.py
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import date, datetime, time, timedelta

import pytz
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    MessageHandler,
    filters,
)

from .config import settings
from . import supabase_client as db
from .query_engine import consultar
from .queue_manager import encolar_job, recuperar_jobs_pendientes, start_workers, _pending_confirmations, _TIMEOUT_CONFIRMACION
from .conversation_history import agregar_turno, obtener_historial, limpiar_historial

logging.basicConfig(
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
    level=logging.INFO,
)


def _usuario_autorizado(update: Update) -> bool:
    """Devuelve True si la whitelist está vacía o el usuario está en ella."""
    allowed = settings.allowed_users
    if not allowed:
        return True
    return update.effective_user.id in allowed


async def _rechazar(update: Update) -> None:
    await update.message.reply_text("No tienes acceso a este sistema.")
    logger.warning("Acceso denegado a user_id=%s username=%s", update.effective_user.id, update.effective_user.username)
logger = logging.getLogger(__name__)

_ZONA_HORARIA = pytz.timezone("Europe/Madrid")

_PRESENTACION = """\
Gestor de Compras

Mándame fotos de albaranes para registrarlos. También puedo responder cualquier pregunta sobre gastos, precios y proveedores.

/estado — Cola de procesamiento
/resumen — Resumen de la semana
/proveedores — Proveedores registrados
/revisiones — Líneas pendientes de revisión
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
Corregir total de [id-corto]: 370.38
Corregir producto [id-corto]: [nombre correcto]
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
        texto = f"No se pudo obtener el estado: {e}"
    await update.message.reply_text(texto)


async def cmd_resumen(update: Update, context: CallbackContext) -> None:
    if not _usuario_autorizado(update):
        await _rechazar(update)
        return
    hoy = date.today()
    lunes = hoy - timedelta(days=hoy.weekday())
    respuesta = await consultar(
        f"Total gastado por proveedor entre {lunes.strftime('%d/%m/%Y')} y {hoy.strftime('%d/%m/%Y')}, "
        f"ordenado por total descendente"
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
        await update.message.reply_text(f"Error al obtener proveedores: {e}")


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
        lineas = await db.listar_lineas_pendientes_revision()
        if not lineas:
            await update.message.reply_text("No hay líneas pendientes de revisión.")
            return
        texto_lineas = ["Líneas pendientes de revisión:\n"]
        for r in lineas:
            albaran_info = r.get("albaranes") or {}
            proveedor_info = albaran_info.get("proveedores") or {}
            fecha = albaran_info.get("fecha", "?")
            proveedor = proveedor_info.get("nombre", "?")
            texto_lineas.append(
                f"{fecha} {proveedor} — {r['descripcion_limpia']}: "
                f"cant.={r['cantidad']}, precio={r['precio_unitario']}"
            )
        texto_lineas.append(
            "\nUsa 'Corregir producto [id-8char]: [nombre]' para actualizar descripción."
        )
        await update.message.reply_text("\n".join(texto_lineas))
    except Exception as e:
        await update.message.reply_text(f"Error al obtener revisiones: {e}")


async def _procesar_confirmacion(update: Update, conf: dict) -> None:
    """Procesa la respuesta del usuario a una solicitud de confirmación."""
    chat_id = update.effective_chat.id
    texto = update.message.text.strip()

    # Limpiar siempre, independiente de la respuesta
    del _pending_confirmations[chat_id]

    if texto.lower() == "ok":
        await update.message.reply_text("Albarán confirmado.")
        return

    # Parsear respuestas "N: valor"
    correcciones = []
    for linea_texto in texto.split("\n"):
        linea_texto = linea_texto.strip()
        m = re.match(r'^(\d+)\s*[:\-]\s*(.+)$', linea_texto)
        if m:
            num = int(m.group(1))
            valor = m.group(2).strip()
            for item in conf["lineas"]:
                if item["num"] == num:
                    correcciones.append({"item": item, "valor": valor})

    if not correcciones:
        await update.message.reply_text(
            "No entendí la respuesta. El albarán está guardado. "
            "Puedes corregirlo con 'Corregir producto [id]: [valor]'."
        )
        return

    resultados = []
    for c in correcciones:
        linea_id = c["item"]["linea_id"]
        valor = c["valor"]
        descripcion = c["item"]["descripcion"]
        try:
            # Heurística: si es número → actualizar cantidad; si es texto → actualizar descripción
            try:
                nuevo_valor_num = float(valor.replace(",", "."))
                campo = "cantidad"
                valor_original = str(c["item"].get("cantidad", ""))
                kwargs = {"cantidad": nuevo_valor_num, "requiere_revision": False}
            except ValueError:
                campo = "descripcion_limpia"
                valor_original = descripcion
                kwargs = {"descripcion_limpia": valor, "requiere_revision": False}

            # Registrar corrección en BD
            await db.registrar_correccion(linea_id, campo, valor_original, valor)
            # Aplicar corrección
            await db.actualizar_linea_albaran(linea_id, **kwargs)
            resultados.append(f"'{descripcion}' {campo} → {valor}")
        except Exception as e:
            logger.error("Error aplicando corrección: %s", e)
            resultados.append(f"Error corrigiendo '{descripcion}': {e}")

    await update.message.reply_text("Correcciones aplicadas:\n" + "\n".join(resultados))


# ── Manejador de fotos ────────────────────────────────────────────────────────

async def handle_photo(update: Update, context: CallbackContext) -> None:
    if not _usuario_autorizado(update):
        await _rechazar(update)
        return
    chat_id = update.effective_chat.id
    foto = update.message.photo[-1]

    try:
        file = await context.bot.get_file(foto.file_id)
        imagen_bytes = bytes(await file.download_as_bytearray())
    except Exception as e:
        await update.message.reply_text(f"Error descargando la imagen: {e}")
        return

    # Comprobación por hash antes de gastar tokens de OCR
    imagen_hash = hashlib.sha256(imagen_bytes).hexdigest()
    try:
        existente = await db.buscar_albaran_por_hash(imagen_hash)
        if existente:
            fecha_str = (existente.get("creado_en") or "")[:10]
            numero_str = existente.get("numero_albaran") or "sin número"
            await update.message.reply_text(
                f"Ya procesé esta imagen exacta (albarán {numero_str}, registrado el {fecha_str}).\n"
                "Si quieres registrar un albarán distinto, envía una foto nueva."
            )
            return
    except Exception as e:
        logger.warning("Error comprobando hash de imagen: %s — continuando", e)

    try:
        job = await db.crear_job(telegram_user_id=chat_id)
        pos = await encolar_job(job["id"], imagen_bytes, chat_id, context.bot, imagen_hash=imagen_hash)

        if pos == 1:
            await update.message.reply_text("Recibido, procesando...")
        else:
            await update.message.reply_text(f"Recibido. Hay {pos - 1} albarán(es) antes que este.")
    except Exception as e:
        logger.error("Error encolando job: %s", e, exc_info=True)
        await update.message.reply_text(f"Error al procesar la imagen: {e}")


# ── Manejador de texto ────────────────────────────────────────────────────────

_PATRON_CORRECCION_TOTAL = re.compile(
    r"^corregir\s+total\s+de\s+([a-f0-9]{6,8})\s*:\s*([\d.,]+)$",
    re.IGNORECASE,
)
_PATRON_CORRECCION_PRODUCTO = re.compile(
    r"^corregir\s+producto\s+([a-f0-9]{6,8})\s*:\s*(.+)$",
    re.IGNORECASE,
)


async def handle_text(update: Update, context: CallbackContext) -> None:
    if not _usuario_autorizado(update):
        await _rechazar(update)
        return

    chat_id = update.effective_chat.id

    # Comprobar confirmación pendiente
    conf = _pending_confirmations.get(chat_id)
    if conf:
        if datetime.now() - conf["timestamp"] < _TIMEOUT_CONFIRMACION:
            await _procesar_confirmacion(update, conf)
            return
        else:
            # Expirado
            del _pending_confirmations[chat_id]
            await update.message.reply_text(
                "Tiempo de confirmación expirado. Albarán guardado con líneas marcadas para revisión. "
                "Usa /revisiones para verlas."
            )

    texto = update.message.text.strip()

    # Corrección de total de albarán
    m = _PATRON_CORRECCION_TOTAL.match(texto)
    if m:
        id_corto, nuevo_total_str = m.group(1), m.group(2)
        await _corregir_total(update, id_corto, nuevo_total_str)
        return

    # Corrección de nombre de producto
    m = _PATRON_CORRECCION_PRODUCTO.match(texto)
    if m:
        id_corto, nuevo_nombre = m.group(1), m.group(2).strip()
        await _corregir_producto(update, id_corto, nuevo_nombre)
        return

    # Consulta en lenguaje natural
    try:
        historial = obtener_historial(chat_id)
        respuesta = await consultar(texto, historial=historial)
        await update.message.reply_text(respuesta)
        # Guardar turno solo si la respuesta no fue un error técnico del sistema
        if not respuesta.startswith(("No pude", "Solo puedo", "Sistema temporalmente")):
            agregar_turno(chat_id, texto, respuesta)
    except Exception as e:
        logger.error("Error en consulta: %s", e, exc_info=True)
        await update.message.reply_text("No pude procesar la consulta. Inténtalo de nuevo.")


async def _corregir_total(update: Update, id_corto: str, total_str: str) -> None:
    try:
        nuevo_total = float(total_str.replace(",", "."))
        rows = await db.ejecutar_sql(
            f"SELECT id FROM albaranes WHERE id::text LIKE '{id_corto}%' LIMIT 1"
        )
        if not rows:
            await update.message.reply_text(f"No encontré ningún albarán con ID que empiece por '{id_corto}'.")
            return
        albaran_id = rows[0]["id"]
        await db.actualizar_campo_albaran(albaran_id, total=nuevo_total)
        await update.message.reply_text(f"Total del albarán {id_corto} actualizado a {nuevo_total:.2f}€.")
    except ValueError:
        await update.message.reply_text(f"'{total_str}' no es un número válido.")
    except Exception as e:
        await update.message.reply_text(f"Error al corregir: {e}")


async def _corregir_producto(update: Update, id_corto: str, nuevo_nombre: str) -> None:
    try:
        rows = await db.ejecutar_sql(
            f"SELECT id FROM lineas_albaran WHERE id::text LIKE '{id_corto}%' LIMIT 1"
        )
        if not rows:
            await update.message.reply_text(f"No encontré ninguna línea con ID que empiece por '{id_corto}'.")
            return
        linea_id = rows[0]["id"]
        client = await db.get_client()
        await client.table("lineas_albaran").update({"descripcion_limpia": nuevo_nombre}).eq("id", linea_id).execute()
        await update.message.reply_text(f"Nombre del producto actualizado a '{nuevo_nombre}'.")
    except Exception as e:
        await update.message.reply_text(f"Error al corregir: {e}")


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
            f"gasto total, top 3 proveedores por gasto, y productos con mayor variación de precio"
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


# ── Arranque ──────────────────────────────────────────────────────────────────

async def post_init(application: Application) -> None:
    import asyncio
    eliminados = await db.limpiar_jobs_antiguos(dias=7)
    if eliminados:
        logger.info("%d jobs antiguos eliminados al arrancar", eliminados)
    recuperados = await recuperar_jobs_pendientes(application.bot)
    if recuperados:
        logger.info("%d jobs pendientes recuperados al arrancar", recuperados)
    asyncio.ensure_future(start_workers(3))
    logger.info("Pool de 3 workers iniciado")


def main() -> None:
    app = (
        ApplicationBuilder()
        .token(settings.TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("estado", cmd_estado))
    app.add_handler(CommandHandler("resumen", cmd_resumen))
    app.add_handler(CommandHandler("proveedores", cmd_proveedores))
    app.add_handler(CommandHandler("revisiones", cmd_revisiones))
    app.add_handler(CommandHandler("ayuda", cmd_ayuda))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    if settings.TELEGRAM_ADMIN_CHAT_ID:
        app.job_queue.run_daily(
            resumen_semanal,
            time=time(hour=9, minute=0, tzinfo=_ZONA_HORARIA),
            days=(0,),
        )
        logger.info("Resumen semanal programado para los lunes a las 9:00 (Madrid)")

    logger.info("Bot iniciado. Esperando mensajes...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
