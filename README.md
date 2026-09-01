# Albarán Bot — control de compras

Bot privado de Telegram para los dos propietarios de un restaurante. Recibe
fotografías de albaranes, conserva el original, extrae los datos, aplica controles
contables deterministas y obliga a revisar el candidato antes de publicarlo.

El principio central es: **un resultado de IA nunca es un dato contable por sí
solo**. La base de datos canónica solo contiene albaranes confirmados mediante una
operación atómica; OCR, respuestas del modelo, cálculos, correcciones y decisiones
humanas quedan versionados y auditados.

## Qué resuelve

- Recepción durable: el original privado se guarda antes de responder “recibido”.
- Idempotencia por identificador de Telegram, SHA-256, clave lógica y número de
  albarán normalizado.
- Hash perceptual canónico resistente a recompressión/rotación para avisar de
  fotos visualmente similares, incluso si ambas siguen en cola; nunca descarta
  automáticamente por similitud.
- Duplicados probables por proveedor, fecha y total: se muestran al usuario, no se
  descartan automáticamente.
- Manuscritos, OCR incompleto o baja confianza: siempre pasan a revisión.
- Separación explícita entre precio neto, descuento, importe de línea, base, IVA y
  total; ninguna cifra observada se sobrescribe silenciosamente con un cálculo.
- Validación de cantidad × precio, suma de líneas, tramos de IVA y total.
- Entrada manual guiada, reutilización de una foto fallida, corrección versionada
  de candidatos y archivo auditado de confirmados erróneos.
- Cola persistente con leases, reintentos y recuperación tras reinicios.
- Límite de carga por usuario/global y presupuesto mensual de IA.
- Auditoría append-only, métricas, feedback, backups y restauración ensayable.
- Consultas en lenguaje natural mediante intenciones cerradas y PostgREST
  parametrizado; el modelo no genera ni ejecuta SQL.

## Flujo de datos

```mermaid
flowchart LR
    A["Foto en Telegram"] --> B["Validar tipo y tamaño"]
    B --> C["Guardar original privado"]
    C --> D["Crear ingesta y job durable"]
    D --> E["OCR + clasificación + extracción"]
    E --> F["Artefactos versionados"]
    F --> G["Validación contable"]
    G --> H["Revisión humana"]
    H --> I["RPC atómica de confirmación"]
    I --> J["Albarán canónico confirmado"]
```

## Estado del proyecto

La base remota ya tiene las migraciones `000`–`009`, el contrato PostgreSQL pasa,
el bucket es privado y las 12 tablas verificadas tienen RLS. La restauración real
aislada se repitió sobre ese esquema y conserva 9 proveedores, 67 productos, 10
albaranes y 72 líneas. Los 73 eventos históricos de IA se reconciliaron y las
llamadas posteriores no dejan spool pendiente. El backup registra
`system.backup.completed` y ya existe un evento real verificable.

El [dashboard v2 privado](dashboard/) está desplegado, conectado a una RPC agregada
ejecutada exclusivamente desde backend y se refresca cada 30 segundos; la
`service_role` nunca se entrega al navegador. La imagen final y el healthcheck
profundo son correctos. La suite actual tiene 123 pruebas correctas.

Esto todavía no constituye un go-live aprobado. Falta añadir el segundo usuario de
Telegram, elegir el host y sus alertas externas, configurar costes fijos reales y
realizar la aceptación con ambos propietarios. Consulta [PROGRESS.md](PROGRESS.md)
para el checklist vigente.

## Requisitos

- Python 3.12 para desarrollo o Docker Engine + Compose v2 para producción.
- Proyecto Supabase/PostgreSQL y bucket privado `albaranes`.
- Bot de Telegram dedicado.
- Clave de Mistral.
- `pg_dump`/`pg_restore` para backups.

## Preparación

```sh
cp .env.example .env
chmod 600 .env
python -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.lock
python -m pytest -q
```

En producción son obligatorios `SUPABASE_SERVICE_ROLE_KEY` y una whitelist no
vacía en `TELEGRAM_ALLOWED_USERS`. La credencial `service_role` pertenece solo al
backend/contenedor y al proceso de backup: nunca se entrega a clientes, mensajes,
logs ni repositorios. `SUPABASE_ANON_KEY` solo mantiene compatibilidad local.

## Base de datos

Las migraciones son la fuente de verdad y se aplican en orden:

```sh
psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -f sql/schema.sql
```

Antes de aplicar sobre un proyecto existente hay que crear y verificar un backup.
La migración hace privado el bucket, revoca acceso de clientes, elimina la antigua
RPC de SQL dinámico y modifica constraints; por eso requiere ventana de
mantenimiento. El contrato completo está en [sql/README.md](sql/README.md).

```sh
psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -f sql/preflight.sql
```

## Arranque de producción

```sh
IMAGE_TAG=<revision-inmutable> docker compose build --pull
IMAGE_TAG=<revision-inmutable> docker compose up -d
docker compose ps
docker compose logs --tail=100 bot
docker compose exec bot python /app/ops/healthcheck.py --deep
```

Debe ejecutarse una sola réplica del bot por token de Telegram. El contenedor es
no-root, read-only, sin capabilities y con límites de CPU, memoria y procesos.

## Uso

- Envía una foto y conserva la referencia corta que responde el bot.
- `/revisiones`: documentos pendientes.
- `/revisar REFERENCIA`: candidato, incidencias y decisiones.
- `/ultimos`: referencias de los últimos albaranes.
- `/detalle REFERENCIA`: cabecera y productos de un confirmado.
- `/corregir REFERENCIA total 123,45`.
- `/corregir REFERENCIA linea 2 importe 47,25`.
- `/corregir REFERENCIA linea 2 nombre Sardina`.
- `/anular REFERENCIA motivo`: archiva un confirmado incorrecto sin borrar su
  historia; después se registra el correcto con `/manual`.
- `/manual`: alta guiada para manuscritos o documentos ilegibles; permite indicar
  `BASE / IVA / TOTAL`, rechaza descuadres, deja añadir portes como líneas y
  admite `nombre | cantidad | tarifa | descuento | neto | importe`.
- `/manual REFERENCIA`: transcribe a mano una ingesta fallida o bloqueada usando
  el mismo original, sin volver a subir la fotografía.
- `/reintentar REFERENCIA`: reprocesa un fallo conservando el original.
- `/estado`: cola.
- `/metricas`: confirmados, fallidos, revisiones y coste mensual.
- `/costes`: snapshot actualizable con coste de hoy/mes por operación y modelo,
  llamadas, páginas OCR, tokens de entrada/salida, últimas llamadas y costes fijos.
- `/feedback texto`: observación general persistente para el operador.
- `/feedback REFERENCIA texto`: observación asociada al documento para poder
  investigar el OCR, candidato y decisiones exactas.
- `/resumen`, `/proveedores` y preguntas directas sobre compras confirmadas.

`AUTO_CONFIRM_CLEAN=false` es obligatorio en producción: incluso un documento
perfectamente cuadrado necesita la confirmación de uno de los propietarios. La
configuración falla al arrancar si se intenta activar allí.

Cada respuesta facturable se registra inmediatamente en el ledger append-only
`ai_usage_events`: OCR, clasificación visual, extracción, clasificación de consultas
y redacción. Si Supabase no está disponible, el evento se guarda con `fsync` en
el volumen persistente de runtime y `/costes` lo incluye hasta reconciliarlo. Los
costes fijos no se pueden inferir de una llamada API; se declaran
como `HOSTING_MONTHLY_COST_USD`, `SUPABASE_MONTHLY_COST_USD` y
`OTHER_MONTHLY_COST_USD` y se muestran separados del consumo medido.

## Operación y recuperación

La plataforma recomendada para producción es Supabase Pro con un dashboard
operativo propio. La comparación Free/Pro/Team, la decisión sobre PITR y los límites
entre coste estimado y factura real están en
[docs/SUPABASE_DECISION.md](docs/SUPABASE_DECISION.md).

El procedimiento de despliegue, backup diario, restauración aislada, rollback,
monitorización e incidentes está en [docs/OPERATIONS.md](docs/OPERATIONS.md). La
auditoría técnica inicial y riesgos encontrados están en
[AUDITORIA_PRODUCCION.md](AUDITORIA_PRODUCCION.md). La evaluación iterativa de
las 11 fotografías reales está en
[docs/ACCEPTANCE_REPORT.md](docs/ACCEPTANCE_REPORT.md).

Comandos principales de backup:

```sh
scripts/backup_database.sh --output-dir /ruta/cifrada/albaran-backups
scripts/verify_backup.sh /ruta/cifrada/albaran-backups/<timestamp>
```

Sin contraseña PostgreSQL puede crearse una exportación de emergencia de filas y
Storage con `scripts/backup_emergency_api.sh`. No contiene roles, índices,
triggers ni funciones y nunca sustituye al backup verificable con `pg_dump`. Sus
filas pueden reconstruirse sobre un baseline vacío con
`scripts/restore_emergency_api.sh`; el comando aborta si el destino ya contiene
datos y valida todos los conteos antes del commit.

Nunca se restaura directamente sobre producción como primera prueba. Se restaura
en un proyecto aislado, se verifican conteos y una muestra de originales, y solo
entonces se decide el procedimiento de recuperación.

## Estructura

- `src/intake_service.py`: recepción durable, límites y hashes.
- `src/ingestion_service.py`: OCR, clasificación, extracción y artefactos.
- `src/accounting_validation.py`: invariantes contables deterministas.
- `src/review_service.py`: revisión y correcciones versionadas.
- `src/queue_manager.py`: workers persistentes y leases.
- `src/query_engine.py`: consultas mediante rutas permitidas.
- `sql/migrations`: contrato y seguridad de PostgreSQL.
- `ops` y `scripts`: healthchecks, backup y restauración.
- `tests`: regresiones contables, seguridad e ingesta durable.
