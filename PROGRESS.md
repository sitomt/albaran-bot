# Albarán Bot — estado actual

Fecha de corte: 2026-08-07.

Este documento muestra el estado vigente. El historial del prototipo se retiró de
aquí porque describía RPC, seguridad, colas y flujos que ya no existen.

## Completado

- Ingesta durable: el original privado se persiste antes del acuse de recibo.
- Dedupe exacta y perceptual, claves idempotentes y publicación transaccional.
- Cola PostgreSQL con claim atómico, leases, reintentos y recuperación.
- OCR, clasificación y extracción versionados; manuscritos y ambigüedades pasan a
  revisión humana.
- Controles deterministas de cantidad, precio neto, descuento, importe, base, IVA
  y total.
- Revisión compartida, corrección de candidatos, alta manual y archivo auditado.
- Consultas cerradas y parametrizadas; no existe SQL generado por el modelo.
- Ledger append-only de IA, spool durable, presupuesto y desglose de costes.
- Dashboard v2 privado desplegado y conectado a `dashboard_snapshot_v1`, con
  refresco cada 30 segundos; la `service_role` solo se utiliza en backend.
- Docker endurecido, Compose de una réplica, locks con hashes y CI.
- Backup y restauración de PostgreSQL y Storage con verificación.

## Estado remoto verificado

- Migraciones `000`–`009` aplicadas.
- `production_contract.sql`: correcto contra Supabase remoto.
- Bucket `albaranes`: privado.
- RLS: activo en las 12 tablas verificadas.
- Restauración real aislada repetida tras aplicar `000`–`009`: 9 proveedores, 67
  productos, 10 albaranes y 72 líneas; conteos y relaciones correctos.
- El backup registra `system.backup.completed` y ya existe un evento real
  verificable para la señal operativa.
- Costes: los 73 eventos históricos se reconciliaron; las llamadas posteriores se
  registran y no queda spool pendiente.
- Imagen final construida; healthcheck profundo correcto para Telegram, Supabase,
  RPC y bucket.
- Suite: 123 pruebas correctas.

## Decisiones vigentes

- Supabase Pro es la recomendación para producción; PITR se evaluará según RPO y
  presupuesto.
- El dashboard es la superficie privada de operación. El Dashboard de Supabase se
  reserva para administración técnica, backups, consumo y facturación.
- Los importes OCR/LLM son estimaciones basadas en uso medido y tarifas
  configuradas. La factura real de cada proveedor prevalece.
- Una única réplica debe consumir el token de Telegram.
- `AUTO_CONFIRM_CLEAN=false` es obligatorio: ningún documento se publica solo por
  parecer correcto al modelo.

## Bloqueos restantes para go-live

- [ ] Añadir el segundo propietario a `TELEGRAM_ALLOWED_USERS`.
- [ ] Elegir y preparar el host definitivo del bot.
- [ ] Activar alertas externas de caída del proceso/host y ausencia de backup.
- [ ] Configurar los costes fijos reales: hosting, Supabase y otros.
- [ ] Ejecutar y firmar la prueba funcional con ambos propietarios.

Hasta completar esos puntos el sistema está técnicamente preparado, pero no debe
declararse en producción ni plenamente aceptado por los usuarios.

## Evidencia

- [Informe de aceptación](docs/ACCEPTANCE_REPORT.md)
- [Preparación de producción](docs/PRODUCTION_READINESS.md)
- [Operación y recuperación](docs/OPERATIONS.md)
- [Decisión de Supabase y costes](docs/SUPABASE_DECISION.md)
- [Resultados de pruebas](TEST_RESULTS.md)
