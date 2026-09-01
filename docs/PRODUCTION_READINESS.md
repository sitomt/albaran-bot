# Estado de salida a producción

Fecha de verificación local y remota: 2026-08-07.

## Preparación técnica completada

- Ingesta privada y durable, backpressure e idempotencia.
- Duplicados exactos protegidos ante carreras; similitudes pasan a revisión.
- Cola PostgreSQL con claim atómico, lease, reintentos y recuperación.
- OCR y candidatos versionados, validación contable y confirmación humana.
- Alta manual, corrección previa y archivo auditado.
- Consultas permitidas y parametrizadas, sin SQL generado por el modelo.
- Ledger append-only, presupuesto, costes desglosados y spool reconciliable.
- Dashboard v2 privado desplegado mediante RPC backend-only y con refresco cada
  30 segundos.
- Imagen Docker endurecida, Compose, CI y dependencias bloqueadas con hashes.
- Backup completo de PostgreSQL/Storage, restauración y runbook.

## Evidencia vigente

- 123 pruebas correctas.
- Corpus privado de 11 fotografías en rutas seguras.
- Migraciones `000`–`009` aplicadas en Supabase.
- `production_contract.sql` correcto contra la instancia remota.
- Bucket `albaranes` privado y RLS activo en 12 tablas verificadas.
- Restauración aislada real repetida tras aplicar `000`–`009`: 9 proveedores, 67
  productos, 10 albaranes y 72 líneas, sin relaciones huérfanas.
- El backup registra `system.backup.completed` y ya existe un evento real
  verificable para la señal operativa.
- Los 73 eventos históricos de IA están reconciliados; llamadas posteriores
  registradas y spool sin pendientes.
- Imagen final construida y healthcheck profundo correcto.
- Dashboard v2 privado desplegado, conectado a `dashboard_snapshot_v1` y con
  refresco cada 30 segundos, sin exponer `service_role` en el navegador.

## Bloqueos externos actuales

- Solo hay un propietario en `TELEGRAM_ALLOWED_USERS`; falta el segundo.
- Falta elegir y preparar el host definitivo de la única réplica.
- Faltan alertas externas que detecten caída del bot/host y ausencia de backup.
- Los costes fijos reales de hosting, Supabase y otros aún deben configurarse y
  conciliarse con facturas.
- Falta la aceptación funcional y firma de ambos propietarios.

La instancia de datos y el artefacto están técnicamente preparados, pero estos
puntos impiden declarar el go-live completo.

## Go/no-go

Antes de autorizar producción:

1. añadir y verificar ambos IDs de Telegram;
2. desplegar una única réplica en el host elegido;
3. comprobar desde fuera health, alertas y antigüedad del backup;
4. configurar costes fijos y umbrales;
5. probar con ambos propietarios foto limpia, duplicado, manuscrito/manual,
   corrección, rechazo, confirmación, archivo, consulta, costes y feedback;
6. registrar la aceptación o los riesgos residuales pendientes.
