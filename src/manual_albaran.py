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
from datetime import datetime, timedelta

from . import supabase_client as db
from .albaran_processor import _normalizar_numero_albaran, _parsear_numero

logger = logging.getLogger(__name__)

# Estado por chat_id. Estructura del flujo (ver docstring del módulo).
_manual_flows: dict[int, dict] = {}
# Foto cuyo OCR falló, a la espera de que el usuario decida meterla a mano (botón inline).
_foto_pendiente: dict[int, bytes] = {}
_TIMEOUT = timedelta(minutes=15)


def recordar_foto_fallida(chat_id: int, imagen_bytes: bytes) -> None:
    """Guarda la foto cuyo OCR falló para reaprovecharla si el usuario elige meterla a mano."""
    _foto_pendiente[chat_id] = imagen_bytes

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


def _total_lineas(flow: dict) -> float:
    return round(sum(l["cantidad"] * l["precio"] for l in flow["lineas"]), 2)


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

async def iniciar(chat_id: int) -> str:
    proveedores = await db.listar_todos_proveedores()
    flow = {
        "step": "proveedor",
        "proveedor_id": None,
        "proveedor_nombre": None,
        "numero_albaran": None,
        "fecha": None,
        "lineas": [],
        "forma_pago": None,
        "total_manual": None,
        "imagen_url": None,
        "timestamp": datetime.now(),
        "_proveedores": proveedores,
        "_nuevo": None,
        # Si venimos de una foto cuyo OCR falló, la reaprovechamos (se adjunta al final).
        "_imagen_bytes": _foto_pendiente.pop(chat_id, None),
    }
    _manual_flows[chat_id] = flow

    if proveedores:
        listado = "\n".join(f"{i}. {p['nombre']}" for i, p in enumerate(proveedores, 1))
        cuerpo = f"Proveedores registrados:\n{listado}\n\nEscribe el número o el nombre si es uno nuevo."
    else:
        cuerpo = "No hay proveedores registrados todavía. Escribe el nombre del proveedor."
    nota_foto = "📎 Usaré la foto que enviaste como archivo del albarán.\n\n" if flow["_imagen_bytes"] else ""
    return (
        "Vamos a registrar un albarán manualmente.\n"
        "(escribe /cancelar en cualquier momento para abortar)\n\n"
        f"{nota_foto}¿De qué proveedor es?\n\n{cuerpo}"
    )


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
        flow["_nuevo"]["nif"] = texto.strip()
    flow["step"] = "nuevo_pago"
    return "¿Forma de pago habitual? (ej: 15 días, 30 días, contado)"


async def _step_nuevo_pago(chat_id: int, flow: dict, texto: str) -> str:
    if texto.lower() not in ("no", "n", ""):
        flow["_nuevo"]["forma_pago"] = texto.strip()
    nuevo = flow["_nuevo"]
    proveedor, _ = await db.buscar_o_crear_proveedor(
        nombre=nuevo["nombre"],
        nif=nuevo["nif"],
        forma_pago_habitual=nuevo["forma_pago"],
    )
    flow["proveedor_id"] = proveedor["id"]
    flow["proveedor_nombre"] = proveedor["nombre"]
    flow["forma_pago"] = nuevo["forma_pago"]  # se podrá sobrescribir después
    flow["step"] = "cabecera"
    return f"Proveedor «{proveedor['nombre']}» registrado.\n\n" + _pedir_cabecera(proveedor["nombre"])


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
        "Escribe FIN cuando termines o /corregir para borrar el último producto."
    )


def _step_productos(flow: dict, texto: str) -> str:
    if texto.lower() == "fin":
        if not flow["lineas"]:
            return "No has añadido ningún producto todavía. Añade al menos uno o escribe /cancelar."
        flow["step"] = "total"
        total = _total_lineas(flow)
        return (
            f"Total calculado de las líneas: {_fmt_importe(total)}\n"
            "¿Es correcto? Escribe el total real si es diferente, o OK si coincide."
        )

    parsed = _parsear_producto(texto)
    if not parsed:
        return (
            "No he entendido el producto. Usa el formato:\n"
            "  nombre, cantidad, precio neto\n"
            "Ejemplo: Tomate entero, 12, 1.81"
        )
    nombre, cantidad, precio = parsed
    flow["lineas"].append({"nombre": nombre, "cantidad": cantidad, "precio": precio})
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
    if texto.lower() != "ok":
        valor = _num(texto)
        if valor is None or valor <= 0:
            return "Escribe el total real (ej: 103,08) o OK si el calculado es correcto."
        flow["total_manual"] = valor
    flow["step"] = "forma_pago"
    return "¿Forma de pago? (ej: 15 días, 30 días, contado)\nO escribe NO si no aplica."


def _step_forma_pago(flow: dict, texto: str) -> str:
    if texto.lower() not in ("no", "n"):
        flow["forma_pago"] = texto.strip()
    # Si ya tenemos la foto (venía de un OCR fallido), saltamos el paso de foto.
    if flow.get("_imagen_bytes"):
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
    ruta = f"albaranes/manual/{chat_id}/{uuid.uuid4().hex}.jpg"
    try:
        flow["imagen_url"] = await db.subir_imagen("albaranes", ruta, imagen_bytes)
        msg = "Foto guardada.\n\n"
    except Exception as e:
        logger.warning("No se pudo subir la foto del albarán manual: %s", e)
        msg = "No pude guardar la foto, pero seguimos sin ella.\n\n"
    flow["step"] = "confirmacion"
    return msg + _resumen(flow)


# ── Paso 7: confirmación e inserción ────────────────────────────────────────────

def _resumen(flow: dict) -> str:
    total = flow["total_manual"] if flow["total_manual"] is not None else _total_lineas(flow)
    cabecera = [flow["proveedor_nombre"]]
    if flow["numero_albaran"]:
        cabecera.append(f"Nº {flow['numero_albaran']}")
    cabecera.append(flow["fecha"])
    if flow["forma_pago"]:
        cabecera.append(flow["forma_pago"])
    lineas = [
        "Resumen del albarán:",
        " | ".join(cabecera),
        f"{len(flow['lineas'])} productos | Total: {_fmt_importe(total)}",
        "",
        "Líneas:",
    ]
    for l in flow["lineas"]:
        lineas.append(f" · {l['nombre']} × {_cant(l['cantidad'])} a {_fmt_importe(l['precio'])}")
    if flow.get("imagen_url") or flow.get("_imagen_bytes"):
        lineas.append("📎 Con foto adjunta.")
    lineas.append("")
    lineas.append("Escribe OK para guardar o /cancelar para abortar.")
    return "\n".join(lineas)


async def _step_confirmacion(chat_id: int, flow: dict, texto: str) -> str:
    if texto.lower() != "ok":
        return "Escribe OK para guardar el albarán o /cancelar para abortar."
    try:
        resultado = await _insertar(flow)
    except Exception as e:
        logger.error("Error insertando albarán manual: %s", e, exc_info=True)
        _manual_flows.pop(chat_id, None)
        return f"Error al guardar el albarán: {e}"

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


async def _detectar_duplicado(proveedor_id: str, proveedor_nombre: str, fecha: str,
                              total: float, numero: str | None) -> dict | None:
    """Misma detección que el pipeline OCR: proveedor+fecha+total, nombre+fecha+total, número."""
    dup = await db.buscar_albaran_duplicado_combinacion(proveedor_id, fecha, total)
    if dup:
        return dup
    dup = await db.buscar_albaran_duplicado_por_nombre_proveedor(proveedor_nombre, fecha, total)
    if dup:
        return dup
    numero_norm = _normalizar_numero_albaran(numero or "")
    if numero_norm:
        dup = await db.buscar_albaran_duplicado_norm(numero_norm, proveedor_id)
        if dup:
            return dup
    return None


async def _insertar(flow: dict) -> dict:
    proveedor_id = flow["proveedor_id"]
    total = flow["total_manual"] if flow["total_manual"] is not None else _total_lineas(flow)

    # Subir la foto reaprovechada (OCR fallido) si aún no se ha subido.
    if not flow.get("imagen_url") and flow.get("_imagen_bytes"):
        try:
            ruta = f"albaranes/manual/{uuid.uuid4().hex}.jpg"
            flow["imagen_url"] = await db.subir_imagen("albaranes", ruta, flow["_imagen_bytes"])
        except Exception as e:
            logger.warning("No se pudo subir la foto reaprovechada del albarán manual: %s", e)

    # Detección de duplicados ANTES de insertar (req: los manuales SÍ se comprueban)
    dup = await _detectar_duplicado(proveedor_id, flow["proveedor_nombre"], flow["fecha"], total, flow["numero_albaran"])
    if dup:
        return {"duplicado": True, "dup": dup}

    albaran = await db.insertar_albaran(
        proveedor_id=proveedor_id,
        numero_albaran=flow["numero_albaran"],
        fecha=flow["fecha"],
        forma_pago=flow["forma_pago"],
        base_imponible=None,
        total_iva=None,
        total=total,
        imagen_url=flow.get("imagen_url"),
        origen="manual",
    )

    lineas_insert = []
    productos = []
    for l in flow["lineas"]:
        prod = await db.buscar_o_crear_producto_catalogo(
            proveedor_id=proveedor_id,
            nombre_normalizado=l["nombre"],
        )
        productos.append(prod)
        lineas_insert.append({
            "albaran_id": albaran["id"],
            "producto_catalogo_id": prod["id"],
            "descripcion_original": l["nombre"],
            "descripcion_limpia": l["nombre"],
            "cantidad": l["cantidad"],
            "unidad": None,
            "precio_unitario": l["precio"],
            "importe_neto": round(l["cantidad"] * l["precio"], 2),
            "confianza": 100,
            "requiere_revision": False,
        })
    await db.insertar_lineas(lineas_insert)

    # Mantener precios de catálogo al día (como en el pipeline OCR) para las consultas
    for prod, l in zip(productos, flow["lineas"]):
        try:
            await db.actualizar_precio_catalogo(prod["id"], l["precio"])
        except Exception as e:
            logger.warning("No se pudo actualizar precio de %s: %s", l["nombre"], e)

    return {"duplicado": False, "albaran": albaran, "total": total}
