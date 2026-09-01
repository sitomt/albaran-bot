"""Modelos, prompt y reglas deterministas compartidas por la ingesta actual."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, field_validator

from .config import settings

logger = logging.getLogger(__name__)

_MODELO_OCR = settings.OCR_MODEL
_MODELO_LLM = settings.EXTRACTION_MODEL

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


def _normalizar_fecha(v: str | None) -> str | None:
    """Convierte cualquier formato de fecha reconocido a ISO (YYYY-MM-DD).

    Sin esto, cadenas como '26/08/2026' llegan tal cual a Postgres, que las
    interpreta como MM/DD/YYYY por defecto: mes=26 no existe → error 22008
    ("date/time field value out of range") y el confirm entero falla.
    """
    if not v:
        return None
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y", "%d-%m-%y"):
        try:
            return datetime.strptime(str(v).strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


# ── Modelos de datos ──────────────────────────────────────────────────────────

class LineaAlbaranLLM(BaseModel):
    nombre_producto: str | None = None
    descripcion_original: str | None = None
    cantidad: float | None = None
    unidad: str | None = None
    precio_unitario: float | None = None
    precio_tarifa: float | None = None   # columna TARIFA / precio de lista (bruto), transitorio
    precio_neto: float | None = None     # columna NETO / PRECIO FINAL explícita, transitorio
    importe_neto: float | None = None
    peso_unitario_g: float | None = None
    unidades_por_envase: int | None = None
    bultos: float | None = None
    peso_total_kg: float | None = None
    volumen_unitario_l: float | None = None
    formato_envase: str | None = None
    numero_lote: str | None = None
    caducidad: str | None = None
    descuento_pct: float | None = None
    confianza: int = 0

    @field_validator("confianza", mode="before")
    @classmethod
    def limpiar_confianza(cls, v: Any) -> int:
        try:
            n = int(float(str(v)))
            return max(0, min(100, n))
        except Exception:
            # Confianza ausente/inválida nunca debe convertirse en certeza.
            return 0

    @field_validator("cantidad", mode="before")
    @classmethod
    def cantidad_positiva(cls, v: Any) -> float | None:
        v = _parsear_numero(v)
        if v is not None and v <= 0:
            return None
        return v

    @field_validator("precio_unitario", "precio_tarifa", "precio_neto", "importe_neto", "peso_unitario_g", "peso_total_kg", "volumen_unitario_l", "descuento_pct", "bultos", mode="before")
    @classmethod
    def limpiar_numerico(cls, v: Any) -> float | None:
        return _parsear_numero(v)

    @field_validator("unidades_por_envase", mode="before")
    @classmethod
    def limpiar_entero(cls, v: Any) -> int | None:
        n = _parsear_numero(v)
        return int(n) if n is not None else None

    @field_validator("caducidad", mode="before")
    @classmethod
    def normalizar_caducidad(cls, v: str | None) -> str | None:
        return _normalizar_fecha(v)


class DetalleIvaLLM(BaseModel):
    tipo: float | None = None
    base: float | None = None
    cuota: float | None = None


class AlbaranLLM(BaseModel):
    proveedor_nombre: str | None = None
    proveedor_nif: str | None = None
    proveedor_direccion: str | None = None
    proveedor_telefono: str | None = None
    proveedor_email: str | None = None
    numero_albaran: str | None = None
    fecha: str | None = None
    forma_pago: str | None = None
    base_imponible: float | None = None
    total_iva: float | None = None
    total: float | None = None
    detalle_iva: list[DetalleIvaLLM] | None = None
    lineas: list[LineaAlbaranLLM]

    @field_validator("fecha", mode="before")
    @classmethod
    def normalizar_fecha(cls, v: str | None) -> str | None:
        return _normalizar_fecha(v)

    @field_validator("base_imponible", "total_iva", "total", mode="before")
    @classmethod
    def limpiar_importe(cls, v: Any) -> float | None:
        return _parsear_numero(v)

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
      "bultos": número de piezas/cajas de la columna UDS si además existe una columna KG/KGRS o null,
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

REGLA CRÍTICA — NÚMERO DEL DOCUMENTO:
`numero_albaran` es exclusivamente el valor etiquetado como ALBARÁN / Nº ALBARÁN.
Si no existe pero el documento es una factura, usa Nº FACTURA. Si aparecen ambos,
prefiere el Nº ALBARÁN. Nunca uses Nº R.S./registro sanitario, R.P.P., código de
cliente, pedido, ruta, cuenta, lote o teléfono. Ejemplo: "Nº RS 40.20059/MU",
"Nº FACTURA 26/2.968" y dentro de la tabla "ALBARÁN 3.950" → devuelve "3.950".

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

REGLA CRÍTICA — DE DÓNDE SALEN `cantidad` Y `unidad` (LA REGLA MÁS IMPORTANTE):

La unidad la decide la ESTRUCTURA del albarán (qué columna trae el número),
NUNCA el tipo de alimento ni el texto del nombre del producto.
La medida base por defecto son UNIDADES; los kilos hay que demostrarlos.

Aplica este procedimiento a CADA línea, en este orden exacto:

PASO 1 — ¿Existe una columna de peso propia (KGRS, KG, KILOS, PESO, Ud/Kg, NETO KG)
         y trae VALOR en ESTA línea concreta?
   SÍ → la línea se factura por peso:
        cantidad = ese peso, unidad = "kg", peso_total_kg = ese mismo peso.
        Si además hay columna UDS/CAJAS/BULTOS, esa cifra va en `bultos`,
        NUNCA en `cantidad`.
   NO → pasa al PASO 2.
        Una columna de peso que EXISTE pero está VACÍA en esta línea cuenta como NO.
        Una celda vacía significa "esta línea no se vende por peso": jamás rellenes
        el peso copiando el número de la columna de unidades ni de ninguna otra.

PASO 2 — Mira la cifra de la columna de cantidad (UNID., CDAD, CANTIDAD, CTD, UDS).
         ¿Es un decimal de báscula, es decir, tiene decimales que solo salen de pesar
         (p.ej. 1,557 / 5,74 / 18,60 / 25,26 / 2,39)?
   SÍ → la línea se factura por peso aunque no haya columna de peso:
        cantidad = esa cifra, unidad = "kg", peso_total_kg = esa cifra.
   NO → la línea se factura por UNIDADES (lo normal):
        cantidad = esa cifra tal cual, unidad = "ud", peso_total_kg = null.

POR DEFECTO, ANTE LA DUDA: unidad = "ud" y peso_total_kg = null.
Es mucho peor inventar kilos que el albarán no dice que contar unidades.

PROHIBIDO deducir la unidad del TIPO de producto. Que sea carne, pescado, queso,
embutido, aceituna, verdura o fruta NO significa que se venda a granel: esos mismos
alimentos se venden a diario en tarrinas, bolsas, botes, cubos y bandejas cerradas.
Solo el PASO 1 (columna de peso con valor) o el PASO 2 (decimal de báscula)
autorizan unidad = "kg". Si ninguno se cumple, es "ud" aunque el producto sea fresco.

PROHIBIDO usar un peso o volumen que aparezca PEGADO AL NOMBRE del producto como
`cantidad`, como `peso_total_kg`, o como excusa para poner unidad = "kg".
Un peso/volumen dentro del nombre describe SIEMPRE el tamaño de CADA envase.
Va en peso_unitario_g / volumen_unitario_l, con unidad = "ud". Da igual que la
cantidad sea 1 o 20, y da igual cuál sea el alimento.

Comprobación final antes de responder: para cada línea verifica qué cifra cumple
PRECIO NETO × CANTIDAD ≈ IMPORTE. Úsala solo para elegir entre la columna de
unidades y la de peso; no cambies ni inventes ningún valor observado.

REGLA — VOLUMEN EN LÍQUIDOS:
  - unidad = "l" solo si el albarán factura litros sueltos (a granel).
  - Un líquido en envases contables (briks, botellas, garrafas, latas) es unidad = "ud",
    y su capacidad va en volumen_unitario_l.

REGLA CRÍTICA — PESO PEGADO AL NOMBRE = PESO POR UNIDAD, NUNCA PESO TOTAL (MUY IMPORTANTE):
Si un número de peso (kg/g) aparece pegado al nombre del producto (p.ej. "Ensaladilla
Rusa Premium 2.3 Kg", "Queso Cremette cubo 3.5kg", "Rosquilla casera 2Kg") Y NO hay una
columna KGRS/KG/KILOS/PESO separada con el peso real de la línea:
  - Ese número es el peso de CADA envase/tarrina/cubo, NUNCA el peso total de la línea.
  - Va SIEMPRE en peso_unitario_g (convertido a gramos), NUNCA en peso_total_kg.
  - unidad = "ud" (se compran por tarrinas/cubos, no a granel), aunque el nombre
    contenga "Kg".
  - cantidad = número de envases/tarrinas (columna UDS o cantidad tal cual), SIN
    multiplicar ni dividir por el peso — esto vale igual si cantidad es 1 o mayor.
  Esto es el PASO 2 de la regla de cantidad/unidad aplicado al nombre: aunque el
  producto sea un alimento que a veces se vende a granel (p.ej. ensaladilla, queso,
  aceitunas, anchoas), si el peso aparece pegado al nombre en vez de en una columna
  propia CON VALOR, es tamaño de envase, no venta a granel.
  Ejemplo con cantidad=1: "Queso Cremette cubo 3.5kg" →
    cantidad: 1.0, unidad: "ud", peso_unitario_g: 3500
  Ejemplo con cantidad=2 (mismo patrón, NO lo conviertas en peso a granel):
    "Ensaladilla Rusa Premium 2.3 Kg" con columna cantidad=2 →
    cantidad: 2.0, unidad: "ud", peso_unitario_g: 2300, peso_total_kg: null
    (NUNCA cantidad: 2.3, ni unidad: "kg", ni peso_total_kg: 2)

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
Si aparece un NIF junto a la tabla de cliente y otro junto al nombre/contacto del
emisor (incluso en el pie), el segundo es el proveedor. Nunca copies el NIF de
HERBAHER HOSTELERIA/RESTAURANTE VENTA ALEGRIA como NIF del proveedor.

REGLAS ADICIONALES:
- Nunca inventes datos. Si un campo no aparece, usa null.
- nombre_producto: nombre limpio SIN cantidades ni unidades ni volúmenes.
  Mal: "Aceite Alto Oleico F40% Frimasol 25L" → Bien: "Aceite Alto Oleico F40% Frimasol"
  Mal: "Bocata gran reserva 150g (50 unid)" → Bien: "Bocata Gran Reserva"
- peso_unitario_g: extrae de "150g", "200gr" en la descripción.
- unidades_por_envase: extrae de "(50 unid)", "(12 pcs)", "x50".
- peso_total_kg: rellénalo SOLO cuando la unidad sea "kg" por el PASO 1 o el PASO 2
  (entonces repite ahí el valor de cantidad). Si la unidad es "ud", va null: el peso
  de cada envase se guarda en peso_unitario_g, no aquí.
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

  "Tomate entero" columna KGRS=12.000 (columna de peso CON valor → PASO 1) →
    cantidad: 12.0, peso_total_kg: 12.0, unidad: "kg"

  "Anchoas cantábricas" columna KGRS=25.26 (PASO 1) →
    cantidad: 25.26, peso_total_kg: 25.26, unidad: "kg"

  MISMO producto, columna de peso VACÍA — el caso que más se falla:
  "Tomate entero 1kg .exito." con UNID.=12,000 y la columna KGRS en blanco →
    nombre_producto: "Tomate Entero Éxito"
    cantidad: 12.0, unidad: "ud", peso_unitario_g: 1000, peso_total_kg: null
    (son 12 envases de 1 kg; NUNCA unidad "kg" solo porque el tomate sea verdura)

  "Aceitunas partidas cubo 2.5 kg" con UNID.=4,000 y columna KGRS en blanco →
    nombre_producto: "Aceitunas Partidas Cubo"
    cantidad: 4.0, unidad: "ud", peso_unitario_g: 2500, peso_total_kg: null
    (4 cubos, NO 4 kg: el 2.5 kg es lo que pesa cada cubo)

  "Anchoas cantabrico Noemar" con UNID.=3,000 y columna KGRS en blanco →
    cantidad: 3.0, unidad: "ud", peso_total_kg: null
    (la celda de peso vacía manda: son 3 envases, aunque sea pescado)

  "Queso Cremette cubo 3.5kg" →
    nombre_producto: "Queso Cremette Cubo"
    cantidad: 1.0, unidad: "ud", peso_unitario_g: 3500

  "Ensaladilla Rusa Premium 2.3 Kg" con columna cantidad=2 (peso pegado al nombre,
  SIN columna KGRS propia — es el peso de cada tarrina, no de toda la línea) →
    nombre_producto: "Ensaladilla Rusa Premium"
    cantidad: 2.0, unidad: "ud", peso_unitario_g: 2300, peso_total_kg: null

  "Bocata gran reserva 150g (50 unid)" →
    nombre_producto: "Bocata Gran Reserva"
    cantidad: 50.0, unidad: "ud", peso_unitario_g: 150, unidades_por_envase: 50

  "Garbanzos Miau lata 3kg" 6 latas →
    nombre_producto: "Garbanzos Cocidos Miau"
    cantidad: 6.0, unidad: "ud", peso_unitario_g: 3000, formato_envase: "lata"

  "Harina de freír Miau" con UNID.=10,000 y la columna KGRS en blanco →
    cantidad: 10.0, unidad: "ud", peso_total_kg: null
    (son 10 sacos; sin peso en su columna NO se puede afirmar que sean 10 kg)

  "Harina de freír" en un albarán cuya columna KGRS marca 10,000 para esa línea →
    cantidad: 10.0, peso_total_kg: 10.0, unidad: "kg"
"""


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
        result = json.loads(content)
        result["_extraction_complete"] = True
        return result
    except json.JSONDecodeError:
        pass

    escaped = _escapar_control(content)
    try:
        result = json.loads(escaped)
        result["_extraction_complete"] = True
        return result
    except json.JSONDecodeError:
        pass

    recuperado = _recuperar_lineas_truncadas(escaped)
    if recuperado is not None:
        logger.warning("JSON del LLM truncado/dañado — recuperadas %d líneas completas", len(recuperado.get("lineas", [])))
        recuperado["_extraction_complete"] = False
        return recuperado

    # Sin recuperación posible: relanzar el error original para que el pipeline lo gestione.
    return json.loads(escaped)
