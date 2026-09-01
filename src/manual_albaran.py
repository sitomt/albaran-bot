"""
Entrada manual de albaranes vía flujo conversacional de Telegram (comando /manual).

Para albaranes manuscritos ilegibles, documentos dañados o casos donde el OCR falla.
Máquina de estados con timeout de 15 min. Solo inserta al confirmar con OK.

Estados (campo `step` del flujo):
  proveedor → [nuevo_nif → nuevo_pago] → cabecera → [fecha] → productos
            → total → forma_pago → foto → confirmacion
(equivale a los pasos 1-7 del enunciado; los sub-estados cubren el alta de proveedor
nuevo y la petición de fecha por separado).
"""
from __future__ import annotations

import logging
import re
import uuid
import hashlib
from datetime import datetime, timedelta

from . import supabase_client as db
from .albaran_processor import _normalizar_numero_albaran, _parsear_numero
from .config import settings
from .intake_service import _validate_image
from .spanish_tax_id import is_valid_spanish_tax_id, normalize_tax_id

logger = logging.getLogger(__name__)

# Estado por chat_id. Estructura del flujo (ver docstring del módulo).
_manual_flows: dict[int, dict] = {}
_TIMEOUT = timedelta(minutes=15)

_MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}


# ── Utilidades ────────────────────────────────────────────────────────────────

def _num(v: str) -> float | None:
    """Parsea un número tecleado por el usuario (formato es-ES, miles + decimal)."""
    return _parsear_numero(v)


def _fmt_importe(valor: float) -> str:
    return f"{valor:,.2f}€".replace(",", "X").replace(".", ",").replace("X", ".")


def _parsear_fecha(texto: str) -> str | None:
    """Devuelve fecha ISO (YYYY-MM-DD) desde formatos flexibles, o None."""
    t = texto.strip()
    # Numérica: dd/mm/yyyy, dd-mm-yyyy, dd.mm.yyyy, con año de 2 o 4 dígitos
    m = re.search(r'(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})', t)
    if m:
        d, mes, a = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if a < 100:
            a += 2000
        try:
            return datetime(a, mes, d).strftime("%Y-%m-%d")
        except ValueError:
            return None
    # Textual: "4 mayo 2026", "4 de mayo de 2026"
    m = re.search(r'(\d{1,2})\s+(?:de\s+)?([a-záéíóú]+)\s+(?:de\s+)?(\d{4})', t.lower())
    if m and m.group(2) in _MESES:
        try:
            return datetime(int(m.group(3)), _MESES[m.group(2)], int(m.group(1))).strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def _parsear_cabecera(texto: str) -> tuple[str | None, str | None]:
    """
    Parsea 'número y fecha' de forma flexible. Devuelve (numero|None, fecha_iso|None).
      '3950 / 04-05-2026'      → ('3950', '2026-05-04')
      '3950, 4 mayo 2026'      → ('3950', '2026-05-04')
      '04/05/2026'             → (None, '2026-05-04')
      '3950'                   → ('3950', None)
    """
    fecha = _parsear_fecha(texto)
    resto = texto
    if fecha:
        # Eliminar la parte de fecha del texto para quedarnos con el número
        resto = re.sub(r'\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}', '', resto)
        resto = re.sub(r'\d{1,2}\s+(?:de\s+)?[a-záéíóú]+\s+(?:de\s+)?\d{4}', '', resto, flags=re.IGNORECASE)
    resto = resto.replace("/", " ").replace(",", " ").strip()
    numero = resto.split()[0] if resto.split() else None
    return (numero or None), fecha


_PATRON_PRODUCTO = re.compile(
    r'^(?P<nombre>.*?)[,\s]+(?P<cant>\d+(?:[.,]\d+)?)[,\s]+(?P<precio>\d+(?:[.,]\d+)?)\s*$'
)


def _parsear_producto(texto: str) -> tuple[str, float, float] | None:
    """
    'Tomate entero, 12, 1.81' → ('Tomate entero', 12.0, 1.81). None si no encaja.

    Ancla los DOS últimos números (cantidad y precio) al final, tolerando decimal con
    coma o punto. Así 'Aceite Oliva, 2, 46,75' no se rompe por la coma decimal y un
    nombre con comas/números ('Vino 2020, 12, 5,50') se parsea bien.
    """
    m = _PATRON_PRODUCTO.match(texto.strip())
    if not m:
        return None
    nombre = m.group("nombre").strip().rstrip(",").strip()
    cantidad = _num(m.group("cant"))
    precio = _num(m.group("precio"))
    if not nombre or cantidad is None or precio is None or cantidad <= 0 or precio < 0:
        return None
    return nombre, cantidad, precio


def _parsear_producto_detallado(texto: str) -> dict | None:
    """Nombre | cantidad | tarifa | descuento | neto | importe, todo observado."""
    partes = [parte.strip() for parte in texto.split("|")]
    if len(partes) != 6 or not partes[0]:
        return None
    cantidad = _num(partes[1])
    tarifa = _num(partes[2])
    descuento = _num(partes[3].rstrip("% "))
    neto = _num(partes[4])
    importe = _num(partes[5])
    if (
        cantidad is None or cantidad <= 0 or tarifa is None or tarifa < 0
        or descuento is None or not 0 <= descuento < 100
        or neto is None or neto < 0 or importe is None or importe < 0
    ):
        return None
    neto_esperado = tarifa * (1 - descuento / 100)
    if abs(neto - neto_esperado) > 0.02 or min(
        abs(cantidad * neto - importe), abs(cantidad * neto_esperado - importe)
    ) > 0.03:
        return None
    return {
        "nombre": partes[0], "cantidad": cantidad, "precio": importe / cantidad,
        "precio_neto_observado": neto,
        "precio_tarifa": tarifa, "descuento_pct": descuento, "importe": importe,
        "entrada_detallada": True,
    }


def _total_lineas(flow: dict) -> float:
    return round(sum(
        l.get("importe") if l.get("importe") is not None else l["cantidad"] * l["precio"]
        for l in flow["lineas"]
    ), 2)


def _parsear_totales(texto: str, suma_lineas: float) -> tuple[float, float, float] | None:
    """Interpreta OK, TOTAL, IVA x o BASE / IVA / TOTAL y valida el cuadre."""
    raw = texto.strip()
    if raw.casefold() == "ok":
        return suma_lineas, 0.0, suma_lineas

    iva_match = re.fullmatch(r"iva\s*[:=]?\s*(.+)", raw, flags=re.IGNORECASE)
    if iva_match:
        iva = _num(iva_match.group(1))
        if iva is None or iva < 0:
            return None
        return suma_lineas, round(iva, 2), round(suma_lineas + iva, 2)

    partes = [parte.strip() for parte in raw.split("/")]
    if len(partes) == 3:
        valores = [_num(re.sub(r"^(?:base|iva|total)\s*[:=]?\s*", "", p, flags=re.IGNORECASE))
                   for p in partes]
        if any(v is None or v < 0 for v in valores):
            return None
        base, iva, total = (round(float(v), 2) for v in valores)
        if abs(base - suma_lineas) > 0.03 or abs(base + iva - total) > 0.03:
            return None
        return base, iva, total

    total = _num(raw)
    if total is None or total <= 0 or total + 0.03 < suma_lineas:
        return None
    return suma_lineas, round(total - suma_lineas, 2), round(total, 2)


# ── Gestión de estado / ciclo de vida ──────────────────────────────────────────

def flujo_activo(chat_id: int) -> bool:
    """True si hay un flujo manual en curso y no ha expirado. Limpia si expiró."""
    flow = _manual_flows.get(chat_id)
    if not flow:
        return False
    if datetime.now() - flow["timestamp"] > _TIMEOUT:
        del _manual_flows[chat_id]
        return False
    return True


def _expirado(chat_id: int) -> bool:
    flow = _manual_flows.get(chat_id)
    return bool(flow) and (datetime.now() - flow["timestamp"] > _TIMEOUT)


def cancelar(chat_id: int) -> str:
    _manual_flows.pop(chat_id, None)
    return "Entrada manual cancelada. No se ha guardado nada."


# ── Inicio del flujo (paso 1) ──────────────────────────────────────────────────

async def iniciar(chat_id: int, user_id: int | None = None) -> str:
    proveedores = await db.listar_todos_proveedores()
    flow = {
        "step": "proveedor",
        "proveedor_id": None,
        "proveedor_nombre": None,
        "numero_albaran": None,
        "fecha": None,
        "lineas": [],
        "forma_pago": None,
        "base_manual": None,
        "iva_manual": None,
        "total_manual": None,
        "imagen_url": None,
        "timestamp": datetime.now(),
        "_proveedores": proveedores,
        "_nuevo": None,
        "_chat_id": chat_id,
        "_user_id": user_id or chat_id,
        "_imagen_bytes": None,
        "_existing_evidence": False,
        "_durable_ingestion_id": None,
    }
    _manual_flows[chat_id] = flow

    if proveedores:
        listado = "\n".join(f"{i}. {p['nombre']}" for i, p in enumerate(proveedores, 1))
        cuerpo = f"Proveedores registrados:\n{listado}\n\nEscribe el número o el nombre si es uno nuevo."
    else:
        cuerpo = "No hay proveedores registrados todavía. Escribe el nombre del proveedor."
    nota_foto = (
        "📎 Usaré la foto que ya está guardada como archivo del albarán.\n\n"
        if flow["_imagen_bytes"] or flow["_existing_evidence"] else ""
    )
    return (
        "Vamos a registrar un albarán manualmente.\n"
        "(escribe /cancelar en cualquier momento para abortar)\n\n"
        f"{nota_foto}¿De qué proveedor es?\n\n{cuerpo}"
    )


async def iniciar_desde_ingestion(chat_id: int, user_id: int, ingestion_id: str) -> str:
    """Reutiliza el original durable de un OCR fallido sin volver a subirlo."""
    ingestion = await db.obtener_ingestion(ingestion_id)
    if not ingestion or ingestion.get("status") not in {
        "failed", "rejected", "extracted", "needs_review"
    }:
        raise ValueError("Ese documento no está disponible para entrada manual")
    text = await iniciar(chat_id, user_id)
    flow = _manual_flows[chat_id]
    flow["_durable_ingestion_id"] = ingestion_id
    flow["_source_status"] = ingestion.get("status")
    flow["_existing_evidence"] = bool(
        ingestion.get("storage_bucket") and ingestion.get("storage_path")
    )
    if flow["_existing_evidence"]:
        text = "📎 Usaré la foto que ya está guardada como archivo del albarán.\n\n" + text
    return text


# ── Manejador principal de texto ────────────────────────────────────────────────

async def manejar_texto(chat_id: int, texto: str) -> str:
    """Procesa un mensaje de texto dentro del flujo manual. Devuelve la respuesta del bot."""
    if _expirado(chat_id):
        _manual_flows.pop(chat_id, None)
        return "Entrada manual cancelada por inactividad."

    flow = _manual_flows.get(chat_id)
    if not flow:
        return "No hay ninguna entrada manual en curso. Empieza con /manual."

    flow["timestamp"] = datetime.now()
    texto = texto.strip()
    step = flow["step"]

    if step == "proveedor":
        return await _step_proveedor(flow, texto)
    if step == "nuevo_nif":
        return _step_nuevo_nif(flow, texto)
    if step == "nuevo_pago":
        return await _step_nuevo_pago(chat_id, flow, texto)
    if step == "cabecera":
        return _step_cabecera(flow, texto)
    if step == "fecha":
        return _step_fecha(flow, texto)
    if step == "productos":
        return _step_productos(flow, texto)
    if step == "total":
        return _step_total(flow, texto)
    if step == "forma_pago":
        return _step_forma_pago(flow, texto)
    if step == "foto":
        return _step_foto_texto(flow, texto)
    if step == "confirmacion":
        return await _step_confirmacion(chat_id, flow, texto)
    return "Estado desconocido. Usa /cancelar y vuelve a empezar con /manual."


# ── Paso 2: proveedor ───────────────────────────────────────────────────────────

async def _step_proveedor(flow: dict, texto: str) -> str:
    proveedores = flow["_proveedores"]
    # ¿Eligió un número de la lista?
    if texto.isdigit():
        idx = int(texto)
        if 1 <= idx <= len(proveedores):
            p = proveedores[idx - 1]
            flow["proveedor_id"] = p["id"]
            flow["proveedor_nombre"] = p["nombre"]
            flow["step"] = "cabecera"
            return _pedir_cabecera(p["nombre"])
        return f"No hay proveedor con el número {idx}. Elige uno de la lista o escribe un nombre nuevo."

    # Nombre escrito: ¿coincide con uno existente (case-insensitive)?
    existente = next((p for p in proveedores if p["nombre"].strip().lower() == texto.lower()), None)
    if existente:
        flow["proveedor_id"] = existente["id"]
        flow["proveedor_nombre"] = existente["nombre"]
        flow["step"] = "cabecera"
        return _pedir_cabecera(existente["nombre"])

    # Proveedor nuevo
    flow["_nuevo"] = {"nombre": texto, "nif": None, "forma_pago": None}
    flow["step"] = "nuevo_nif"
    return f"Proveedor nuevo: «{texto}».\n¿Cuál es su NIF? (escribe NO si no lo tienes)"


def _step_nuevo_nif(flow: dict, texto: str) -> str:
    if texto.lower() not in ("no", "n"):
        nif = texto.strip()
        normalized = normalize_tax_id(nif)
        if normalized in settings.customer_nifs_set:
            return "Ese NIF pertenece al restaurante, no al proveedor. Escribe el NIF del proveedor o NO."
        if not is_valid_spanish_tax_id(nif):
            return "Ese NIF/CIF no supera el dígito de control. Revísalo o escribe NO."
        flow["_nuevo"]["nif"] = nif
    flow["step"] = "nuevo_pago"
    return "¿Forma de pago habitual? (ej: 15 días, 30 días, contado)"


async def _step_nuevo_pago(chat_id: int, flow: dict, texto: str) -> str:
    if texto.lower() not in ("no", "n", ""):
        flow["_nuevo"]["forma_pago"] = texto.strip()
    nuevo = flow["_nuevo"]
    # El proveedor no se crea todavía: se publicará dentro de la misma transacción
    # que el albarán para no dejar catálogos huérfanos al cancelar o fallar.
    flow["proveedor_id"] = None
    flow["proveedor_nombre"] = nuevo["nombre"]
    flow["forma_pago"] = nuevo["forma_pago"]  # se podrá sobrescribir después
    flow["step"] = "cabecera"
    return f"Proveedor nuevo «{nuevo['nombre']}».\n\n" + _pedir_cabecera(nuevo["nombre"])


# ── Paso 3: cabecera (número + fecha) ───────────────────────────────────────────

def _pedir_cabecera(proveedor_nombre: str) -> str:
    return (
        f"Proveedor: {proveedor_nombre}.\n\n"
        "¿Número de albarán y fecha?\n"
        "Escríbelos así: 3950 / 04-05-2026\n"
        "(o solo la fecha si no hay número)"
    )


def _step_cabecera(flow: dict, texto: str) -> str:
    numero, fecha = _parsear_cabecera(texto)
    flow["numero_albaran"] = numero
    if fecha:
        flow["fecha"] = fecha
        flow["step"] = "productos"
        return _pedir_productos()
    flow["step"] = "fecha"
    return "No he reconocido la fecha. Escríbela así: 04-05-2026 (o «4 mayo 2026»)."


def _step_fecha(flow: dict, texto: str) -> str:
    fecha = _parsear_fecha(texto)
    if not fecha:
        return "Sigo sin entender la fecha. Prueba con 04-05-2026 o «4 de mayo de 2026»."
    flow["fecha"] = fecha
    flow["step"] = "productos"
    return _pedir_productos()


# ── Paso 4: productos en bucle ──────────────────────────────────────────────────

def _pedir_productos() -> str:
    return (
        "Ahora añade los productos uno a uno.\n"
        "Formato: nombre, cantidad, precio neto\n"
        "Ejemplo: Tomate entero, 12, 1.81\n\n"
        "Si el papel muestra todas las columnas, también puedes escribir:\n"
        "nombre | cantidad | tarifa | descuento | neto | importe\n"
        "Ejemplo: Tomate | 10 | 2,00 | 10 | 1,80 | 18,00\n\n"
        "Escribe FIN cuando termines o /corregir para borrar el último producto."
    )


def _step_productos(flow: dict, texto: str) -> str:
    if texto.lower() == "fin":
        if not flow["lineas"]:
            return "No has añadido ningún producto todavía. Añade al menos uno o escribe /cancelar."
        flow["step"] = "total"
        total = _total_lineas(flow)
        return (
            f"Base calculada de las líneas: {_fmt_importe(total)}\n\n"
            "Escribe una de estas opciones:\n"
            "• OK — no hay IVA y el total coincide.\n"
            "• IVA 3,78 — añade ese IVA a la base.\n"
            "• 98,43 — total final; la diferencia se tratará como IVA.\n"
            "• 94,65 / 3,78 / 98,43 — BASE / IVA / TOTAL.\n\n"
            "Si falta un porte, envase u otro cargo, escribe ATRÁS y añádelo como producto."
        )

    detailed = _parsear_producto_detallado(texto) if "|" in texto else None
    parsed = _parsear_producto(texto) if detailed is None and "|" not in texto else None
    if detailed is None and parsed is None:
        return (
            "No he entendido o las cifras no cuadran. Usa:\n"
            "nombre, cantidad, precio neto\n"
            "o nombre | cantidad | tarifa | descuento | neto | importe"
        )
    if detailed is not None:
        line = detailed
        nombre, cantidad, precio = line["nombre"], line["cantidad"], line["precio"]
    else:
        nombre, cantidad, precio = parsed
        line = {
            "nombre": nombre, "cantidad": cantidad, "precio": precio,
            "importe": round(cantidad * precio, 2), "entrada_detallada": False,
        }
    flow["lineas"].append(line)
    return f"✓ {nombre} × {_cant(cantidad)} a {_fmt_importe(precio)}\n\nAñade otro, o FIN para terminar."


def corregir_ultimo(chat_id: int) -> str:
    """/corregir — elimina la última línea añadida en el paso de productos."""
    flow = _manual_flows.get(chat_id)
    if not flow:
        return "No hay ninguna entrada manual en curso."
    flow["timestamp"] = datetime.now()
    if flow["step"] != "productos":
        return "/corregir solo sirve mientras añades productos."
    if not flow["lineas"]:
        return "No hay productos que borrar todavía."
    eliminado = flow["lineas"].pop()
    return f"Eliminado: {eliminado['nombre']}. Vuelve a escribir el producto correcto, o FIN."


def _cant(valor: float) -> str:
    texto = f"{valor:.3f}".rstrip("0").rstrip(".")
    return texto.replace(".", ",")


# ── Paso 5: total y forma de pago ───────────────────────────────────────────────

def _step_total(flow: dict, texto: str) -> str:
    if texto.casefold() in {"atrás", "atras", "productos"}:
        flow["step"] = "productos"
        return "Vuelve a añadir el cargo como una línea (por ejemplo: Portes, 1, 4,50). Escribe FIN al terminar."

    suma_lineas = _total_lineas(flow)
    totales = _parsear_totales(texto, suma_lineas)
    if totales is None:
        return (
            "Los importes no cuadran. La BASE debe coincidir con las líneas y BASE + IVA = TOTAL.\n"
            f"Base de líneas: {_fmt_importe(suma_lineas)}.\n"
            "Usa OK, IVA 3,78, un total final, o BASE / IVA / TOTAL."
        )
    base, iva, total = totales
    flow["base_manual"] = base
    flow["iva_manual"] = iva
    flow["total_manual"] = total
    flow["step"] = "forma_pago"
    return (
        f"Cuadre: {_fmt_importe(base)} + IVA {_fmt_importe(iva)} = {_fmt_importe(total)}.\n\n"
        "¿Forma de pago? (ej: 15 días, 30 días, contado)\nO escribe NO si no aplica."
    )


def _step_forma_pago(flow: dict, texto: str) -> str:
    if texto.lower() not in ("no", "n"):
        flow["forma_pago"] = texto.strip()
    # Si ya tenemos la foto (venía de un OCR fallido), saltamos el paso de foto.
    if flow.get("_imagen_bytes") or flow.get("_existing_evidence"):
        flow["step"] = "confirmacion"
        return _resumen(flow)
    flow["step"] = "foto"
    return (
        "¿Quieres añadir una foto del albarán para archivo?\n"
        "Mándala ahora o escribe NO."
    )


# ── Paso 6: foto opcional ───────────────────────────────────────────────────────

def _step_foto_texto(flow: dict, texto: str) -> str:
    if texto.lower() in ("no", "n"):
        flow["step"] = "confirmacion"
        return _resumen(flow)
    return "Manda la foto del albarán, o escribe NO para continuar sin foto."


async def manejar_foto(chat_id: int, imagen_bytes: bytes) -> str | None:
    """Procesa una foto recibida durante el flujo (paso 6). None si no aplica."""
    if _expirado(chat_id):
        _manual_flows.pop(chat_id, None)
        return "Entrada manual cancelada por inactividad."
    flow = _manual_flows.get(chat_id)
    if not flow or flow["step"] != "foto":
        return None
    flow["timestamp"] = datetime.now()
    try:
        _validate_image(imagen_bytes)
        flow["_imagen_bytes"] = imagen_bytes
        msg = "Foto preparada; se guardará al confirmar.\n\n"
    except Exception as e:
        logger.warning("No se pudo subir la foto del albarán manual: %s", e)
        msg = "No pude guardar la foto, pero seguimos sin ella.\n\n"
    flow["step"] = "confirmacion"
    return msg + _resumen(flow)


# ── Paso 7: confirmación e inserción ────────────────────────────────────────────

def _resumen(flow: dict) -> str:
    base = flow["base_manual"] if flow["base_manual"] is not None else _total_lineas(flow)
    iva = flow["iva_manual"] if flow["iva_manual"] is not None else 0.0
    total = flow["total_manual"] if flow["total_manual"] is not None else base + iva
    cabecera = [flow["proveedor_nombre"]]
    if flow["numero_albaran"]:
        cabecera.append(f"Nº {flow['numero_albaran']}")
    cabecera.append(flow["fecha"])
    if flow["forma_pago"]:
        cabecera.append(flow["forma_pago"])
    lineas = [
        "Resumen del albarán:",
        " | ".join(cabecera),
        f"{len(flow['lineas'])} productos",
        f"Base: {_fmt_importe(base)} + IVA: {_fmt_importe(iva)} = Total: {_fmt_importe(total)}",
        "",
        "Líneas:",
    ]
    for l in flow["lineas"]:
        if l.get("entrada_detallada"):
            lineas.append(
                f" · {l['nombre']} × {_cant(l['cantidad'])} | tarifa {_fmt_importe(l['precio_tarifa'])} "
                f"− {_cant(l['descuento_pct'])}% → neto {_fmt_importe(l['precio_neto_observado'])} "
                f"= {_fmt_importe(l['importe'])}"
            )
        else:
            lineas.append(f" · {l['nombre']} × {_cant(l['cantidad'])} a {_fmt_importe(l['precio'])}")
    if flow.get("imagen_url") or flow.get("_imagen_bytes") or flow.get("_existing_evidence"):
        lineas.append("📎 Con foto adjunta.")
    lineas.append("")
    lineas.append("Escribe OK para guardar o /cancelar para abortar.")
    return "\n".join(lineas)


async def _step_confirmacion(chat_id: int, flow: dict, texto: str) -> str:
    if texto.lower() == "nuevo":
        flow["_duplicate_override"] = True
    elif texto.lower() != "ok":
        return "Escribe OK para guardar el albarán o /cancelar para abortar."
    try:
        resultado = await _insertar(flow)
    except Exception as e:
        logger.error("Error insertando albarán manual: %s", e, exc_info=True)
        flow["timestamp"] = datetime.now()
        return (
            "No se ha guardado el albarán. Tus datos siguen disponibles durante "
            "15 minutos: escribe OK para reintentar o /cancelar."
        )

    if resultado.get("probable_duplicate"):
        duplicate = resultado["dup"]
        return (
            "Posible duplicado: ya existe un albarán del mismo proveedor, fecha y total "
            f"(Nº {duplicate.get('numero_albaran') or 'sin número'}).\n"
            "Si es otra entrega legítima escribe NUEVO; si no, usa /cancelar."
        )

    _manual_flows.pop(chat_id, None)
    if resultado.get("duplicado"):
        dup = resultado["dup"]
        fecha = (dup.get("creado_en") or "")[:10]
        return (
            "Este albarán ya estaba registrado (mismo proveedor, fecha y total).\n"
            f"No se ha duplicado. Original: Nº {dup.get('numero_albaran') or 'sin número'}"
            f"{f', registrado el {fecha}' if fecha else ''}."
        )
    total = resultado["total"]
    return (
        f"✓ Albarán manual guardado — {flow['proveedor_nombre']}\n"
        f"{len(flow['lineas'])} productos | Total: {_fmt_importe(total)}\n"
        "Registrado como entrada manual."
    )


async def _insertar(flow: dict) -> dict:
    return await _insertar_atomico(flow)


async def _insertar_atomico(flow: dict) -> dict:
    """Publica un alta manual mediante la misma transacción canónica que el OCR."""
    suma_lineas = _total_lineas(flow)
    total_base = flow["base_manual"] if flow["base_manual"] is not None else suma_lineas
    total_iva = flow["iva_manual"] if flow["iva_manual"] is not None else 0.0
    total = flow["total_manual"] if flow["total_manual"] is not None else total_base + total_iva
    if abs(total_base - suma_lineas) > 0.03 or abs(total_base + total_iva - total) > 0.03:
        raise ValueError("El desglose manual no cumple BASE = líneas y BASE + IVA = TOTAL")

    # El número del proveedor es identidad fuerte. Proveedor+fecha+total solo
    # detiene para comparar: dos entregas legítimas pueden coincidir en importe.
    if flow.get("proveedor_id") and flow.get("numero_albaran"):
        number_duplicate = await db.buscar_albaran_duplicado_norm(
            _normalizar_numero_albaran(flow["numero_albaran"]), flow["proveedor_id"]
        )
        if number_duplicate:
            return {"duplicado": True, "dup": number_duplicate, "total": total}
    probable = await db.buscar_albaran_duplicado_por_nombre_proveedor(
        flow["proveedor_nombre"], flow["fecha"], total
    )
    if probable and not flow.get("_duplicate_override"):
        return {"probable_duplicate": True, "dup": probable, "total": total}

    ingestion_id = flow.get("_durable_ingestion_id") or str(uuid.uuid4())
    durable_already_created = bool(flow.get("_durable_ingestion_id"))
    bucket = path = content_type = image_hash = None
    byte_size = 0
    image_bytes = flow.get("_imagen_bytes")
    if image_bytes and not durable_already_created:
        content_type, extension = _validate_image(image_bytes)
        image_hash = hashlib.sha256(image_bytes).hexdigest()
        bucket = settings.STORAGE_BUCKET
        path = f"manual/{flow.get('proveedor_id') or 'nuevo'}/{ingestion_id}.{extension}"
        await db.subir_original_privado(bucket, path, image_bytes, content_type)
        byte_size = len(image_bytes)
    idempotency_key = f"manual:{flow.get('_chat_id', 'telegram')}:{ingestion_id}"
    if durable_already_created:
        await db.actualizar_ingestion(
            ingestion_id, status="extracted", failed_at=None, duplicate_reason=None
        )
    if not durable_already_created:
        try:
            await db.crear_ingestion_manual(
                ingestion_id=ingestion_id, idempotency_key=idempotency_key,
                telegram_user_id=int(flow.get("_user_id") or flow.get("_chat_id") or 0),
                telegram_chat_id=int(flow.get("_chat_id") or flow.get("_user_id") or 0),
                storage_bucket=bucket, storage_path=path, content_type=content_type,
                byte_size=byte_size, image_hash=image_hash,
                metadata={"provider": flow["proveedor_nombre"], "date": flow["fecha"], "total": total},
            )
            flow["_durable_ingestion_id"] = ingestion_id
        except Exception:
            if bucket and path:
                try:
                    await db.borrar_original_privado(bucket, path)
                except Exception:
                    logger.exception("No se pudo retirar el original huérfano %s", path)
            raise
    header = {
        "proveedor_id": flow.get("proveedor_id"),
        "proveedor_nombre": flow["proveedor_nombre"],
        "proveedor_nif": (flow.get("_nuevo") or {}).get("nif"),
        "forma_pago_habitual": (flow.get("_nuevo") or {}).get("forma_pago"),
        "numero_albaran": flow["numero_albaran"],
        "fecha": flow["fecha"],
        "forma_pago": flow["forma_pago"],
        "base_imponible": total_base,
        "total_iva": total_iva,
        "total": total,
        "detalle_iva": None,
        "origen": "manual",
    }
    if not header["proveedor_id"]:
        header.pop("proveedor_id")
    lines = [{
        "descripcion_original": line["nombre"],
        "descripcion_limpia": line["nombre"],
        "cantidad": line["cantidad"],
        "unidad": line.get("unidad") or "ud",
        "precio_unitario": line["precio"],
        "importe_neto": round(
            line.get("importe") if line.get("importe") is not None
            else line["cantidad"] * line["precio"], 2
        ),
        "descuento_pct": line.get("descuento_pct"),
        "confianza": 100,
        "valores_observados": {
            "source": "manual", "precio_tarifa": line.get("precio_tarifa"),
            "descuento_pct": line.get("descuento_pct"),
            "precio_neto": line.get("precio_neto_observado", line["precio"]),
            "importe_neto": line.get("importe"),
        },
        "valores_calculados": (
            {"precio_unitario_aceptado_desde_importe": line["precio"]}
            if line.get("entrada_detallada") else {}
        ),
        "decisiones": {
            "accepted_by_user": True,
            "input_mode": "detailed" if line.get("entrada_detallada") else "net_fast",
        },
    } for line in flow["lineas"]]
    try:
        attempt = await db.siguiente_intento_extraccion(ingestion_id)
        artifact = await db.registrar_artefacto_extraccion(
            ingestion_id=ingestion_id, attempt=attempt, artifact_type="candidate",
            payload={"header": header, "lines": lines, "observed": {"source": "manual"}},
            prompt_version="manual-v1", complete=True,
        )
        if flow.get("_source_status") in {"extracted", "needs_review"}:
            await db.resolver_revisiones_abiertas(
                ingestion_id, status="rejected",
                resolved_by=f"telegram_user:{flow.get('_user_id') or flow.get('_chat_id')}",
                note="Candidato OCR sustituido por transcripción manual del mismo original",
            )
            await db.registrar_evento_auditoria(
                "candidate.replaced_by_manual", ingestion_id=ingestion_id,
                actor_type="telegram_user",
                actor_id=str(flow.get("_user_id") or flow.get("_chat_id") or "unknown"),
                data={"manual_artifact_id": artifact["id"]},
            )
        result = await db.confirmar_albaran_atomico(
            ingestion_id=ingestion_id, idempotency_key=idempotency_key,
            actor_type="telegram_user",
            actor_id=str(flow.get("_user_id") or flow.get("_chat_id") or "unknown"),
            albaran=header, lineas=lines, extraction_artifact_id=artifact["id"],
        )
    except Exception:
        from datetime import timezone
        await db.actualizar_ingestion(
            ingestion_id, status="failed", failed_at=datetime.now(timezone.utc).isoformat(),
        )
        await db.registrar_evento_auditoria(
            "manual.confirmation_failed", ingestion_id=ingestion_id,
            actor_type="telegram_user",
            actor_id=str(flow.get("_user_id") or flow.get("_chat_id") or "unknown"),
        )
        raise
    return {"duplicado": False, "albaran": {"id": result["albaran_id"]}, "total": total}
