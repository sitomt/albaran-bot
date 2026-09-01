# Resultados de verificación

Fecha de corte: 2026-08-07.

## Resumen

| Área | Resultado |
|---|---|
| Suite Python | 123 pruebas correctas |
| Migraciones | `000`–`009` aplicadas y reejecutables |
| Contrato PostgreSQL remoto | `production_contract_ok` |
| Privacidad de Storage | bucket `albaranes` privado |
| Seguridad de tablas | RLS activo en 12 tablas verificadas |
| Restauración aislada | 9 proveedores, 67 productos, 10 albaranes, 72 líneas |
| Ledger de IA | 73 eventos históricos reconciliados; posteriores registrados; spool vacío |
| Señal de backup | evento real `system.backup.completed` registrado |
| Contenedor | imagen final construida y endurecida |
| Health profundo | Telegram, Supabase, RPC y bucket correctos |
| Dashboard | v2 privado desplegado, conectado al snapshot backend y refresco cada 30 s |

## Pruebas automatizadas

La suite cubre:

- invariantes contables, precio neto, descuento, IVA y totales;
- deduplicación exacta, lógica y perceptual;
- recepción durable, límites de carga y recuperación de cola;
- coste facturable, fallos con uso medible, spool e idempotencia;
- entrada manual, revisión, corrección y transiciones humanas atómicas;
- seguridad de consultas y ausencia de SQL dinámico;
- migraciones, RLS, privilegios backend-only y contratos RPC;
- dashboard agregado sin OCR crudo, rutas de Storage ni secretos;
- restauración de emergencia y recorridos completos de usuario.

Resultado vigente: **123 correctas**.

## Base de datos y recuperación

Las migraciones `000`–`009` están aplicadas en el proyecto remoto. El contrato
transaccional comprueba tablas, RLS, privilegios, bucket privado, publicación
atómica, compare-and-swap de revisiones, reintentos, ledger append-only y snapshot
del dashboard.

La restauración real final se repitió en un destino aislado después de aplicar
`000`–`009` y conservó:

- 9 proveedores;
- 67 productos de catálogo;
- 10 albaranes;
- 72 líneas.

Se verificaron los conteos, relaciones sin huérfanos, migraciones posteriores y
RLS en 12 tablas. Esta prueba demuestra recuperabilidad del conjunto ensayado; no
sustituye el backup diario externo ni los simulacros periódicos.

## Corpus real OCR/LLM

La aceptación usa 11 fotografías privadas con impresos, manuscritos, documentos
mixtos, varios IVA, tarifa/descuento/neto, precio por peso y pies incompletos. Las
imágenes no se versionan. La pasada completa terminó en rutas seguras y las
ambigüedades siguen requiriendo confirmación humana.

El detalle de incidencias y coste de esa iteración está en
[docs/ACCEPTANCE_REPORT.md](docs/ACCEPTANCE_REPORT.md).

## Costes

Los 73 eventos históricos del corpus se reconciliaron con el ledger remoto. Se
comprobaron llamadas posteriores sin dejar consumos en el spool local.

Los valores del ledger son **costes estimados**: páginas y tokens multiplicados por
tarifas configuradas en el momento de la llamada. Créditos, impuestos, tramos,
egress, almacenamiento y otros ajustes deben conciliarse con las facturas reales.

## Resultado operativo

La infraestructura técnica comprobada es apta para continuar hacia el go-live,
pero el go-live **no está aprobado todavía**. Quedan pendientes:

- segundo ID autorizado de Telegram;
- host definitivo y alertas externas;
- costes fijos reales;
- aceptación funcional por ambos propietarios.
