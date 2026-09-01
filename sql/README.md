# Contrato de base de datos

La fuente de verdad son las migraciones numeradas de `sql/migrations`. No se
aplican automáticamente desde la aplicación y nunca deben ejecutarse con la
clave `anon`.

## Orden de despliegue

1. Backup de PostgreSQL y del bucket `albaranes`.
2. Ensayo de restauración en un proyecto Supabase aislado.
3. Ejecutar `sql/preflight.sql` y resolver explícitamente cualquier dato legado
   que bloquee constraints o índices únicos.
4. Detener el bot y aplicar las migraciones en orden dentro de una ventana de
   mantenimiento.
5. Ejecutar las consultas de verificación de este documento.
6. Confirmar que el bucket es privado.
7. Desplegar la aplicación que usa `service_role` y ya no depende de
   `execute_select`, y completar una prueba de humo antes de habilitar a usuarios.

`001_production_core.sql` es forward-only. No se incluye un rollback destructivo:
si el despliegue falla, se restaura el backup probado. La migración no se ha
aplicado a ningún entorno remoto desde este repositorio.

## Estados

`ingestions.status`:

`received -> queued -> processing -> extracted -> needs_review -> confirmed`

`rejected` y `failed` son terminales. El original debe estar guardado en
`storage_bucket/storage_path` antes de cambiar de `received` a `queued`.

`ingestions.source_type` diferencia `telegram_photo` y `manual`. Para
`telegram_photo` son obligatorios bucket, ruta, MIME, SHA-256 y un tamaño
positivo. Una ingesta `manual` puede no tener foto; en ese caso usa
`byte_size=0` y deja los cuatro campos de evidencia a `null`. Si se adjunta una
foto durante `/manual`, debe enviarse el conjunto completo y se aplican las
mismas restricciones que a una foto normal.

`albaranes.status` separa candidatos de datos publicables: `draft`,
`needs_review`, `confirmed`, `rejected`, `archived`. Los informes solo deben
leer `confirmed`.

Cada `review_items` pertenece obligatoriamente a una versión concreta de
`extraction_artifacts`. La unicidad es por artefacto + entidad + campo, no por
ingesta: reprocesar crea un artefacto nuevo y conserva las decisiones tomadas
sobre versiones anteriores.

## Identidad y duplicados

Son identidades fuertes y únicas:

- `ingestions.idempotency_key`;
- SHA-256 exacto del original (`ingestions.image_hash`);
- `telegram_user_id + telegram_file_unique_id`, cuando Telegram lo proporciona;
- `proveedor_id + numero_albaran_norm`, si hay número;
- una única confirmación por `ingestion_id`.

La unicidad del SHA-256 usa un índice parcial (`image_hash IS NOT NULL`), por lo
que múltiples altas manuales sin imagen siguen dependiendo de su clave
idempotente y de proveedor + número de albarán, no de un hash ficticio.

Proveedor + fecha + total **no es único**. Se puede usar para presentar un
duplicado probable, pero nunca para descartar automáticamente una entrega.

## RPC `claim_ingestion_job_v1`

Argumentos:

- `p_worker_id text`: identificador no vacío del worker.
- `p_lease_seconds integer = 300`: entre 30 y 3.600 segundos.

Devuelve cero o una fila de `jobs`. Reclama con `FOR UPDATE SKIP LOCKED`, aumenta
`attempts`, sincroniza el legado `intentos`, establece el lease y cambia la
ingesta a `processing`. Un worker solo debe completar un job cuyo
`lease_owner` coincida con su identificador.

## RPC `confirm_albaran_v1`

Argumentos:

- `p_ingestion_id uuid`;
- `p_idempotency_key text`;
- `p_actor_type text`: `telegram_user`, `operator` o `system`;
- `p_actor_id text`;
- `p_albaran jsonb`;
- `p_lineas jsonb`;
- `p_extraction_artifact_id uuid = null`.

Cabecera mínima:

```json
{
  "proveedor_nombre": "Proveedor Ejemplo S.L.",
  "proveedor_nif": "B-12345678",
  "numero_albaran": "A-123",
  "fecha": "2026-08-06",
  "forma_pago": "transferencia",
  "base_imponible": 100.00,
  "total_iva": 10.00,
  "total": 110.00,
  "detalle_iva": [{"tipo": 10, "base": 100, "cuota": 10}],
  "origen": "ocr"
}
```

`proveedor_id` es opcional. Si no se envía, la RPC resuelve o crea el proveedor
en la misma transacción usando `proveedor_nombre` (obligatorio),
`proveedor_nif`, `proveedor_direccion`, `proveedor_telefono` y
`proveedor_email`. La resolución bloquea por NIF normalizado o nombre para
evitar carreras. Si se proporciona `proveedor_id`, se conserva el contrato
anterior y se exige que exista.

Cada línea requiere descripción, `cantidad > 0` e `importe_neto >= 0`.
`precio_unitario`, si existe, es el **precio neto unitario aceptado**; el
descuento es información de procedencia y no se vuelve a aplicar. Para no
perder evidencia, cada línea admite `valores_observados`,
`valores_calculados` y `decisiones` como objetos JSON.

La función, en una única transacción:

- bloquea reintentos de la misma clave idempotente;
- devuelve el resultado anterior si clave y payload coinciden;
- rechaza reutilizar una clave con otro payload;
- exige que no queden `review_items` abiertos;
- valida líneas, base, tramos de IVA, cuota y total con tolerancias de céntimos;
- inserta cabecera y líneas completas;
- confirma la ingesta y completa el job;
- añade `albaran.confirmed` al registro inmutable `audit_events`.

La confirmación humana usa `accept_confirm_candidate_v1`: compara bajo bloqueo el
`candidate_artifact_id` mostrado al propietario con la versión actual, verifica
que cabecera y líneas coincidan con ese artefacto, resuelve sus revisiones y llama
a la publicación canónica en la misma transacción. `reject_ingestion_v1` aplica el
mismo compare-and-swap y nunca rechaza una ingesta ya confirmada.

`retry_ingestion_v1` bloquea conjuntamente la ingesta fallida y su job en error;
ambos pasan a `queued`/`pendiente` o ninguno cambia.

Respuesta:

```json
{
  "albaran_id": "uuid",
  "status": "confirmed",
  "idempotent": false,
  "line_count": 4,
  "version": 1
}
```

## Seguridad

Todas las tablas tienen RLS habilitado y no hay políticas para `anon` ni
`authenticated`. Se revocan sus privilegios de tablas y RPC. El backend es el
único cliente de la base y utiliza `service_role`; esa credencial no puede
exponerse en Telegram, logs, frontend o repositorio.

`execute_select(text)` se elimina deliberadamente: aceptar SQL dinámico dentro
de una función `SECURITY DEFINER` permite escapar cualquier filtro textual. Los
informes deben implementarse como consultas parametrizadas y explícitamente
permitidas.

El bucket `albaranes` queda privado y se revoca acceso directo de `anon` y
`authenticated` a las tablas de Storage. `storage_path` no es una URL pública. Para
mostrar un original se usan bytes servidos por el backend o una URL firmada de
corta duración.

## Costes de IA

`ai_usage_events` es el ledger append-only de llamadas facturables. Se inserta
una fila después de cada respuesta remota, tanto para OCR como para extracción,
clasificación u otra operación. Conserva proveedor, modelo, tokens de entrada y
salida, páginas, reintentos, usuario y precios unitarios vigentes en el momento
de la llamada. `cost_usd` es el coste final calculado por la aplicación según la
unidad de facturación del proveedor; la fórmula o particularidades (tokens en
caché, lotes, descuentos) se guardan en `metadata`.

Los precios de tokens se expresan en USD por millón de tokens y el precio de
página en USD por página. No se recalculan históricos cuando cambie una tarifa.
Si el proveedor entrega un identificador de petición, debe guardarse en
`request_id`; su índice único evita contabilizar dos veces la misma llamada al
reintentar una escritura.

La aplicación inserta y reconcilia estos eventos mediante
`append_ai_usage_event_v1(jsonb)`. La RPC usa exclusivamente
`ON CONFLICT DO NOTHING`: un replay por UUID o por `provider + request_id` se
considera reconciliado sin ejecutar un `UPDATE` contra el trigger append-only.

Ejemplo de coste diario:

```sql
select created_at::date as dia,
       operation,
       provider,
       model,
       sum(cost_usd) as coste_usd,
       sum(input_tokens) as input_tokens,
       sum(output_tokens) as output_tokens,
       sum(pages) as paginas
from public.ai_usage_events
group by 1, 2, 3, 4
order by 1 desc, 2, 3, 4;
```

## Verificación posterior

```sql
select id, public from storage.buckets where id = 'albaranes';

select tablename, rowsecurity
from pg_tables
where schemaname = 'public'
  and tablename in ('ingestions','jobs','extraction_artifacts','review_items',
                    'albaranes','lineas_albaran','audit_events','ai_usage_events');

select indexname
from pg_indexes
where schemaname = 'public' and indexname = 'idx_albaran_duplicado';

select has_function_privilege('anon',
  'public.confirm_albaran_v1(uuid,text,text,text,jsonb,jsonb,uuid)', 'execute');
```

Resultados esperados: bucket `public=false`, todas las filas con
`rowsecurity=true`, ausencia de `idx_albaran_duplicado` y privilegio RPC de
`anon=false`.
