# Albaran Bot — Gestor de Compras

Bot de Telegram para gestión automática de albaranes de restaurante.
Procesa fotos de albaranes con OCR + IA y permite consultas en lenguaje natural sobre gastos y proveedores.

## Requisitos

- Python 3.12+
- Cuenta en [Mistral AI](https://console.mistral.ai)
- Proyecto Supabase: `tdyeivstcmtbmzuzrimd` (ya configurado)
- Bot de Telegram (créalo en [@BotFather](https://t.me/BotFather))

## Instalación

```bash
pip install -r requirements.txt
```

## Configuración

Edita el archivo `.env` con tus claves:

```
MISTRAL_API_KEY=        # https://console.mistral.ai/api-keys
SUPABASE_URL=           # https://supabase.com/dashboard/project/tdyeivstcmtbmzuzrimd/settings/api
SUPABASE_ANON_KEY=      # mismo panel
TELEGRAM_BOT_TOKEN=     # @BotFather en Telegram
TELEGRAM_ADMIN_CHAT_ID= # @userinfobot para obtener tu ID
```

## Supabase Storage

Antes de arrancar, crea el bucket `albaranes` en Supabase:
1. Ve a [Storage](https://supabase.com/dashboard/project/tdyeivstcmtbmzuzrimd/storage/buckets)
2. Clic en "New bucket"
3. Nombre: `albaranes`
4. Marca "Public bucket"
5. Guardar

## Arranque

```bash
python -m src.bot
```

O bien:

```bash
cd /ruta/al/albaran-bot
python src/bot.py
```

## Uso

### Procesar albaranes
Envía una foto del albarán al bot. El bot:
1. Acusa recibo inmediatamente
2. Aplica OCR con Mistral
3. Extrae datos estructurados
4. Guarda en Supabase
5. Responde con resumen: proveedor, número, total, alertas de precio

### Consultas en lenguaje natural
Escribe directamente al bot:
- `¿Cuánto he gastado en De Bandera este mes?`
- `¿Cuánto me han cobrado por longaniza blanca en las últimas 3 compras?`
- `Total gastado por proveedor este mes`
- `¿Qué productos han subido de precio?`

### Correcciones
- `Corregir total de abc12345: 370.38`
- `Corregir producto abc12345: Longaniza Blanca L/S`

### Comandos
| Comando | Descripción |
|---------|-------------|
| `/start` | Presentación |
| `/estado` | Estado de la cola de procesamiento |
| `/resumen` | Resumen de gastos de la semana |
| `/proveedores` | Lista de proveedores registrados |
| `/ayuda` | Ejemplos de consultas |

## Resumen semanal automático
Cada lunes a las 9:00 (hora de Madrid) el bot envía al admin:
- Gasto total de la semana anterior
- Top proveedores
- Productos con variación de precio

Para activarlo, rellena `TELEGRAM_ADMIN_CHAT_ID` en `.env`.

## Base de datos

La base de datos ya está configurada en Supabase (`tdyeivstcmtbmzuzrimd`).
El schema completo está en `sql/schema.sql`.

Tablas: `proveedores`, `productos_catalogo`, `albaranes`, `lineas_albaran`, `auditoria`, `jobs`

## Estructura del proyecto

```
albaran-bot/
├── .env                    ← Claves de API (no compartir)
├── requirements.txt
├── sql/schema.sql          ← Schema de BD (ya aplicado)
└── src/
    ├── bot.py              ← Punto de entrada
    ├── config.py           ← Validación de configuración
    ├── supabase_client.py  ← Acceso a BD
    ├── albaran_processor.py ← Pipeline OCR + extracción
    ├── product_normalizer.py ← Normalización de productos
    ├── queue_manager.py    ← Cola de procesamiento
    └── query_engine.py     ← Consultas NL → SQL
```
