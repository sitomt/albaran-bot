# Operación de producción

Este documento cubre despliegue, comprobaciones, backup, restauración, rollback e
incidentes del bot. Supabase permanece como servicio administrado y el bot debe
ejecutarse en **una única réplica**: Telegram long polling no admite que varias
instancias consuman el mismo token de forma coordinada.

La decisión para el primer go-live es **Supabase Pro más un dashboard operativo
propio y privado**. Alcance, alternativas, PITR y límites de la estimación de costes
están documentados en [SUPABASE_DECISION.md](SUPABASE_DECISION.md).

## Requisitos de producción

- Host Linux con Docker Engine y Docker Compose v2.
- Proyecto Supabase separado del entorno de desarrollo.
- Bucket `albaranes` privado y políticas RLS aplicadas mediante migraciones.
- Bot de Telegram dedicado al entorno.
- Dos IDs de usuario configurados en `TELEGRAM_ALLOWED_USERS`.
- Gestor de secretos del proveedor o `.env` con permisos `0600` fuera del control
  de versiones.
- PostgreSQL client igual o más reciente que la versión del servidor para backups.
- Destino de backup cifrado, externo al host y con control de acceso.

El bot usa `service_role` porque es un backend privado y las migraciones revocan
todo acceso `anon`/`authenticated`. Inyectarla solo en el contenedor y en el
proceso de backup; nunca exponerla a usuarios, logs o repositorios.

## Primer despliegue

1. Copiar `.env.example` a `.env`, completar secretos y ejecutar `chmod 600 .env`.
2. Confirmar que `TELEGRAM_ALLOWED_USERS` contiene exactamente los propietarios.
3. Aplicar las migraciones SQL aprobadas al proyecto de producción.
4. Verificar que el bucket es privado y que el usuario de aplicación solo puede
   acceder a los objetos y filas necesarios.
5. Construir y ejecutar:

   ```sh
   IMAGE_TAG=2026-08-06 docker compose build --pull
   IMAGE_TAG=2026-08-06 docker compose up -d
   docker compose ps
   docker compose logs --tail=100 bot
   docker compose exec bot python /app/ops/healthcheck.py --deep
   ```

6. Enviar una imagen de prueba no contable, confirmar el flujo completo y borrarla
   siguiendo el procedimiento funcional aprobado.
7. Comprobar en Supabase el job, la auditoría, la imagen privada y las filas
   relacionadas. Nunca validar producción únicamente por el mensaje de Telegram.

El entrypoint rechaza el arranque si falta un secreto o la whitelist está vacía;
no existe un bypass de esa comprobación.

## Despliegue de una versión nueva

Antes del cambio:

1. Exigir CI verde y revisión del diff.
2. Crear un backup completo y ejecutar `scripts/verify_backup.sh`.
3. Registrar versión actual, versión nueva y persona responsable.
4. Evitar desplegar mientras haya jobs `procesando`; si no es posible, comprobar
   que sus originales ya están persistidos y que el reinicio puede recuperarlos.

Desplegar una sola réplica:

```sh
IMAGE_TAG=<revision-inmutable> docker compose build --pull
IMAGE_TAG=<revision-inmutable> docker compose up -d --no-deps bot
docker compose ps
docker compose logs --tail=200 bot
docker compose exec bot python /app/ops/healthcheck.py --deep
```

Observar durante al menos un ciclo real: recepción, extracción, revisión y
confirmación. Un contenedor `healthy` solo demuestra vida del proceso; la prueba
`--deep` valida credenciales/conectividad, no la corrección del pipeline.

## Rollback

El rollback de aplicación no debe revertir automáticamente la base de datos.

1. Detener nuevas ingestas avisando a los dos usuarios.
2. Registrar jobs pendientes/procesando y revisar logs.
3. Cambiar `IMAGE_TAG` a la última imagen aprobada y ejecutar:

   ```sh
   IMAGE_TAG=<revision-anterior> docker compose up -d --no-deps bot
   docker compose exec bot python /app/ops/healthcheck.py --deep
   ```

4. Reconciliar todos los jobs que estaban en curso.
5. Si una migración no es compatible hacia atrás, usar su migración correctiva.
   Restaurar toda la base es el último recurso y requiere autorización explícita.

## Backups

Objetivo inicial recomendado para este volumen:

- RPO: 24 horas; backup diario de base y bucket privado.
- RTO: 4 horas.
- Retención: 7 diarios, 5 semanales y 12 mensuales.
- Una copia fuera de Supabase y del host del bot, cifrada en reposo.
- Simulacro mensual de restauración en una base aislada.

Crear una copia:

```sh
export SUPABASE_DB_URL='postgresql://...?sslmode=require'
export SUPABASE_URL='https://PROJECT_REF.supabase.co'
export SUPABASE_SERVICE_ROLE_KEY='...'
scripts/backup_database.sh --output-dir /ruta/cifrada/albaran-backups
```

El script falla si no puede copiar Storage, salvo que se indique expresamente
`--skip-storage`. Una copia marcada `INCOMPLETE` no es utilizable. La rotación no
se automatiza en el script para impedir borrados accidentales; debe configurarse
en el almacén de backups mediante una política de ciclo de vida versionada.

Para un host con systemd hay plantillas en `ops/systemd`. Crear el usuario
`albaran-backup`, instalar el repositorio en `/opt/albaran-bot`, guardar las tres
variables del backup en `/etc/albaran-bot/backup.env` con permisos `0600`, copiar
las unidades a `/etc/systemd/system` y habilitar:

```sh
systemctl daemon-reload
systemctl enable --now albaran-backup.timer
systemctl list-timers albaran-backup.timer
```

El temporizador no implementa rotación destructiva: la retención debe vivir en un
destino externo versionado. Configurar alerta si la unidad falla o si no aparece
un backup `COMPLETE` en 26 horas.

Si temporalmente no está disponible la contraseña PostgreSQL, se puede preservar
el contenido mediante PostgREST y Storage:

```sh
scripts/backup_emergency_api.sh /ruta/restringida
scripts/verify_emergency_backup.sh /ruta/restringida/emergency-<timestamp>
```

Esta exportación es una red de emergencia, no un backup de base de datos: no
incluye funciones, triggers, índices, roles ni privilegios. No autoriza una
migración sin el `pg_dump` y el simulacro exigidos para el go-live.

Si se pierde la base antes de obtener el `pg_dump`, sus filas pueden recuperarse
sobre un baseline legado **vacío**:

```sh
psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -f sql/migrations/000_legacy_baseline.sql
scripts/restore_emergency_api.sh /ruta/restringida/emergency-<timestamp>
```

El restaurador verifica primero todos los hashes, bloquea las tablas, rechaza un
destino que ya contenga filas, inserta en orden de dependencias y comprueba todos
los conteos antes del commit. El 7 de agosto de 2026 se ensayó con la exportación
real: 9 proveedores, 67 productos, 10 albaranes y 72 líneas; a continuación
pasaron todas las migraciones y el contrato PostgreSQL de producción.

## Restauración y simulacro

Restaurar primero en un proyecto/base temporal sin usuarios reales:

```sh
scripts/restore_database.sh /ruta/backup/20260806T020000Z \
  --target-url 'postgresql://...base-aislada...?sslmode=require'

# Revisar el plan; solo después ejecutar:
scripts/restore_database.sh /ruta/backup/20260806T020000Z \
  --target-url 'postgresql://...base-aislada...?sslmode=require' --execute

scripts/verify_restored_database.sh \
  'postgresql://...base-aislada...?sslmode=require'
```

Después comparar conteos con producción en el instante del backup, abrir una
muestra de originales y ejecutar consultas funcionales conocidas. Para Storage,
crear primero un bucket **privado y vacío** en el proyecto aislado y ejecutar:

```sh
export SUPABASE_URL='https://PROYECTO-AISLADO.supabase.co'
export SUPABASE_SERVICE_ROLE_KEY='...clave del proyecto aislado...'
python3 ops/restore_storage.py \
  --source /ruta/backup/20260806T020000Z/storage \
  --bucket albaranes

# El primer comando solo valida. Tras comprobar proyecto y bucket:
python3 ops/restore_storage.py \
  --source /ruta/backup/20260806T020000Z/storage \
  --bucket albaranes --execute
```

El restaurador se niega a escribir si el bucket contiene algún objeto y nunca
sobrescribe. Al final compara todos los paths remotos con el manifiesto local.

Una restauración sobre producción requiere `--allow-production`. `--clean` requiere
además `--allow-destructive-clean`; ambos flags son barreras, no sustituyen una
aprobación y una ventana de mantenimiento.

## Monitorización mínima

Alertar cuando ocurra cualquiera de estos casos:

- contenedor reiniciándose o `unhealthy` durante más de 2 minutos;
- job `procesando` durante más de 10 minutos;
- cualquier job en `error`;
- crecimiento sostenido de pendientes;
- discrepancia contable o documento que requiere revisión sin resolver;
- gasto diario de OCR/LLM por encima del presupuesto acordado;
- ausencia del backup diario o fallo de su checksum;
- más de un intento de acceso de un usuario no autorizado.

Revisión diaria: errores, pendientes, revisiones y coste. Revisión semanal:
duplicados probables, exactitud corregida por proveedor, almacenamiento y tiempos.
Revisión mensual: restauración real aislada y rotación de credenciales si procede.

`/costes` consulta directamente el ledger en cada apertura o pulsación de
**Actualizar**. El bloque de IA es consumo medido; hosting, Supabase y otros son
importes contractuales configurados. Si estos últimos aparecen a cero, el total del
proyecto está incompleto aunque el total de IA sea correcto.

El volumen `runtime-data` conserva consumos facturables que no pudieron llegar a
Supabase. Se reintentan de forma idempotente por UUID y `/costes` muestra una
alerta mientras quede alguno pendiente. Este volumen debe incluirse en la copia
del host y nunca debe eliminarse durante un despliegue normal.

## Incidentes

### El bot no responde

1. `docker compose ps` y `docker compose logs --tail=300 bot`.
2. Ejecutar `healthcheck.py --deep`.
3. Verificar estado de Telegram, Supabase y Mistral.
4. No reiniciar repetidamente si hay escrituras en curso; anotar IDs de jobs.
5. Reiniciar una vez y reconciliar jobs recuperados, pendientes y parciales.

### Datos incorrectos o duplicados

1. Suspender nuevas ingestas y preservar documento, auditoría y logs.
2. No borrar filas para “arreglarlo”. Marcar/revertir mediante el flujo auditado.
3. Identificar alcance por proveedor, versión del extractor y ventana temporal.
4. Corregir, ejecutar regresiones y reprocesar solo mediante una operación
   idempotente aprobada.

### Credencial expuesta

1. Revocar/rotar inmediatamente en el proveedor correspondiente.
2. Actualizar el gestor de secretos y recrear el contenedor.
3. Revisar auditoría desde el primer instante posible de exposición.
4. Si fue la service role, asumir acceso total a datos y objetos y escalar el
   incidente; nunca escribir esa clave en logs o tickets.

### Coste inesperado o carga masiva

1. Pausar ingesta sin descartar originales ya persistidos.
2. Identificar usuario, número de jobs y coste acumulado.
3. Mantener los trabajos en estado recuperable; no vaciar la cola mediante borrado.
4. Reanudar en lotes pequeños tras fijar límites y presupuesto.

## Checklist de salida

- [ ] CI verde y artefacto identificado por revisión inmutable.
- [ ] Variables y whitelist validadas; ningún secreto en Git o imagen.
- [ ] Bucket privado, RLS y privilegios mínimos comprobados.
- [ ] Backup completo `COMPLETE` y restauración aislada verificada.
- [ ] Solo una réplica y política de reinicio activa.
- [ ] Health local y `--deep` correctos.
- [ ] Alertas, presupuesto y responsable de guardia definidos.
- [ ] Flujo de corrección, duplicado y revisión probado por ambos usuarios.
- [ ] Rollback de aplicación ensayado.
- [ ] Riesgos residuales aceptados explícitamente.
