"""
Pipeline completo de procesamiento de albaranes:
OCR (Mistral) → extracción estructurada (LLM) → validación → guardado en Supabase.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import date as _date
from datetime import datetime
from typing import Any

from mistralai.client.sdk import Mistral
from pydantic import BaseModel, field_validator, model_validator

from .config import settings
from . import supabase_client as db
from .product_normalizer import normalizar_productos_batch, invalidar_cache_proveedor

logger = logging.getLogger(__name__)

_MODELO_OCR = "mistral-ocr-latest"
_MODELO_LLM = "mistral-small-2506"

# Coste estimado por token (USD) — mistral-small-2506
_COSTE_INPUT_POR_TOKEN = 0.0000002
_COSTE_OUTPUT_POR_TOKEN = 0.0000006

_BLACKLIST = [
    "nómina", "nomina", "salario", "sueldo bruto",
    "factura de luz", "endesa", "iberdrola", "naturgy",
    "gas natural", "suministro eléctrico",
    "alquiler", "arrendamiento",
    "extracto bancario", "movimientos de cuenta",
]


def _normalizar_numero_albaran(numero: str) -> str:
    return re.sub(r'[^a-z0-9]', '', numero.lower().strip())


def _verificar_blacklist(texto: str) -> str | None:
    texto_lower = texto.lower()
    for palabra in _BLACKLIST:
        if palabra in texto_lower:
            return palabra
    return None


def _validar_datos_minimos(albaran: "AlbaranLLM") -> tuple[bool, str]:
    if not albaran.proveedor_nombre or not albaran.proveedor_nombre.strip():
        return False, "nombre de proveedor vacío"
    try:
        fecha = _date.fromisoformat(albaran.fecha)
        if fecha > _date.today():
            return False, f"fecha futura ({albaran.fecha})"
    except Exception:
        return False, f"fecha inválida ({albaran.fecha})"
    if not albaran.lineas:
        return False, "sin líneas de productos"
    if albaran.total is not None and albaran.total <= 0:
        return False, f"total inválido ({albaran.total})"
    if not any(l.precio_unitario and l.precio_unitario > 0 for l in albaran.lineas):
        return False, "ninguna línea con precio válido"
    return True, ""


# ── Modelos de datos ──────────────────────────────────────────────────────────

class LineaAlbaranLLM(BaseModel):
    nombre_producto: str
    descripcion_original: str | None = None
    cantidad: float
    unidad: str | None = None
    precio_unitario: float | None = None
    precio_tarifa: float | None = None   # columna TARIFA / precio de lista (bruto), transitorio
    precio_neto: float | None = None     # columna NETO / PRECIO FINAL explícita, transitorio
    importe_neto: float | None = None
    peso_unitario_g: float | None = None
    unidades_por_envase: int | None = None
    peso_total_kg: float | None = None
    volumen_unitario_l: float | None = None
    formato_envase: str | None = None
    numero_lote: str | None = None
    caducidad: str | None = None
    descuento_pct: float | None = None
    confianza: int = 100

    @field_validator("confianza", mode="before")
    @classmethod
    def limpiar_confianza(cls, v: Any) -> int:
        try:
            n = int(float(str(v)))
            return max(0, min(100, n))
        except Exception:
            return 100

    @field_validator("cantidad", mode="before")
    @classmethod
    def cantidad_positiva(cls, v: Any) -> float:
        v = _parsear_numero(v)
        if v is None or v <= 0:
            raise ValueError("cantidad debe ser > 0")
        return v

    @field_validator("precio_unitario", "precio_tarifa", "precio_neto", "importe_neto", "peso_unitario_g", "peso_total_kg", "volumen_unitario_l", "descuento_pct", mode="before")
    @classmethod
    def limpiar_numerico(cls, v: Any) -> float | None:
        return _parsear_numero(v)

    @field_validator("unidades_por_envase", mode="before")
    @classmethod
    def limpiar_entero(cls, v: Any) -> int | None:
        n = _parsear_numero(v)
        return int(n) if n is not None else None


class DetalleIvaLLM(BaseModel):
    tipo: float
    base: float
    cuota: float


class AlbaranLLM(BaseModel):
    proveedor_nombre: str
    proveedor_nif: str | None = None
    proveedor_direccion: str | None = None
    proveedor_telefono: str | None = None
    proveedor_email: str | None = None
    numero_albaran: str | None = None
    fecha: str
    forma_pago: str | None = None
    base_imponible: float | None = None
    total_iva: float | None = None
    total: float | None = None
    detalle_iva: list[DetalleIvaLLM] | None = None
    lineas: list[LineaAlbaranLLM]

    @field_validator("fecha", mode="before")
    @classmethod
    def normalizar_fecha(cls, v: str) -> str:
        if not v:
            return datetime.now().strftime("%Y-%m-%d")
        for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y", "%d-%m-%y"):
            try:
                return datetime.strptime(str(v).strip(), fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        logger.warning("Fecha no reconocida: %s — usando fecha actual", v)
        return datetime.now().strftime("%Y-%m-%d")

    @field_validator("base_imponible", "total_iva", "total", mode="before")
    @classmethod
    def limpiar_importe(cls, v: Any) -> float | None:
        return _parsear_numero(v)

    @model_validator(mode="after")
    def proveedor_nombre_no_vacio(self) -> "AlbaranLLM":
        if not self.proveedor_nombre or not self.proveedor_nombre.strip():
            raise ValueError("proveedor_nombre no puede estar vacío")
        return self


class ResultadoProcesamiento(BaseModel):
    albaran_id: str
    proveedor_id: str
    proveedor_nombre: str
    numero_albaran: str | None
    fecha: str
    forma_pago: str | None
    total: float | None
    num_lineas: int
    es_proveedor_nuevo: bool
    es_duplicado: bool
    es_duplicado_fecha: str | None = None      # creado_en del registro original (ISO)
    es_duplicado_numero_original: str | None = None  # numero_albaran del registro original
    lineas_con_revision: int
    alertas_precio: list[dict]  # [{"producto": str, "anterior": float, "nuevo": float, "pct": float}]
    imagen_url: str | None
    lineas_para_confirmacion: list[dict] = []
    # cada dict: {"num": int, "linea_id": str, "descripcion": str, "cantidad": float, "precio": float|None, "unidad": str|None, "razon": str}


# ── Utilidades ────────────────────────────────────────────────────────────────

def _parsear_numero(v: Any) -> float | None:
    """Parsea números en formato es-ES, distinguiendo separador de miles del decimal.

    '1.234,56' → 1234.56 (punto = miles, coma = decimal)
    '1,234.56' → 1234.56 (coma = miles, punto = decimal)
    '46,75'    → 46.75   |  '1.81' → 1.81  |  '1234' → 1234
    Sin esta distinción, '1.234,56' se rompía (→ None) y se perdían precios ≥ 1.000€.
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.replace("€", "").replace(" ", "").strip()
        if not s:
            return None
        if "," in s and "." in s:
            # El separador que aparece más a la derecha es el decimal.
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")   # punto = miles
            else:
                s = s.replace(",", "")                       # coma = miles
        elif "," in s:
            s = s.replace(",", ".")                          # coma decimal
        # Solo puntos o sin separador → se asume punto decimal (se deja igual).
        try:
            return float(s)
        except ValueError:
            return None
    return None


async def _con_reintento(func: Callable, *args: Any, max_intentos: int = 3, **kwargs: Any) -> Any:
    ultimo_error: Exception | None = None
    for i in range(max_intentos):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            ultimo_error = e
            if i < max_intentos - 1:
                espera = 2 ** i
                logger.warning("Intento %d/%d fallido: %s — reintentando en %ds", i + 1, max_intentos, e, espera)
                await asyncio.sleep(espera)
    raise ultimo_error


# ── Resolución determinista del precio neto ─────────────────────────────────────

def _cantidad_facturable(linea: "LineaAlbaranLLM") -> float:
    """Cantidad sobre la que se calcula el importe de la línea (mejor estimación).

    Si el producto se factura por peso (hay peso_total_kg y la unidad NO es ya kg —
    p.ej. carnes vendidas como "2 uds" pero cobradas por 18,60 kg), el importe se
    calcula sobre los kg, no sobre las unidades. En el resto de casos, la cantidad.
    """
    if linea.peso_total_kg and linea.peso_total_kg > 0 and linea.unidad != "kg":
        return linea.peso_total_kg
    return linea.cantidad


def _bases_importe(linea: "LineaAlbaranLLM") -> list[float]:
    """
    Cantidades plausibles sobre las que el albarán pudo calcular el importe de la línea.
    Una línea cuadra si su importe coincide con precio × (alguna de estas bases):
      - cantidad (uds o kg directos),
      - peso_total_kg (columna KGRS cuando el producto se cobra por peso),
      - peso_unitario_g/1000 × cantidad (cubo/bandeja de N kg cobrado por kg, p.ej.
        "Queso cubo 3,5kg" → 1 ud × 3,5 kg).
    """
    bases: list[float] = []
    if linea.cantidad:
        bases.append(linea.cantidad)
    if linea.peso_total_kg and linea.peso_total_kg > 0:
        bases.append(linea.peso_total_kg)
    if linea.peso_unitario_g and linea.peso_unitario_g > 0:
        bases.append(linea.peso_unitario_g / 1000.0 * (linea.cantidad or 1))
    return bases or [linea.cantidad or 0]


def _resolver_precio_neto(linea: "LineaAlbaranLLM") -> None:
    """Fija precio_unitario (neto) e importe_neto de forma determinista, mutando la línea.

    Regla "el neto prevalece SIEMPRE":
      1. Si hay columna NETO explícita (precio_neto) → ese es el precio_unitario.
      2. Si no, pero hay descuento_pct > 0 y precio_tarifa → calcular tarifa × (1 - dto/100).
      3. Si no → usar precio_tarifa (o el precio_unitario que viniera).
    No depende de que el LLM "elija" la columna correcta: solo de que transcriba lo que ve.

    Para importe_neto: el valor IMPRESO en el albarán es la verdad. Solo se sobreescribe
    cuando falta o cuando es claramente el importe BRUTO (tarifa × cantidad), nunca por el
    simple hecho de no coincidir con neto × cantidad (eso rompía las líneas por kg).
    """
    tarifa = linea.precio_tarifa if linea.precio_tarifa and linea.precio_tarifa > 0 else linea.precio_unitario
    dto = linea.descuento_pct or 0.0

    if linea.precio_neto and linea.precio_neto > 0:
        neto = linea.precio_neto
    elif tarifa and dto > 0:
        neto = round(tarifa * (1 - dto / 100.0), 4)
    else:
        neto = tarifa

    linea.precio_unitario = neto

    if not neto:
        return

    qty = _cantidad_facturable(linea)
    esperado_neto = round(neto * qty, 2) if qty else None

    # 1) Importe ausente → calcular desde el neto.
    if not linea.importe_neto or linea.importe_neto <= 0:
        linea.importe_neto = esperado_neto
    # 2) Importe impreso ya cuadra con el neto → conservar tal cual (valor impreso).
    elif esperado_neto and abs(linea.importe_neto - esperado_neto) / max(esperado_neto, 0.01) <= 0.02:
        pass
    # 3) Importe impreso == BRUTO (tarifa × cantidad) → reemplazar por el neto.
    elif tarifa and qty and round(tarifa * qty, 2) and \
            abs(linea.importe_neto - round(tarifa * qty, 2)) / max(round(tarifa * qty, 2), 0.01) <= 0.02:
        linea.importe_neto = esperado_neto
    # 4) En cualquier otro caso, conservar el importe impreso; la validación decidirá.

    # En TODAS las rutas: alinear cantidad/unidad si la línea se cobra por peso.
    _alinear_cantidad_unidad(linea)


def _alinear_cantidad_unidad(linea: "LineaAlbaranLLM") -> None:
    """
    Si la línea se cobra por PESO pero el LLM dejó cantidad en uds (p.ej. Cordero
    '2 ud' que en realidad son 18,60 kg, o 'Queso cubo 3,5 kg' como '1 ud'),
    reescribe cantidad = kg y unidad = 'kg' para que en BD se cumpla SIEMPRE
    precio_unitario × cantidad = importe_neto y las consultas por kg sean correctas.
    """
    if not (linea.precio_unitario and linea.precio_unitario > 0 and linea.importe_neto):
        return
    qty_real = linea.importe_neto / linea.precio_unitario
    if not linea.cantidad or abs(qty_real - linea.cantidad) / linea.cantidad <= 0.02:
        return  # la cantidad ya cuadra con el importe
    peso_g = (linea.peso_unitario_g / 1000.0 * (linea.cantidad or 1)) if linea.peso_unitario_g else None
    for cand in (linea.peso_total_kg, peso_g):
        if cand and cand > 0 and abs(qty_real - cand) / cand <= 0.02:
            linea.cantidad = round(cand, 3)
            linea.unidad = "kg"
            if not linea.peso_total_kg or linea.peso_total_kg <= 0:
                linea.peso_total_kg = round(cand, 3)
            return


# ── Validación de línea ───────────────────────────────────────────────────────

def _reconciliar_lineas_total(albaran: "AlbaranLLM") -> tuple[bool, float]:
    """
    Comprueba si la suma de importes netos de línea (= base imponible) cuadra con el
    documento, TOLERANDO el IVA. La suma de líneas es SIN IVA, mientras que `total` lo
    incluye; comparar directamente daba falsos positivos masivos.

    Acepta si la suma cuadra (±5%) con CUALQUIERA de los objetivos plausibles:
      - base_imponible (cuando el LLM lo leyó bien),
      - total - total_iva,
      - total (albaranes sin IVA).
    Devuelve (cuadra, suma_lineas).
    """
    suma = round(sum(l.importe_neto or 0 for l in albaran.lineas), 2)
    if suma <= 0:
        return True, suma  # sin importes que comparar (p.ej. manuscrito sin importes)

    # Sin TOTAL impreso no reconciliamos: base_imponible sola es señal débil (el LLM la
    # inventa/lee mal en albaranes internos sin pie de totales). Las líneas ya se validan
    # una a una en _validar_linea, así que un error real de línea se detecta igual.
    if not albaran.total or albaran.total <= 0:
        return True, suma

    objetivos: list[float] = [albaran.total]
    if albaran.total_iva:
        objetivos.append(albaran.total - albaran.total_iva)
    if albaran.base_imponible and albaran.base_imponible > 0:
        objetivos.append(albaran.base_imponible)

    mejor = min(abs(suma - o) / o for o in objetivos if o > 0)
    return mejor <= 0.05, suma


def _validar_linea(linea: "LineaAlbaranLLM") -> tuple[bool, str]:
    """Retorna (ok, motivo). Si ok=False, la línea necesita revisión."""
    if linea.precio_unitario is not None and linea.precio_unitario <= 0:
        return False, "precio_unitario inválido"
    if linea.cantidad <= 0:
        return False, "cantidad inválida"
    if not linea.nombre_producto or not linea.nombre_producto.strip():
        return False, "nombre producto vacío"
    # Detección del bug silencioso: hay descuento pero el neto sigue igual a la tarifa bruta.
    if (
        linea.descuento_pct and linea.descuento_pct > 0
        and linea.precio_tarifa and linea.precio_unitario
        and abs(linea.precio_unitario - linea.precio_tarifa) / linea.precio_tarifa < 0.005
    ):
        return False, f"descuento {linea.descuento_pct}% no aplicado (neto = tarifa)"
    if linea.precio_unitario and linea.importe_neto and linea.importe_neto > 0:
        # precio_unitario SIEMPRE es el neto. La línea cuadra si el importe coincide con
        # precio × (cualquier base plausible de cantidad/peso); así una línea cobrada por
        # kg no se marca por error solo porque la unidad sea "ud".
        mejor = min(abs(linea.precio_unitario * b - linea.importe_neto) for b in _bases_importe(linea))
        if mejor / linea.importe_neto > 0.05:
            esperado = linea.precio_unitario * _cantidad_facturable(linea)
            return False, f"importe no cuadra ({esperado:.2f} calculado vs {linea.importe_neto:.2f} en albarán)"
    return True, ""


# ── OCR ───────────────────────────────────────────────────────────────────────

_EXTRACTION_SYSTEM_PROMPT = """\
Eres un experto en extracción de datos de albaranes de restaurante español.
Extrae TODOS los datos del albarán y devuelve JSON con esta estructura exacta:
{
  "proveedor_nombre": "nombre del proveedor",
  "proveedor_nif": "NIF/CIF o null",
  "proveedor_direccion": "dirección completa del proveedor o null",
  "proveedor_telefono": "teléfono del proveedor o null",
  "proveedor_email": "email del proveedor o null",
  "numero_albaran": "número o null",
  "fecha": "DD/MM/YYYY",
  "forma_pago": "forma de pago o null",
  "base_imponible": suma de todas las bases imponibles o null,
  "total_iva": suma de todas las cuotas IVA o null,
  "total": número total del albarán o null,
  "detalle_iva": array con los tipos de IVA desglosados, o null si no aparecen:
    [{"tipo": 10, "base": 307.53, "cuota": 30.75}, {"tipo": 4, "base": 30.87, "cuota": 1.23}],
  "lineas": [
    {
      "nombre_producto": "nombre del producto",
      "descripcion_original": "descripción completa tal como aparece",
      "cantidad": número,
      "unidad": "kg" | "ud" | "l" | "caja" según corresponda,
      "precio_tarifa": precio de la columna TARIFA / precio de lista (bruto, antes de descuento) o null,
      "precio_neto": precio de la columna NETO / PRECIO FINAL si aparece explícita en el albarán, o null si no existe esa columna,
      "precio_unitario": copia aquí el mismo valor de precio_neto si existe; si no, copia precio_tarifa,
      "importe_neto": importe total de la línea tal como aparece o null,
      "peso_unitario_g": gramos por unidad si aparece (ej: 150g → 150) o null,
      "unidades_por_envase": unidades si aparece (ej: (50 unid) → 50) o null,
      "peso_total_kg": peso total en kg o null,
      "volumen_unitario_l": litros por unidad si el producto es líquido (ej: garrafa 25L → 25) o null,
      "formato_envase": "garrafa/cubo/bandeja/bolsa/lata/bote/etc" o null,
      "numero_lote": "lote si aparece o null",
      "caducidad": "DD/MM/YYYY si aparece o null",
      "descuento_pct": porcentaje de descuento (solo informativo) o null,
      "confianza": número entre 0-100 indicando la certeza de extracción
    }
  ]
}

REGLA CRÍTICA — IVA DESGLOSADO:
Si el albarán muestra varios tipos de IVA, extrae cada tramo por separado en detalle_iva.
Ejemplo: "IVA 10% 307,53€ = 30,75€" y "IVA 4% 30,87€ = 1,23€" →
  detalle_iva: [{"tipo": 10, "base": 307.53, "cuota": 30.75}, {"tipo": 4, "base": 30.87, "cuota": 1.23}]
  base_imponible: 338.40 (suma de 307.53 + 30.87)
  total_iva: 31.98 (suma de 30.75 + 1.23)
Si solo hay un tipo, igual extráelo: [{"tipo": 10, "base": 307.53, "cuota": 30.75}]
Si no aparece desglose de IVA, pon detalle_iva: null.

REGLA CRÍTICA — PRECIOS POR COLUMNA (MUY IMPORTANTE):
NO decidas ni calcules el precio neto. Tu trabajo es SOLO TRANSCRIBIR cada columna a su campo:
  - columna TARIFA / precio de lista / precio bruto  → precio_tarifa
  - columna DTO% / descuento                          → descuento_pct
  - columna NETO / PRECIO NETO / PRECIO FINAL          → precio_neto (solo si existe esa columna)

Copia los números TAL CUAL los ves, sin hacer cuentas. El sistema aplicará el descuento después.

Si el albarán tiene columnas separadas (TARIFA / DTO% / NETO o similar):
  Ejemplo: TARIFA=7,74 | DTO=15% | NETO=6,58 →
    precio_tarifa: 7.74, descuento_pct: 15, precio_neto: 6.58, precio_unitario: 6.58

Si solo hay un precio y un descuento (SIN columna neto explícita):
  → precio_tarifa: el precio que ves, descuento_pct: el dto, precio_neto: null
  Ejemplo: precio 2,01€ con 10% dto → precio_tarifa: 2.01, descuento_pct: 10, precio_neto: null, precio_unitario: 2.01

Si solo hay un precio y ningún descuento:
  → precio_tarifa: el precio, descuento_pct: null, precio_neto: null, precio_unitario: el precio

NUNCA inventes una columna NETO si no aparece: en ese caso precio_neto debe ser null.

REGLA CRÍTICA — COLUMNAS DE PESO (KGRS / KG / KILOS / PESO):
Si el albarán tiene una columna llamada KGRS, KG, KILOS, PESO o similar:
  - Ese valor es el peso real de la línea.
  - Pon ese valor en AMBOS campos: cantidad Y peso_total_kg.
  - Pon unidad = "kg".
  Ejemplo: columna KGRS = 12.000 → cantidad: 12.0, peso_total_kg: 12.0, unidad: "kg"

REGLA CRÍTICA — INFERENCIA DE UNIDAD DESDE DESCRIPCIÓN:
Analiza el nombre del producto y las unidades indicadas. NO uses siempre "ud":
  - unidad = "kg": carnes, pescados, embutidos, quesos a granel, verduras, frutas,
    aceites a granel, legumbres a granel, harina a granel. Cualquier alimento por peso.
  - unidad = "ud": latas, botes, cajas, paquetes, bolsas con peso fijo, botellas contables.
  - unidad = "l": líquidos vendidos en litros. Si la descripción contiene "L", "litros",
    "garrafa Xl", extrae volumen_unitario_l.

REGLA CRÍTICA — EXTRACCIÓN DE VOLUMEN PARA LÍQUIDOS:
Si la descripción contiene un volumen (ej: "25L", "5 litros", "garrafa 25L"):
  - unidad = "l"
  - volumen_unitario_l = ese número (ej: 25)
  - formato_envase = "garrafa" si corresponde
  Ejemplo: "Aceite Alto Oleico F40% Frimasol 25L" →
    nombre_producto: "Aceite Alto Oleico F40% Frimasol"
    unidad: "l", volumen_unitario_l: 25, formato_envase: "garrafa"

REGLA CRÍTICA — QUIÉN ES EL PROVEEDOR (MUY IMPORTANTE):
El albarán lo EMITE el proveedor y lo RECIBE el restaurante (cliente).
proveedor_nombre = la empresa que VENDE y ENVÍA los productos = quien emite el documento.
  - Suele aparecer en la cabecera con su logotipo, razón social, NIF y dirección propios.
  - Puede aparecer como "Emisor:", "Vendedor:", o simplemente en el membrete.
  - NO es el campo "Destinatario:", "Cliente:", "A/A:", "Facturar a:", "Entregar a:".
  - Si ves dos empresas, la que EMITE el documento es el proveedor; la que lo RECIBE es el cliente.
  proveedor_nif es el CIF/NIF del PROVEEDOR (emisor), NOT el del cliente o destinatario.
  El NIF del proveedor aparece en la cabecera junto a su nombre y dirección.
  Si el único NIF visible está junto a "Cliente:", "Destinatario:", "A/A:" — pon proveedor_nif: null.
Ejemplo: cabecera "Embutidos García S.L. CIF B12345678" | pie "Cliente: Bar Los Pinos CIF B87654321"
  → proveedor_nombre: "Embutidos García S.L.", proveedor_nif: "B12345678"  (NO "B87654321")
Ejemplo: albarán sin CIF del proveedor visible, solo aparece el CIF del cliente en el pie
  → proveedor_nif: null

REGLAS ADICIONALES:
- Nunca inventes datos. Si un campo no aparece, usa null.
- nombre_producto: nombre limpio SIN cantidades ni unidades ni volúmenes.
  Mal: "Aceite Alto Oleico F40% Frimasol 25L" → Bien: "Aceite Alto Oleico F40% Frimasol"
  Mal: "Bocata gran reserva 150g (50 unid)" → Bien: "Bocata Gran Reserva"
- peso_unitario_g: extrae de "150g", "200gr" en la descripción.
- unidades_por_envase: extrae de "(50 unid)", "(12 pcs)", "x50".
- peso_total_kg: si unidad=kg, repite el valor de cantidad aquí también.
- Los importes pueden usar coma o punto decimal. Elimina €.

CAMPO CONFIANZA POR LÍNEA:
Para cada línea, devolver campo "confianza" (0-100):
- 100: datos completamente claros y legibles (texto impreso nítido)
- 70-99: alguna ambigüedad menor (texto algo borroso pero identificable)
- 50-69: dato inferido o poco legible
- <50: muy dudoso, podría ser incorrecto

REGLA CRÍTICA — DOCUMENTOS MANUSCRITOS / ILEGIBLES:
Si el documento está escrito A MANO, o la línea procede de texto manuscrito, borroso,
tachado o de difícil lectura, asigna confianza < 50 a esas líneas (NO 100). Es preferible
marcar para revisión que dar por bueno un dato dudoso. Si un campo concreto (nombre,
cantidad, precio) no se puede leer con seguridad, pon null en ese campo — NUNCA lo inventes.
Un nombre de producto que quede como sigla suelta o ilegible (p.ej. "C N P") debe llevar
confianza baja para que el usuario lo verifique.

CORRECCIÓN DE ERRATAS:
Antes de devolver, revisar erratas ortográficas obvias en nombres de productos:
- 'Alún' → 'Atún'
- 'Calamr' → 'Calamar'
- 'Pollo asdo' → 'Pollo Asado'
Corregir erratas evidentes pero mantener nombres comerciales (Frimasol, Cremette, Miau, etc.)

EJEMPLOS COMPLETOS:
  "Aceite Alto Oleico F40% Frimasol 25L" cantidad=1 →
    nombre_producto: "Aceite Alto Oleico F40% Frimasol"
    cantidad: 1.0, unidad: "l", volumen_unitario_l: 25, formato_envase: "garrafa"

  "Tomate entero" columna KGRS=12.000 →
    cantidad: 12.0, peso_total_kg: 12.0, unidad: "kg"

  "Anchoas cantábricas" columna KGRS=25.26 →
    cantidad: 25.26, peso_total_kg: 25.26, unidad: "kg"

  "Queso Cremette cubo 3.5kg" →
    nombre_producto: "Queso Cremette Cubo"
    cantidad: 1.0, unidad: "ud", peso_unitario_g: 3500

  "Bocata gran reserva 150g (50 unid)" →
    nombre_producto: "Bocata Gran Reserva"
    cantidad: 50.0, unidad: "ud", peso_unitario_g: 150, unidades_por_envase: 50

  "Garbanzos Miau lata 3kg" 6 latas →
    nombre_producto: "Garbanzos Cocidos Miau"
    cantidad: 6.0, unidad: "ud", peso_unitario_g: 3000, formato_envase: "lata"

  "Harina de freír" 10 kg saco →
    cantidad: 10.0, peso_total_kg: 10.0, unidad: "kg"
"""


async def _ocr_imagen(imagen_base64: str, client: Mistral) -> str:
    response = await asyncio.wait_for(
        client.ocr.process_async(
            model=_MODELO_OCR,
            document={
                "type": "image_url",
                "image_url": f"data:image/jpeg;base64,{imagen_base64}",
            },
        ),
        timeout=60,
    )
    paginas = response.pages or []
    return "\n\n".join(p.markdown for p in paginas if p.markdown)


async def _clasificar_documento(imagen_b64: str, client: Mistral) -> dict:
    """
    Clasifica el documento en un tipo cerrado.
    Retorna {'tipo': str, 'motivo': str, 'confianza': int}.
    Solo 'albaran_proveedor' con confianza >= 75 pasa al pipeline.
    """
    prompt_clasificacion = (
        "Mira esta imagen. ¿Es claramente una nómina, un recibo de luz/gas/agua/alquiler/seguro, "
        "o un ticket de caja de supermercado?\n\n"
        "Responde SOLO en JSON: "
        '{"rechazar": true o false, "motivo": "explicación breve en español", "confianza": número entre 0 y 100}\n\n'
        "rechazar=true SOLO si estás completamente seguro de que es uno de esos documentos. "
        "Si hay cualquier duda, o si parece un documento de proveedor/distribuidor, responde rechazar=false."
    )
    response = await client.chat.complete_async(
        model=_MODELO_LLM,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{imagen_b64}"}},
                {"type": "text", "text": prompt_clasificacion},
            ],
        }],
        response_format={"type": "json_object"},
        max_tokens=120,
        temperature=0.1,
    )
    data = json.loads(response.choices[0].message.content)
    rechazar = data.get("rechazar", False)
    confianza = int(data.get("confianza", 0))
    return {
        "es_albaran": not rechazar,
        "tipo": "rechazado" if rechazar else "albaran_proveedor",
        "confianza": confianza,
        "motivo": data.get("motivo", ""),
    }


def _escapar_control(content: str) -> str:
    """Escapa caracteres de control literales que el LLM mete sin escapar dentro de strings."""
    result = []
    in_string = False
    i = 0
    while i < len(content):
        c = content[i]
        if in_string:
            if c == '\\' and i + 1 < len(content):
                result.append(c)
                result.append(content[i + 1])
                i += 2
                continue
            elif c == '"':
                in_string = False
                result.append(c)
            elif ord(c) < 0x20:
                _esc = {'\n': '\\n', '\r': '\\r', '\t': '\\t', '\b': '\\b', '\f': '\\f'}
                result.append(_esc.get(c, f'\\u{ord(c):04x}'))
            else:
                result.append(c)
        else:
            if c == '"':
                in_string = True
            result.append(c)
        i += 1
    return ''.join(result)


def _recuperar_lineas_truncadas(content: str) -> dict | None:
    """
    Recupera un JSON cortado por max_tokens: conserva la cabecera y todos los objetos de
    línea COMPLETOS dentro del array "lineas", descartando el último objeto a medias.
    Garantiza que un albarán largo nunca se pierda entero por truncación.
    """
    m = re.search(r'"lineas"\s*:\s*\[', content)
    if not m:
        return None
    arr_start = m.end()
    depth = 0
    in_str = False
    esc = False
    start: int | None = None
    objetos: list[str] = []
    i = arr_start
    while i < len(content):
        c = content[i]
        if in_str:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == '{':
                if depth == 0:
                    start = i
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0 and start is not None:
                    objetos.append(content[start:i + 1])
                    start = None
            elif c == ']' and depth == 0:
                break
        i += 1
    if not objetos:
        return None
    candidato = content[:arr_start] + ",".join(objetos) + "]}"
    try:
        return json.loads(candidato)
    except json.JSONDecodeError:
        return None


def _parse_json_robusto(content: str) -> dict:
    """
    Parsea JSON del LLM de forma tolerante:
      1. Intento directo.
      2. Escapando caracteres de control literales dentro de strings.
      3. Recuperando líneas completas si la respuesta llegó truncada (max_tokens).
    """
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    escaped = _escapar_control(content)
    try:
        return json.loads(escaped)
    except json.JSONDecodeError:
        pass

    recuperado = _recuperar_lineas_truncadas(escaped)
    if recuperado is not None:
        logger.warning("JSON del LLM truncado/dañado — recuperadas %d líneas completas", len(recuperado.get("lineas", [])))
        return recuperado

    # Sin recuperación posible: relanzar el error original para que el pipeline lo gestione.
    return json.loads(escaped)


async def _extraer_datos_llm(ocr_text: str, client: Mistral) -> dict:
    response = await asyncio.wait_for(
        client.chat.complete_async(
            model=_MODELO_LLM,
            messages=[
                {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": f"Texto del albarán:\n\n{ocr_text}"},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=8192,
        ),
        timeout=60,
    )
    return _parse_json_robusto(response.choices[0].message.content)


# ── Pipeline principal ────────────────────────────────────────────────────────

async def procesar_albaran(
    job_id: str,
    imagen_bytes: bytes,
    chat_id: int,
    progress_callback: Callable[[str], Awaitable[None]],
    imagen_hash: str | None = None,
) -> ResultadoProcesamiento:
    """
    Pipeline completo. Actualiza el job en cada etapa.
    Lanza AlbaranProcessingError si algo falla de forma irrecuperable.
    """
    mistral = Mistral(api_key=settings.MISTRAL_API_KEY)
    tokens_totales = 0
    imagen_url: str | None = None

    try:
        # a) OCR — la imagen NO se sube a Storage todavía. Solo se subirá si el albarán
        # se guarda con éxito (no duplicado, legible, válido), para no dejar huérfanas.
        await db.actualizar_job(job_id, estado="procesando")
        imagen_b64 = base64.b64encode(imagen_bytes).decode()
        await progress_callback("Leyendo imagen...")

        ocr_text = await _con_reintento(_ocr_imagen, imagen_b64, mistral)

        if not ocr_text or not ocr_text.strip():
            raise ValueError("El OCR no extrajo texto del documento. Verifica que la imagen sea legible.")

        # c2) Blacklist: rechazar documentos claramente no válidos sin llamar al LLM de extracción
        palabra_prohibida = _verificar_blacklist(ocr_text)
        if palabra_prohibida:
            logger.info("Documento rechazado por blacklist: '%s'", palabra_prohibida)
            raise ValueError(
                "Este documento no es un albarán de compra de proveedor y no se registrará.\n"
                f"Tipo de documento detectado: '{palabra_prohibida}'."
            )

        # d) Extracción LLM
        await progress_callback("Extrayendo datos estructurados...")
        raw_data = await _con_reintento(_extraer_datos_llm, ocr_text, mistral)
        albaran_data = AlbaranLLM.model_validate(raw_data)

        # d1) Resolver precio NETO de forma determinista (el neto prevalece SIEMPRE)
        for linea in albaran_data.lineas:
            _resolver_precio_neto(linea)

        # d2) Validación mínima del JSON extraído
        ok_minimo, motivo_minimo = _validar_datos_minimos(albaran_data)
        if not ok_minimo:
            logger.warning("Validación mínima fallida: %s", motivo_minimo)
            raise ValueError(
                "No pude extraer los datos correctamente de este albarán. "
                "Comprueba que la foto sea nítida y el documento esté completo.\n"
                f"(Detalle: {motivo_minimo})"
            )

        # e) Validar suma de líneas vs total (TOLERANDO el IVA: la suma de líneas es la base)
        lineas_con_revision = 0
        if albaran_data.lineas:
            total_cuadra, suma_lineas = _reconciliar_lineas_total(albaran_data)
            if not total_cuadra:
                logger.warning(
                    "Discrepancia total: suma líneas=%.2f no cuadra con base/total del albarán Nº %s",
                    suma_lineas, albaran_data.numero_albaran,
                )
                lineas_con_revision += 1
            elif suma_lineas > 0 and (
                albaran_data.base_imponible is None
                or abs(albaran_data.base_imponible - suma_lineas) / suma_lineas > 0.02
            ):
                # Líneas verificadas (cuadran con el documento) pero base_imponible del LLM
                # incoherente con ellas (mal leída o alucinada). La base imponible ES, por
                # definición, la suma de los importes netos de línea → la corregimos para que
                # la BD sea coherente con los datos reales del albarán.
                base_en_conflicto = albaran_data.base_imponible is not None
                logger.info(
                    "base_imponible corregida de %s a %.2f (= suma de líneas) en albarán Nº %s",
                    albaran_data.base_imponible, suma_lineas, albaran_data.numero_albaran,
                )
                albaran_data.base_imponible = suma_lineas
                # Si el LLM dio una base en conflicto y NO hay total para verificar la
                # completitud de las líneas, marcamos revisión en vez de corregir en silencio.
                if base_en_conflicto and albaran_data.total is None:
                    lineas_con_revision += 1

        # f) Proveedor — el primero que entra manda; nunca se modifica el NIF almacenado
        await progress_callback("Buscando proveedor...")
        proveedor, es_proveedor_nuevo = await db.buscar_o_crear_proveedor(
            nombre=albaran_data.proveedor_nombre,
            nif=albaran_data.proveedor_nif,
            direccion=albaran_data.proveedor_direccion,
            telefono=albaran_data.proveedor_telefono,
            email=albaran_data.proveedor_email,
            forma_pago_habitual=albaran_data.forma_pago,
        )

        # g) Detectar duplicado (3 capas en paralelo antes del insert)
        numero_norm = _normalizar_numero_albaran(albaran_data.numero_albaran or "")

        checks_dup: list = []
        if albaran_data.total is not None:
            # Capa 1: proveedor_id + fecha + total (±0.50€)
            checks_dup.append(db.buscar_albaran_duplicado_combinacion(
                proveedor["id"], albaran_data.fecha, albaran_data.total
            ))
            # Capa 2: nombre proveedor + fecha + total — cubre NIF mal leído
            checks_dup.append(db.buscar_albaran_duplicado_por_nombre_proveedor(
                albaran_data.proveedor_nombre, albaran_data.fecha, albaran_data.total
            ))
        if numero_norm:
            # Capa 3: número normalizado
            checks_dup.append(db.buscar_albaran_duplicado_norm(numero_norm, proveedor["id"]))

        resultados_dup = await asyncio.gather(*checks_dup, return_exceptions=True) if checks_dup else []
        duplicado = next(
            (r for r in resultados_dup if isinstance(r, dict) and r is not None),
            None,
        )

        if duplicado:
            await db.actualizar_job(job_id, estado="completado")
            return ResultadoProcesamiento(
                albaran_id=duplicado["id"],
                proveedor_id=proveedor["id"],
                proveedor_nombre=proveedor["nombre"],
                numero_albaran=albaran_data.numero_albaran,
                fecha=albaran_data.fecha,
                forma_pago=albaran_data.forma_pago,
                total=albaran_data.total,
                num_lineas=len(albaran_data.lineas),
                es_proveedor_nuevo=False,
                es_duplicado=True,
                es_duplicado_fecha=duplicado.get("creado_en"),
                es_duplicado_numero_original=duplicado.get("numero_albaran"),
                lineas_con_revision=0,
                alertas_precio=[],
                imagen_url=imagen_url,
                lineas_para_confirmacion=[],
            )

        # h-i) Insertar albarán
        await progress_callback("Guardando albarán...")
        detalle_iva_dicts = (
            [d.model_dump() for d in albaran_data.detalle_iva]
            if albaran_data.detalle_iva else None
        )
        try:
            albaran_row = await db.insertar_albaran(
                proveedor_id=proveedor["id"],
                numero_albaran=albaran_data.numero_albaran,
                fecha=albaran_data.fecha,
                forma_pago=albaran_data.forma_pago,
                base_imponible=albaran_data.base_imponible,
                total_iva=albaran_data.total_iva,
                total=albaran_data.total,
                imagen_url=imagen_url,
                detalle_iva=detalle_iva_dicts,
                imagen_hash=imagen_hash,
            )
        except Exception as e:
            if "23505" in str(e):
                # La BD rechazó el insert por constraint UNIQUE — tratar como duplicado
                await db.actualizar_job(job_id, estado="completado")
                existing = await db.buscar_albaran_duplicado_norm(numero_norm, proveedor["id"])
                if existing is None and albaran_data.total is not None:
                    existing = await db.buscar_albaran_duplicado_combinacion(
                        proveedor["id"], albaran_data.fecha, albaran_data.total
                    )
                return ResultadoProcesamiento(
                    albaran_id=existing["id"] if existing else "unknown",
                    proveedor_id=proveedor["id"],
                    proveedor_nombre=proveedor["nombre"],
                    numero_albaran=albaran_data.numero_albaran,
                    fecha=albaran_data.fecha,
                    forma_pago=albaran_data.forma_pago,
                    total=albaran_data.total,
                    num_lineas=len(albaran_data.lineas),
                    es_proveedor_nuevo=False,
                    es_duplicado=True,
                    es_duplicado_fecha=existing.get("creado_en") if existing else None,
                    es_duplicado_numero_original=existing.get("numero_albaran") if existing else None,
                    lineas_con_revision=0,
                    alertas_precio=[],
                    imagen_url=imagen_url,
                    lineas_para_confirmacion=[],
                )
            raise

        # h0) Subir imagen a Storage SOLO ahora — el albarán está guardado y no es duplicado.
        # Si falla la subida, el albarán queda guardado sin URL (no se pierde el dato).
        ruta = f"albaranes/{chat_id}/{job_id}.jpg"
        try:
            imagen_url = await db.subir_imagen("albaranes", ruta, imagen_bytes)
            await db.actualizar_campo_albaran(albaran_row["id"], imagen_url=imagen_url)
            await db.actualizar_job(job_id, imagen_url=imagen_url)
        except Exception as e:
            logger.warning("No se pudo subir imagen: %s — albarán guardado sin URL", e)

        # h) Normalizar y guardar líneas — 3 fases en paralelo
        await progress_callback(f"Procesando {len(albaran_data.lineas)} productos...")
        alertas_precio: list[dict] = []

        # Fase 0: cargar catálogo del proveedor una sola vez
        productos_existentes = await db.buscar_productos_por_proveedor(proveedor["id"])

        # Fase 1: normalizar todas las líneas en una sola llamada LLM (batch)
        norms = await normalizar_productos_batch(
            proveedor["id"],
            [linea.nombre_producto for linea in albaran_data.lineas],
            productos_existentes,
        )

        # Fase 2: buscar/crear productos en catálogo en paralelo
        producto_rows = await asyncio.gather(*[
            db.buscar_o_crear_producto_catalogo(
                proveedor_id=proveedor["id"],
                nombre_normalizado=norm.normalized_name,
                unidad_base=linea.unidad,
                formato_habitual=linea.formato_envase,
            )
            for norm, linea in zip(norms, albaran_data.lineas)
        ])

        # Invalidar caché si se crearon productos nuevos (para el próximo albarán)
        if any(norm.is_new_product for norm in norms):
            invalidar_cache_proveedor(proveedor["id"])

        # Construir lineas_para_insertar
        lineas_para_insertar: list[dict] = [
            {
                "albaran_id": albaran_row["id"],
                "producto_catalogo_id": producto_row["id"],
                "descripcion_original": linea.descripcion_original or linea.nombre_producto,
                "descripcion_limpia": norm.normalized_name,
                "cantidad": linea.cantidad,
                "unidad": linea.unidad,
                "precio_unitario": linea.precio_unitario,
                "importe_neto": linea.importe_neto,
                "peso_unitario_g": linea.peso_unitario_g,
                "unidades_por_envase": linea.unidades_por_envase,
                "peso_total_kg": linea.peso_total_kg,
                "volumen_unitario_l": linea.volumen_unitario_l,
                "formato_envase": linea.formato_envase,
                "numero_lote": linea.numero_lote,
                "caducidad": _parsear_fecha_caducidad(linea.caducidad),
                "descuento_pct": linea.descuento_pct,
            }
            for norm, linea, producto_row in zip(norms, albaran_data.lineas, producto_rows)
        ]

        # Fase 3: actualizar precios históricos en paralelo
        async def _actualizar_precio_linea(
            norm_result: Any, linea: Any, producto_row: dict
        ) -> dict | None:
            if not linea.precio_unitario:
                return None
            try:
                anterior, alerta = await db.actualizar_precio_catalogo(
                    producto_row["id"], linea.precio_unitario
                )
                if alerta and anterior:
                    pct = (linea.precio_unitario - anterior) / anterior * 100
                    return {
                        "producto": norm_result.normalized_name,
                        "anterior": anterior,
                        "nuevo": linea.precio_unitario,
                        "pct": round(pct, 1),
                    }
            except Exception as e:
                logger.warning("Error actualizando precio de %s: %s", norm_result.normalized_name, e)
            return None

        resultados_precio = await asyncio.gather(*[
            _actualizar_precio_linea(norm, linea, producto_row)
            for norm, linea, producto_row in zip(norms, albaran_data.lineas, producto_rows)
        ])
        alertas_precio = [r for r in resultados_precio if r is not None]

        # Añadir confianza y requiere_revision a cada línea antes de insertar
        for i, (linea, linea_dict) in enumerate(zip(albaran_data.lineas, lineas_para_insertar)):
            ok, motivo = _validar_linea(linea)
            sin_datos = linea.importe_neto is None and linea.precio_unitario is None
            linea_dict["confianza"] = linea.confianza
            linea_dict["requiere_revision"] = not ok or linea.confianza < 70 or sin_datos

        lineas_insertadas = await db.insertar_lineas(lineas_para_insertar)

        # Recopilar líneas para confirmación (confianza < 70 o requiere_revision)
        lineas_para_confirmacion: list[dict] = []
        num = 1
        for i, (linea, linea_row) in enumerate(zip(albaran_data.lineas, lineas_insertadas)):
            ok, motivo = _validar_linea(linea)
            if not ok or linea.confianza < 70:
                razon = motivo if not ok else f"confianza {linea.confianza}%"
                norm_name = lineas_para_insertar[i]["descripcion_limpia"]
                lineas_para_confirmacion.append({
                    "num": num,
                    "linea_id": linea_row["id"],
                    "descripcion": norm_name,
                    "cantidad": linea.cantidad,
                    "precio": linea.precio_unitario,
                    "importe": linea.importe_neto,
                    "unidad": linea.unidad,
                    "razon": razon,
                })
                num += 1

        # k) Actualizar job
        await db.actualizar_job(job_id, estado="completado")

        # l) Auditoría
        await db.registrar_auditoria(
            tipo="extraccion",
            resultado="revision" if lineas_con_revision > 0 else "ok",
            telegram_user_id=chat_id,
            imagen_url=imagen_url,
            modelo_ocr=_MODELO_OCR,
            modelo_llm=_MODELO_LLM,
            tokens_consumidos=tokens_totales or None,
            coste_estimado_usd=tokens_totales * (_COSTE_INPUT_POR_TOKEN + _COSTE_OUTPUT_POR_TOKEN) / 2 if tokens_totales else None,
            detalle={"num_lineas": len(albaran_data.lineas), "alertas": len(alertas_precio)},
            albaran_id=albaran_row["id"],
        )

        return ResultadoProcesamiento(
            albaran_id=albaran_row["id"],
            proveedor_id=proveedor["id"],
            proveedor_nombre=proveedor["nombre"],
            numero_albaran=albaran_data.numero_albaran,
            fecha=albaran_data.fecha,
            forma_pago=albaran_data.forma_pago,
            total=albaran_data.total,
            num_lineas=len(albaran_data.lineas),
            es_proveedor_nuevo=es_proveedor_nuevo,
            es_duplicado=False,
            lineas_con_revision=lineas_con_revision,
            alertas_precio=alertas_precio,
            imagen_url=imagen_url,
            lineas_para_confirmacion=lineas_para_confirmacion,
        )

    except Exception as e:
        await db.actualizar_job(job_id, estado="error", error_detalle=str(e))
        await db.registrar_auditoria(
            tipo="extraccion",
            resultado="error",
            telegram_user_id=chat_id,
            imagen_url=imagen_url,
            modelo_ocr=_MODELO_OCR,
            modelo_llm=_MODELO_LLM,
            detalle={"error": str(e)},
        )
        raise


def _parsear_fecha_caducidad(fecha_str: str | None) -> str | None:
    if not fecha_str:
        return None
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y"):
        try:
            return datetime.strptime(fecha_str.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None
