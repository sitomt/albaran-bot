# 🚀 Albaran Bot — Progress Tracker
## Estado global: COMPLETADO

### ✅ Completado
- [x] PROGRESS.md creado
- [x] sql/schema.sql creado y migración aplicada en Supabase (proyecto tdyeivstcmtbmzuzrimd)
- [x] 6 tablas creadas: proveedores, productos_catalogo, albaranes, lineas_albaran, auditoria, jobs
- [x] 4 índices creados: idx_albaranes_fecha, idx_albaranes_proveedor_fecha, idx_lineas_producto, idx_productos_proveedor
- [x] RPC execute_select() creada para consultas dinámicas seguras
- [x] Inserción de prueba en proveedores verificada y borrada ✓
- [x] .env plantilla con las 5 keys necesarias
- [x] src/config.py — pydantic-settings con mensajes de error descriptivos por key
- [x] src/supabase_client.py — wrapper async completo con todos los métodos
- [x] src/product_normalizer.py — normalización LLM con fallback simple
- [x] src/albaran_processor.py — pipeline completo OCR→LLM→validación→BD
- [x] src/queue_manager.py — cola asyncio con worker y recovery al arranque
- [x] src/query_engine.py — NL→SQL→interpretación en español
- [x] src/bot.py — bot completo con todos los comandos, handlers y resumen semanal
- [x] src/__init__.py — módulo Python
- [x] requirements.txt
- [x] README.md con instrucciones completas

### 🔄 En progreso
- (ninguno)

### ⏳ Pendiente
- (ninguno)

### Correcciones de datos — 2026-05-19

**Anchoa Cantábrico** (lineas_albaran ID: 668f600a)
- cantidad: 25.260 → 3.000 (el OCR confundió precio_tarifa 25,26 con la cantidad)
- unidad: 'kg' → 'ud' (son 3 cajas, no kg)
- peso_total_kg: 25.260 → NULL (peso por caja no disponible en el albarán)

**Albarán 26/2.968 Lucas Caballero** (albaranes ID: 78aa316c)
- base_imponible: 307.53 → 338.40 (faltaba la base del tramo al 4%)
- total_iva: 30.75 → 31.98 (faltaba la cuota IVA al 4% del queso)
- detalle_iva: [{"tipo":10,"base":307.53,"cuota":30.75},{"tipo":4,"base":30.87,"cuota":1.23}]

**Cantidades verificadas y correctas tras corrección:**
- Aceite Frimasol: 2 garrafas ✓
- Atún aceite bolsa: 16 ud ✓
- Anchoa Cantábrico: 3 ud ✓ (corregido)
- Garbanzos Miau lata: 6 ud ✓
- Harina Freír Miau: 10 ud ✓
- Queso Cremette Cubo: 1 ud ✓
- Tomate Entero: 12 kg ✓

### Mejoras de código — 2026-05-19 (ronda 1)

- supabase_client.py: insertar_albaran() acepta detalle_iva JSONB
- albaran_processor.py: modelo DetalleIvaLLM + extracción del desglose IVA en el prompt
- query_engine.py: reglas de descuento más explícitas, soporte consultas IVA con jsonb_array_elements, ejemplos de interpretación de IVA y ahorro

### Correcciones de código — 2026-05-19 (ronda 2)

- query_engine.py: sección F nueva para análisis de descuentos basado en importe_neto
  Fórmula correcta: SUM(importe_neto / (1 - descuento_pct/100)) para total_sin_descuento
  Usa importe_neto (no precio_unitario × cantidad) para ser consistente con productos precio-por-peso
  Resultado esperado para Lucas Caballero: total_pagado=338,40€, total_sin_desc=355,30€, ahorro=16,90€
- query_engine.py: sección E IVA corregida con COALESCE + IS NOT NULL para manejar nulos
- query_engine.py: interpretación IVA actualizada para mostrar TODOS los tramos del array
- query_engine.py: validación explícita en prompt: total_sin_descuento >= total_pagado siempre

### Mejoras de código — 2026-05-19 (ronda 3)

**PARTE 1 — Supabase DDL:**
- lineas_albaran: columnas confianza INT DEFAULT 100 y requiere_revision BOOLEAN DEFAULT false añadidas
- tabla correcciones creada (id, linea_albaran_id FK, campo, valor_original, valor_corregido, corregido_por, creado_en)

**PARTE 2 — query_engine.py — fórmula de descuentos corregida:**
- Sección F reescrita: usa precio_unitario × cantidad (no importe_neto) como base
  total_sin_descuento = SUM(precio_unitario / (1 - pct/100) × cantidad)  — SIEMPRE mayor
  total_con_descuento = SUM(precio_unitario × cantidad)
  ahorro = total_sin_descuento - total_con_descuento
- Validación esperada Lucas Caballero: total_sin=330,82€, total_con=316,37€, ahorro=14,45€
- Prompt interpretación actualizado: referencia a total_sin_descuento y total_con_descuento

**PARTE 3 — albaran_processor.py — validación y confianza:**
- LineaAlbaranLLM: campo confianza (0-100) con validator
- _validar_linea(): valida precio, cantidad, nombre y consistencia importe
- Prompt actualizado: campo confianza en JSON + corrección de erratas
- ResultadoProcesamiento: campo lineas_para_confirmacion (list[dict])
- Pipeline: inserta confianza y requiere_revision, recopila líneas dudosas tras insertar

**PARTE 4 — supabase_client.py — nuevos métodos:**
- listar_lineas_pendientes_revision()
- registrar_correccion()
- actualizar_linea_albaran()

**PARTE 5 — queue_manager.py — confirmaciones:**
- _pending_confirmations dict + _TIMEOUT_CONFIRMACION (10 min)
- _formatear_confirmacion() para mensajes con líneas dudosas
- Worker: usa confirmación cuando hay lineas_para_confirmacion, sino flujo normal

**PARTE 6 — bot.py — flujo interactivo + /revisiones:**
- cmd_revisiones: lista líneas con requiere_revision=true
- _procesar_confirmacion(): parsea respuestas "N: valor", aplica correcciones a BD
- handle_text: comprueba confirmaciones pendientes antes de NL query
- /revisiones añadido a _PRESENTACION y a app.add_handler()

### Protecciones contra datos incorrectos — 2026-05-19

**1. Detección de duplicados robusta**
- Nueva columna `numero_albaran_norm TEXT` en albaranes + índice idx_albaranes_num_norm
- Función `_normalizar_numero_albaran()`: elimina espacios, puntos, barras, guiones → minúsculas
  Ejemplo: '26/2.968' = '26 / 2.968' = '26-2968' → '262968' (mismo resultado)
- Nuevo método `buscar_albaran_duplicado_norm()` en supabase_client.py
- Mensaje mejorado: "Este albarán ya está registrado. Nº 26/2.968 de Lucas Caballero, procesado el 19 de mayo de 2026."
- Albarán existente actualizado: numero_albaran_norm='262968' ✓

**2. Clasificación pre-OCR del documento**
- Función `_clasificar_documento()` llama a mistral-small-2506 con la imagen (vision)
- Ejecuta ANTES del OCR para no gastar tokens en documentos no válidos
- Si es_albaran=false O confianza<60: rechaza con mensaje explicativo
- Si clasificación falla (error de API): continúa con OCR (fallback graceful)

**3. Validación mínima del JSON extraído**
- Función `_validar_datos_minimos()`: verifica proveedor.nombre, fecha válida y no futura, len(lineas)>=1, total>0, al menos 1 línea con precio
- Ejecuta después de la extracción LLM, antes de insertar en BD
- Si falla: rechaza con mensaje "foto nítida y documento completo"

**4. Lista negra de documentos no válidos**
- `_BLACKLIST` con palabras: nómina, salario, empleado, factura de luz, electricidad, gas natural, alquiler, arrendamiento, recibo, extracto bancario
- Ejecuta post-OCR sobre el texto extraído, antes de llamar al LLM de extracción
- Si detecta palabra prohibida: rechaza con tipo de documento identificado

### Constraint UNIQUE (proveedor, fecha, total) — 2026-05-19 (ronda 7)

**Índice:** `CREATE UNIQUE INDEX idx_albaran_pft ON albaranes(proveedor_id, fecha, total) WHERE total IS NOT NULL`
**Verificado:** INSERT con número "3.950" (mismo proveedor/fecha/total que 26/2.968) → error 23505 ✓
**Handler en albaran_processor.py:** captura "23505" en el INSERT → devuelve es_duplicado=True
**Mensaje al usuario:** "Este albarán ya está registrado.\nMismo proveedor, fecha y total que un albarán procesado anteriormente."
Sin error técnico visible al usuario.

### Detección duplicados por combinación — 2026-05-19 (ronda 6)

**Problema:** albarán Lucas Caballero tiene dos números (interno: 3.950, factura: 26/2.968).
El OCR extrae uno u otro → comparación por número falla.

**Solución:** detección principal por proveedor_id + fecha + total (±0.50€):
- `supabase_client.py`: nuevo método `buscar_albaran_duplicado_combinacion(proveedor_id, fecha, total)`
- `albaran_processor.py` step g: primero comprueba combinación; si no encuentra, comprueba número normalizado
- `ResultadoProcesamiento`: nuevo campo `es_duplicado_numero_original` (número del registro en BD)
- Mensaje bot: "Mismo proveedor, fecha y total que el nº [original] de [proveedor], procesado el [fecha]."

**Detección en capas (de más a menos fiable):**
1. Proveedor + fecha + total ±0.50€ (soporta números distintos del mismo albarán)
2. Número normalizado (fallback si total no disponible)
3. UNIQUE index en BD sobre numero_albaran_norm (última red de seguridad)

### Protección duplicados nivel BD — 2026-05-19 (ronda 5)

**Problema:** detección de duplicados falló en producción — mismo albarán procesado dos veces.

**Solución implementada (doble capa):**
1. `CREATE UNIQUE INDEX idx_albaran_unico ON albaranes(numero_albaran_norm, proveedor_id) WHERE numero_albaran_norm IS NOT NULL;`
   — La BD rechaza cualquier INSERT duplicado con error 23505, incluso si el código falla.
2. `albaran_processor.py` captura el error 23505 en el INSERT y devuelve ResultadoProcesamiento(es_duplicado=True)
   igual que si la detección previa hubiera funcionado → el bot responde "Este albarán ya está registrado."

**Verificado:** intento de insertar 26/2.968 de Lucas Caballero → error 23505 ✓

### Deduplicación robusta y filtro estricto — 2026-05-20

**Causa raíz del problema:** El OCR de Mistral leía el NIF del proveedor de forma diferente en cada subida (B30402812 vs 4020059M). Esto creaba un segundo proveedor con distinto proveedor_id, haciendo que la comprobación proveedor_id+fecha+total no encontrara el duplicado.

**Limpieza de BD ejecutada:**
- Borrado albarán duplicado de48113a (Lucas Caballero, 2026-05-20, 370.38€)
- Borrado proveedor fantasma c5ee0b3a (NIF erróneo 4020059M)
- Borrados productos_catalogo del proveedor fantasma
- Estado final: 1 albarán, 1 proveedor, 7 líneas ✓

**Migración DDL aplicada:**
- `albaranes.imagen_hash TEXT` + UNIQUE index (deduplicación exacta por foto)
- `proveedores.nombre_normalizado TEXT` + índice (búsqueda por nombre case-insensitive)
- `proveedores.nif_normalizado TEXT` + índice (comparación sin guiones ni espacios)

**Fix 1 — Hash de imagen (capa 0, la más temprana):**
- `bot.py`: calcula SHA-256 de los bytes antes de encolar
- Si la foto ya existe en BD → responde inmediatamente sin gastar tokens OCR
- `supabase_client.py`: `buscar_albaran_por_hash()`
- `albaran_processor.py`: pasa `imagen_hash` a `insertar_albaran()`

**Fix 2 — Lookup de proveedor: el primero que entra, manda:**
- `supabase_client.py`: nueva función `buscar_o_crear_proveedor(nombre, nif)`
  1. Busca por NIF normalizado (sin guiones, uppercase)
  2. Si no → busca por nombre normalizado (case-insensitive)
  3. Si no → crea nuevo proveedor
  - El NIF del OCR se descarta silenciosamente si ya existe el proveedor por nombre
- `albaran_processor.py`: usa `buscar_o_crear_proveedor()` en lugar de lógica manual

**Fix 3 — 4ª capa de detección de duplicados:**
- `supabase_client.py`: `buscar_albaran_duplicado_por_nombre_proveedor(nombre, fecha, total)`
  - SQL con JOIN proveedores filtrando por LOWER(nombre) + fecha + total ±0.50€
  - Detecta duplicados aunque el proveedor_id sea distinto por NIF mal leído
- `albaran_processor.py`: orden de comprobaciones antes del INSERT:
  1. Hash de imagen (en bot.py antes de encolar)
  2. proveedor_id + fecha + total ±0.50€ (existente)
  3. nombre proveedor + fecha + total ±0.50€ (nuevo — Fix 3)
  4. número normalizado + proveedor_id (existente)
  5. Constraint UNIQUE en BD como red de seguridad (existente)

**Fix 4 — Clasificación de documentos con tipo explícito:**
- `albaran_processor.py`: `_clasificar_documento()` ahora pide al LLM identificar el tipo en lista cerrada:
  albaran_proveedor / factura / ticket / nomina / recibo_servicio / otro
- Solo continúa si tipo == "albaran_proveedor" y confianza >= 75 (antes: es_albaran+confianza>=60)
- Mensaje de rechazo específico según el tipo detectado
- Evita que facturas de luz, tickets o facturas fiscales entren al pipeline

### ⚠️ Problemas encontrados
- OCR confundió precio_tarifa de anchoa (25,26) con la cantidad → corregido manualmente y prompt actualizado
- IVA al 4% del queso cremette no estaba desglosado → columna detalle_iva añadida y datos corregidos

### ⚠️ Nota de seguridad
Las tablas de Supabase tienen RLS (Row Level Security) deshabilitado.
Para este bot de uso privado es suficiente, pero si quieres restricción adicional
puedes habilitarla desde el dashboard de Supabase (requiere definir políticas de acceso primero).

### 🔑 Keys pendientes de rellenar en .env
- MISTRAL_API_KEY — https://console.mistral.ai/api-keys
- SUPABASE_URL — https://supabase.com/dashboard/project/tdyeivstcmtbmzuzrimd/settings/api
- SUPABASE_ANON_KEY — mismo panel anterior
- TELEGRAM_BOT_TOKEN — @BotFather en Telegram
- TELEGRAM_ADMIN_CHAT_ID — @userinfobot en Telegram (opcional, para resumen semanal)

### 📋 Paso manual adicional
Crear el bucket "albaranes" en Supabase Storage:
https://supabase.com/dashboard/project/tdyeivstcmtbmzuzrimd/storage/buckets
→ New bucket → Nombre: albaranes → Public bucket ✓

### 🚀 Para arrancar
```
pip install -r requirements.txt
# Rellenar .env con las keys
python -m src.bot
```
