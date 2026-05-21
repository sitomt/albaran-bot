"""
Motor de consultas en lenguaje natural.
NL → SQL (Mistral Small) → Supabase → respuesta en español natural.
"""
from __future__ import annotations

import json
import logging
from datetime import date

from mistralai.client.sdk import Mistral

from .config import settings
from . import supabase_client as db

logger = logging.getLogger(__name__)


_MODELO = "mistral-small-2506"
_ERR_CAPACIDAD = "Sistema temporalmente no disponible. Inténtalo en unos minutos."


async def _mistral_chat(client: Mistral, **kwargs) -> object:
    """Llama a mistral-small-2506; traduce 429 a mensaje amigable."""
    try:
        return await client.chat.complete_async(model=_MODELO, **kwargs)
    except Exception as e:
        if "429" in str(e) or "capacity" in str(e).lower():
            raise _CapacidadError() from e
        raise


class _CapacidadError(Exception):
    pass

_SCHEMA_CONTEXT = """\
Base de datos PostgreSQL con estas tablas (todas las consultas por fecha DESC):

proveedores(id UUID, nombre TEXT, nif TEXT, direccion TEXT, telefono TEXT, email TEXT, forma_pago_habitual TEXT, creado_en TIMESTAMPTZ)

productos_catalogo(id UUID, nombre_normalizado TEXT, proveedor_id UUID→proveedores.id, variantes JSONB, unidad_base TEXT, precio_ultima_compra NUMERIC, precio_medio_historico NUMERIC, creado_en TIMESTAMPTZ)

albaranes(id UUID, numero_albaran TEXT, fecha DATE, proveedor_id UUID→proveedores.id, forma_pago TEXT, base_imponible NUMERIC, total_iva NUMERIC, total NUMERIC, detalle_iva JSONB, imagen_url TEXT, creado_en TIMESTAMPTZ)
  detalle_iva es un array JSON: [{"tipo": 10, "base": 307.53, "cuota": 30.75}, {"tipo": 4, "base": 30.87, "cuota": 1.23}]

lineas_albaran(id UUID, albaran_id UUID→albaranes.id, producto_catalogo_id UUID→productos_catalogo.id, descripcion_original TEXT, descripcion_limpia TEXT, cantidad NUMERIC, unidad TEXT, precio_unitario NUMERIC, importe_neto NUMERIC, peso_unitario_g NUMERIC, unidades_por_envase INT, peso_total_kg NUMERIC, volumen_unitario_l NUMERIC, formato_envase TEXT, numero_lote TEXT, caducidad DATE, descuento_pct NUMERIC)

auditoria(id UUID, tipo TEXT, resultado TEXT, creado_en TIMESTAMPTZ)
jobs(id UUID, estado TEXT, creado_en TIMESTAMPTZ)
"""

_SQL_SYSTEM_PROMPT = """\
Eres un experto en SQL PostgreSQL para un sistema de gestión de albaranes de restaurante.
Genera SOLO una consulta SELECT válida basada en la pregunta del usuario.

ESQUEMA:
{schema}

REGLAS OBLIGATORIAS:
1. Genera SOLO SELECT. Nunca INSERT, UPDATE, DELETE, DROP, TRUNCATE ni DDL.
2. Usa JOINs correctos basados en las FK del esquema.
3. Ordena por fecha DESC por defecto.
4. SIEMPRE usa ILIKE con comodines para texto: campo ILIKE '%palabra%'. NUNCA uses = para comparar nombres.
5. Para "este mes" usa: date_trunc('month', CURRENT_DATE).
6. Para "esta semana" usa: date_trunc('week', CURRENT_DATE).
7. Limita resultados a 50 filas máximo con LIMIT 50.
8. Fecha actual: {hoy}
9. Responde SOLO con el SQL, sin explicaciones, sin ```sql, sin markdown, sin punto y coma final.
10. NUNCA uses SELECT DISTINCT con ORDER BY en columnas que no estén en el SELECT. Si necesitas ordenar por fecha, inclúyela en el SELECT o usa una subconsulta.
11. NUNCA uses WITH ni CTEs. Para "última compra" o "último pedido" usa subquery directa:
    AND a.fecha = (SELECT MAX(a2.fecha) FROM albaranes a2 JOIN lineas_albaran la2 ON la2.albaran_id = a2.id WHERE la2.descripcion_limpia ILIKE '%producto%')
12. Si el usuario NO menciona período de tiempo ("este mes", "esta semana", "en enero", etc.), NO añadas ningún filtro de fecha. Devuelve todos los registros históricos.

REGLA ILIKE OBLIGATORIA:
- Nombres de proveedores: p.nombre ILIKE '%caballero%' ← extraer solo la palabra clave del nombre
- Nombres de productos: la.descripcion_limpia ILIKE '%queso%'
- Nunca: p.nombre = 'Lucas Caballero S.L.' ← PROHIBIDO el igual exacto
- Si el usuario menciona "Lucas Caballero" → genera ILIKE '%caballero%' o ILIKE '%lucas%'
- Si el usuario menciona "Cremette" → genera ILIKE '%cremette%'

REGLA CRÍTICA — CANTIDADES Y PESOS:
El campo `unidad` determina cómo interpretar las cantidades:
- Si unidad = 'kg': la cantidad está en kg. Usa COALESCE(peso_total_kg, cantidad) para obtener el peso real.
- Si unidad = 'ud': la cantidad son unidades (latas, cajas, etc.).
- Para preguntas sobre "cuántos kilos" o "cuánto peso": filtra WHERE unidad = 'kg' y suma COALESCE(peso_total_kg, cantidad).
- Para preguntas sobre "cuántas unidades" o "cuántas cajas": filtra WHERE unidad != 'kg' y suma cantidad.
- Para preguntas de gasto/importe: usa siempre SUM(importe_neto) o SUM(a.total), independiente de la unidad.

REGLA CRÍTICA — DESCUENTO (precio_unitario es SIEMPRE el precio neto ya descontado):
PROHIBIDO: precio_unitario * (1 - descuento_pct/100) ← aplica descuento DOS VECES, resultado menor que precio_unitario
CORRECTO para precio tarifa (antes del descuento): precio_unitario / (1 - descuento_pct/100)
  Ejemplo correcto: 1.81 / (1 - 10/100) = 1.81 / 0.90 = 2.01 ← siempre MAYOR que precio_unitario
  Ejemplo incorrecto: 1.81 * (1 - 10/100) = 1.81 * 0.90 = 1.63 ← ERROR, es menor que precio_unitario

REGLA CRÍTICA — CÁLCULO DE AHORRO POR DESCUENTOS (usa precio_unitario × cantidad):
precio_unitario es el precio neto ya descontado. Para calcular el precio tarifa original:
  precio_tarifa = precio_unitario / (1 - descuento_pct/100)  ← SIEMPRE mayor que precio_unitario
  total_sin_descuento = SUM(CASE WHEN COALESCE(descuento_pct,0) > 0
                              THEN precio_unitario / (1 - descuento_pct/100) * cantidad
                              ELSE precio_unitario * cantidad END)
  total_con_descuento = SUM(precio_unitario * cantidad)
  ahorro = total_sin_descuento - total_con_descuento
total_sin_descuento SIEMPRE es mayor o igual que total_con_descuento.

REGLA CRÍTICA — IVA (campo detalle_iva en tabla albaranes):
detalle_iva es un array JSONB con TODOS los tramos de IVA del albarán.
Para preguntas sobre IVA, SIEMPRE usa jsonb_array_elements con COALESCE para manejar nulls:
  SELECT a.numero_albaran, a.fecha,
         (elem->>'tipo')::numeric as tipo_iva,
         (elem->>'base')::numeric as base,
         (elem->>'cuota')::numeric as cuota
  FROM albaranes a
  JOIN proveedores p ON a.proveedor_id = p.id,
  jsonb_array_elements(COALESCE(a.detalle_iva, '[]'::jsonb)) elem
  WHERE p.nombre ILIKE '%proveedor%'
  AND a.detalle_iva IS NOT NULL
  ORDER BY a.fecha DESC LIMIT 20
Esto devuelve UNA FILA POR TRAMO de IVA — el LLM luego agrupa por albarán.
Si no hay desglose (detalle_iva IS NULL), usa a.total_iva directamente.

REGLA CRÍTICA — PRECIO (precio_unitario es SIEMPRE el precio neto ya descontado):
precio_unitario en la BD ya tiene el descuento aplicado. descuento_pct es solo informativo.
Para calcular el precio de tarifa (antes de descuento): ROUND(la.precio_unitario / (1 - la.descuento_pct/100), 4)

A) Pregunta sobre PRECIO ("¿cuánto me cuesta X?", "precio de X", "¿a cómo está X?"):
   → Consulta la línea MÁS RECIENTE (ORDER BY a.fecha DESC LIMIT 1):
     SELECT la.descripcion_limpia, la.precio_unitario,
            la.descuento_pct,
            CASE WHEN la.descuento_pct > 0
                 THEN ROUND(la.precio_unitario / (1 - la.descuento_pct/100), 4)
                 ELSE NULL END as precio_tarifa,
            la.unidad, la.volumen_unitario_l, p.nombre as proveedor, a.fecha
   → NO uses SUM. NO uses importe_neto.

B) Pregunta sobre GASTO TOTAL ("¿cuánto gasté?", "total gastado", "¿cuánto llevo gastado?"):
   → Usa SUM(la.importe_neto) o SUM(a.total).

C) Pregunta sobre CANTIDAD ("¿cuántos kilos?", "¿cuántas unidades?", "¿cuánto he comprado?"):
   → Usa SUM(la.cantidad) o SUM(COALESCE(la.peso_total_kg, la.cantidad)) para kg.
   → Filtra por la.descripcion_limpia ILIKE '%término%'.
   → Agrupa por la.descripcion_limpia.

E) Pregunta sobre IVA ("¿cuánto IVA lleva?", "¿qué tipos de IVA?", "desglose del IVA"):
   → Usa jsonb_array_elements con COALESCE y filtra IS NOT NULL:
     SELECT a.numero_albaran, a.fecha,
            (elem->>'tipo')::numeric as tipo_iva,
            (elem->>'base')::numeric as base,
            (elem->>'cuota')::numeric as cuota
     FROM albaranes a
     JOIN proveedores p ON a.proveedor_id = p.id,
     jsonb_array_elements(COALESCE(a.detalle_iva, '[]'::jsonb)) elem
     WHERE p.nombre ILIKE '%proveedor%'
     AND a.detalle_iva IS NOT NULL
     ORDER BY a.fecha DESC LIMIT 20
   → Para total IVA sin desglose: SELECT SUM(a.total_iva) FROM albaranes a ...

D) Pregunta sobre FORMA DE PAGO de un proveedor:
   → forma_pago está en tabla albaranes, NO en proveedores.
   → Obtén la del albarán más reciente:
     SELECT a.forma_pago, a.fecha FROM albaranes a
     JOIN proveedores p ON a.proveedor_id = p.id
     WHERE p.nombre ILIKE '%proveedor%'
     ORDER BY a.fecha DESC LIMIT 1

F) Pregunta sobre AHORRO/DESCUENTOS ("¿cuánto me ahorro?", "total sin descuentos", "¿qué me ahorro con el descuento?"):
   → Usa precio_unitario × cantidad como base:
     SELECT
       ROUND(SUM(CASE WHEN COALESCE(la.descuento_pct,0) > 0
                      THEN la.precio_unitario / (1 - la.descuento_pct/100) * la.cantidad
                      ELSE la.precio_unitario * la.cantidad END), 2) as total_sin_descuento,
       ROUND(SUM(la.precio_unitario * la.cantidad), 2) as total_con_descuento,
       ROUND(SUM(CASE WHEN COALESCE(la.descuento_pct,0) > 0
                      THEN (la.precio_unitario / (1 - la.descuento_pct/100) - la.precio_unitario) * la.cantidad
                      ELSE 0 END), 2) as ahorro
     FROM lineas_albaran la
     JOIN albaranes a ON la.albaran_id = a.id
     JOIN proveedores p ON a.proveedor_id = p.id
     WHERE p.nombre ILIKE '%proveedor%'
   → total_sin_descuento SIEMPRE >= total_con_descuento. Si no es así hay un bug.

REGLA CRÍTICA — CANTIDAD EN LITROS:
Si el producto tiene volumen_unitario_l > 0, calcula litros totales:
  SUM(la.cantidad * la.volumen_unitario_l) as litros_totales
  Precio por litro: la.precio_unitario / la.volumen_unitario_l

Ejemplos:
- "¿Cuánto me cuesta el tomate?"
  SELECT la.descripcion_limpia, la.precio_unitario, la.descuento_pct, CASE WHEN la.descuento_pct > 0 THEN ROUND(la.precio_unitario / (1 - la.descuento_pct/100), 4) ELSE NULL END as precio_tarifa, la.unidad, p.nombre as proveedor, a.fecha FROM lineas_albaran la JOIN albaranes a ON la.albaran_id = a.id JOIN proveedores p ON a.proveedor_id = p.id WHERE la.descripcion_limpia ILIKE '%tomate%' ORDER BY a.fecha DESC LIMIT 1

- "¿Cuánto me cuesta el aceite frimasol?"
  SELECT la.descripcion_limpia, la.precio_unitario, la.descuento_pct, CASE WHEN la.descuento_pct > 0 THEN ROUND(la.precio_unitario / (1 - la.descuento_pct/100), 4) ELSE NULL END as precio_tarifa, la.unidad, la.volumen_unitario_l, CASE WHEN la.volumen_unitario_l > 0 THEN ROUND(la.precio_unitario / la.volumen_unitario_l, 4) ELSE NULL END as precio_por_litro, p.nombre as proveedor, a.fecha FROM lineas_albaran la JOIN albaranes a ON la.albaran_id = a.id JOIN proveedores p ON a.proveedor_id = p.id WHERE la.descripcion_limpia ILIKE '%frimasol%' ORDER BY a.fecha DESC LIMIT 1

- "¿Cuántos kilos de tomate he comprado este mes?"
  SELECT la.descripcion_limpia, SUM(COALESCE(la.peso_total_kg, la.cantidad)) as kg_totales FROM lineas_albaran la JOIN albaranes a ON la.albaran_id = a.id WHERE la.descripcion_limpia ILIKE '%tomate%' AND la.unidad = 'kg' AND a.fecha >= date_trunc('month', CURRENT_DATE) GROUP BY la.descripcion_limpia

- "¿Cuántas unidades de garbanzos he comprado?"
  SELECT la.descripcion_limpia, SUM(la.cantidad) as unidades_totales FROM lineas_albaran la JOIN albaranes a ON la.albaran_id = a.id WHERE la.descripcion_limpia ILIKE '%garbanzo%' AND la.unidad = 'ud' GROUP BY la.descripcion_limpia

- "¿Cuánto chorizo he comprado?"
  SELECT la.descripcion_limpia, SUM(la.cantidad) as kg_totales, la.unidad FROM lineas_albaran la JOIN albaranes a ON la.albaran_id = a.id WHERE la.descripcion_limpia ILIKE '%chorizo%' GROUP BY la.descripcion_limpia, la.unidad ORDER BY la.descripcion_limpia

- "¿Cuánto chorizo compré en la última compra?"
  SELECT la.descripcion_limpia, SUM(la.cantidad) as kg_totales, la.unidad, a.fecha FROM lineas_albaran la JOIN albaranes a ON la.albaran_id = a.id WHERE la.descripcion_limpia ILIKE '%chorizo%' AND a.fecha = (SELECT MAX(a2.fecha) FROM albaranes a2 JOIN lineas_albaran la2 ON la2.albaran_id = a2.id WHERE la2.descripcion_limpia ILIKE '%chorizo%') GROUP BY la.descripcion_limpia, la.unidad, a.fecha

- "¿Cuánto he gastado en Lucas Caballero este mes?"
  SELECT p.nombre, SUM(a.total) as total_gastado FROM albaranes a JOIN proveedores p ON a.proveedor_id = p.id WHERE p.nombre ILIKE '%caballero%' AND a.fecha >= date_trunc('month', CURRENT_DATE) GROUP BY p.nombre

- "¿Cómo paga Lucas Caballero?"
  SELECT a.forma_pago, a.fecha FROM albaranes a JOIN proveedores p ON a.proveedor_id = p.id WHERE p.nombre ILIKE '%caballero%' ORDER BY a.fecha DESC LIMIT 1

- "Últimas 3 compras de longaniza blanca con precio y cantidad"
  SELECT la.descripcion_limpia, la.cantidad, la.unidad, COALESCE(la.peso_total_kg, la.cantidad) as cantidad_real, la.precio_unitario, a.fecha, p.nombre as proveedor FROM lineas_albaran la JOIN albaranes a ON la.albaran_id = a.id JOIN proveedores p ON a.proveedor_id = p.id WHERE la.descripcion_limpia ILIKE '%longaniza%' ORDER BY a.fecha DESC LIMIT 3

- "Total gastado por proveedor este mes"
  SELECT p.nombre, COUNT(a.id) as num_albaranes, SUM(a.total) as total FROM albaranes a JOIN proveedores p ON a.proveedor_id = p.id WHERE a.fecha >= date_trunc('month', CURRENT_DATE) GROUP BY p.nombre ORDER BY total DESC

- "¿Cuánto me ahorro con los descuentos de Lucas Caballero?"
  SELECT ROUND(SUM(CASE WHEN COALESCE(la.descuento_pct,0) > 0 THEN la.precio_unitario / (1 - la.descuento_pct/100) * la.cantidad ELSE la.precio_unitario * la.cantidad END), 2) as total_sin_descuento, ROUND(SUM(la.precio_unitario * la.cantidad), 2) as total_con_descuento, ROUND(SUM(CASE WHEN COALESCE(la.descuento_pct,0) > 0 THEN (la.precio_unitario / (1 - la.descuento_pct/100) - la.precio_unitario) * la.cantidad ELSE 0 END), 2) as ahorro FROM lineas_albaran la JOIN albaranes a ON la.albaran_id = a.id JOIN proveedores p ON a.proveedor_id = p.id WHERE p.nombre ILIKE '%caballero%' AND a.fecha >= date_trunc('month', CURRENT_DATE)

- "¿Qué IVA lleva el albarán de Lucas Caballero?"
  SELECT a.numero_albaran, a.fecha, (elem->>'tipo')::numeric as tipo_iva, (elem->>'base')::numeric as base, (elem->>'cuota')::numeric as cuota FROM albaranes a JOIN proveedores p ON a.proveedor_id = p.id, jsonb_array_elements(COALESCE(a.detalle_iva, '[]'::jsonb)) elem WHERE p.nombre ILIKE '%caballero%' AND a.detalle_iva IS NOT NULL ORDER BY a.fecha DESC LIMIT 20
"""

_INTERPRETACION_SYSTEM_PROMPT = """\
Eres un asesor de compras experimentado de un restaurante. Conoces el negocio y hablas como un empleado de confianza: directo, natural, sin florituras.

TONO:
- Una frase cuando basta, lista cuando hay varios elementos.
- Habla en primera persona del negocio: "te sale a", "pagaste", "tu último pedido", "llevas gastados".
- Nunca empieces con "Por supuesto", "Claro que sí", "He encontrado" ni menciones "la base de datos".
- Máximo 150 palabras.

FORMATO — OBLIGATORIO:
- Sin asteriscos ni markdown de ningún tipo (* ** # ` ~~).
- Para listas, una línea por elemento sin guión ni viñeta: "Producto — precio".
- Sin emojis.
- Números en formato español: punto para miles, coma para decimales. Ejemplo: 1.234,56€.
- Fechas en formato natural: "4 de mayo" o "4 de mayo de 2026", nunca "2026-05-04".

REGLAS DE CONTENIDO:
- Precios: precio_unitario es el precio neto (ya tiene el descuento aplicado).
  Con descuento: "El tomate de Lucas Caballero está a 1,81€/kg (tarifa 2,01€, descuento 10%). Último pedido el 4 de mayo."
  Sin descuento: "El queso cremette está a 8,82€/ud. Último pedido el 4 de mayo."
- Líquidos con precio_por_litro: "El aceite Frimasol está a 46,75€/garrafa de 25L (1,87€/litro). Último pedido el 4 de mayo."
- IVA desglosado: los datos vienen como filas separadas (una por tramo). Agrúpalos en la respuesta:
  "Lucas Caballero te aplicó dos tipos de IVA: 10% sobre 307,53€ → 30,75€ y 4% sobre 30,87€ → 1,23€. Total IVA: 31,98€."
  SIEMPRE muestra TODOS los tramos que vengan en los datos, no solo el primero.
- Ahorro por descuento: los datos tienen total_sin_descuento, total_con_descuento y ahorro.
  "Sin los descuentos pagarías [total_sin_descuento]€ de base. Te ahorraste [ahorro]€ (sobre base, sin IVA)."
  total_sin_descuento SIEMPRE es mayor que total_con_descuento.
- Totales: "En mayo llevas gastados 370,38€ con Lucas Caballero."
- Cantidades: "Has comprado 12 kg de tomate este mes."
- Forma de pago: "Con Lucas Caballero trabajas a 15 días."
- Sin datos: "No tengo ese dato registrado todavía."
"""


async def consultar(pregunta: str, historial: list[dict] | None = None) -> str:
    """
    Procesa una pregunta en lenguaje natural y retorna una respuesta en español.
    historial: lista de dicts {"pregunta": str, "respuesta": str} de turnos anteriores.
    """
    hoy = date.today().strftime("%Y-%m-%d")
    client = Mistral(api_key=settings.MISTRAL_API_KEY)

    # Construir contexto previo para inyectar en el paso SQL
    contexto_sql = ""
    if historial:
        lineas = []
        for t in historial:
            lineas.append(f"Usuario: {t['pregunta']}")
            lineas.append(f"Respuesta: {t['respuesta']}")
        contexto_sql = (
            "Contexto previo (para resolver referencias como 'lo mismo', "
            "'¿y el mes pasado?', '¿y de X?'):\n"
            + "\n".join(lineas)
            + "\n\nPregunta actual: "
        )

    # Paso 1: Generar SQL
    sql_prompt = _SQL_SYSTEM_PROMPT.format(schema=_SCHEMA_CONTEXT, hoy=hoy)
    logger.info("[query] Pregunta: %s", pregunta)
    try:
        response_sql = await _mistral_chat(
            client,
            messages=[
                {"role": "system", "content": sql_prompt},
                {"role": "user", "content": contexto_sql + pregunta},
            ],
            temperature=0.1,
            max_tokens=500,
        )
        sql = response_sql.choices[0].message.content.strip().rstrip(";").strip()
        # DISTINCT causa error si ORDER BY usa columna no seleccionada; es seguro quitarlo
        sql = sql.replace("SELECT DISTINCT", "SELECT").replace("select distinct", "select")
        logger.info("[query] SQL generado: %s", sql)
    except _CapacidadError:
        return _ERR_CAPACIDAD
    except Exception as e:
        logger.error("[query] Error generando SQL: %s", e, exc_info=True)
        return "No pude entender esa consulta. Prueba a reformularla."

    # Validación de seguridad
    sql_upper = sql.upper().strip()
    if not sql_upper.startswith("SELECT"):
        logger.warning("[query] SQL no comienza con SELECT: %s", sql)
        return "No pude entender esa consulta. Prueba a reformularla."

    for keyword in ["INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER", "CREATE"]:
        if keyword in sql_upper:
            logger.warning("[query] SQL contiene keyword peligroso '%s': %s", keyword, sql)
            return "Solo puedo responder preguntas de consulta sobre los datos registrados."

    # Paso 2: Ejecutar SQL
    try:
        logger.info("[query] Ejecutando SQL en Supabase...")
        rows = await db.ejecutar_sql(sql)
        logger.info("[query] Resultado: %d filas. Primera: %s", len(rows), rows[0] if rows else "vacío")
    except Exception as e:
        logger.error("[query] Error ejecutando SQL: %s\nSQL: %s", e, sql, exc_info=True)
        return "No pude ejecutar esa consulta. Prueba a reformularla."

    if not rows:
        logger.info("[query] Sin resultados para: %s", sql)
        return "No encontré datos para esa consulta. Puede que no haya albaranes registrados aún para ese período o proveedor."

    # Paso 3: Interpretar resultados
    try:
        datos_str = json.dumps(rows, ensure_ascii=False, default=str)
        logger.info("[query] Interpretando %d filas con Mistral...", len(rows))
        messages_interp: list[dict] = [{"role": "system", "content": _INTERPRETACION_SYSTEM_PROMPT}]
        if historial:
            for t in historial:
                messages_interp.append({"role": "user", "content": t["pregunta"]})
                messages_interp.append({"role": "assistant", "content": t["respuesta"]})
        messages_interp.append({"role": "user", "content": f"Pregunta: {pregunta}\n\nDatos:\n{datos_str}"})
        response_interp = await _mistral_chat(
            client,
            messages=messages_interp,
            temperature=0.3,
            max_tokens=400,
        )
        respuesta = response_interp.choices[0].message.content.strip()
        logger.info("[query] Respuesta generada OK (%d chars)", len(respuesta))
        return respuesta
    except _CapacidadError:
        return _ERR_CAPACIDAD
    except Exception as e:
        logger.error("[query] Error interpretando resultados: %s", e, exc_info=True)
        if len(rows) == 1 and len(rows[0]) == 1:
            valor = list(rows[0].values())[0]
            return f"Resultado: {valor}"
        return "Obtuve los datos pero no pude interpretarlos. Inténtalo de nuevo."
