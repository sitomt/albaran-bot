# Decisión de Supabase y panel operativo

Fecha de decisión: 2026-08-07. Revisar capacidades y precios en la página oficial
antes de contratar o cambiar el plan.

## Decisión recomendada

Para la primera salida a producción se recomienda:

1. **Supabase Pro** para el proyecto de producción.
2. Mantener desarrollo/pruebas en otro proyecto u organización, sin mezclar datos
   reales con pruebas.
3. Construir un **dashboard propio y privado** para la operación diaria del
   restaurante: cola, revisiones, errores, albaranes, feedback, exactitud y costes.
4. Reservar el Dashboard de Supabase para tareas técnicas: base de datos, Storage,
   logs, backups, consumo del proveedor y facturas.
5. Mantener el `pg_dump` y la copia de Storage diarios fuera de Supabase. El backup
   administrado no sustituye una copia independiente ni cubre por sí solo los
   originales del bucket.

Pro es la opción proporcionada para dos propietarios y una sola réplica: evita la
pausa por inactividad de Free y añade backups automáticos diarios, más retención de
logs y soporte. Team no aporta ahora suficiente valor para este tamaño; debe
reconsiderarse si se necesitan controles organizativos, cumplimiento o accesos
segregados al propio Dashboard de Supabase.

## Comparación útil para este proyecto

| Opción | Encaje | Backups y logs | Decisión |
|---|---|---|---|
| Free | Desarrollo y experimentación, no producción del restaurante | Sin backups automáticos; el proyecto puede pausarse tras inactividad; retención de logs corta | No usar para producción |
| Pro | Aplicaciones de producción pequeñas | Backup diario con 7 días de retención, proyecto sin pausa, 7 días de logs y endpoint de métricas | Recomendado |
| Team | Equipos que necesitan gobierno y cumplimiento adicionales | Todo Pro, backup diario con 14 días, 28 días de logs, audit logs, roles de acceso más granulares y certificaciones indicadas por Supabase | No necesario inicialmente |
| PITR | Complemento para reducir el RPO y recuperar a un instante concreto | Sustituye el esquema de backups diarios por recuperación continua dentro de la ventana contratada | Posponer hasta que el riesgo justifique el coste |

Precios públicos contrastados al tomar esta decisión: Free parte de 0 USD/mes,
Pro de 25 USD/mes y Team de 599 USD/mes. El plan de pago se factura por
organización; Pro incluye créditos de cómputo que actualmente cubren una instancia
Micro, pero proyectos, cómputo, exceso de cuota y complementos pueden aumentar la
factura. PITR se factura por proyecto y por horas activas; las referencias mensuales
publicadas son aproximadamente 100 USD para 7 días, 200 USD para 14 y 400 USD para
28, y requiere al menos cómputo Small. Estos importes no deben copiarse como coste
real del sistema sin comprobar antes **Billing** y la factura de la organización.

Fuentes oficiales:

- [Precios y comparación de planes](https://supabase.com/pricing)
- [Facturación por organización y consumo variable](https://supabase.com/docs/guides/platform/billing-on-supabase)
- [Backups administrados y PITR](https://supabase.com/docs/guides/platform/backups)
- [Facturación de PITR](https://supabase.com/docs/guides/platform/manage-your-usage/point-in-time-recovery)

## Dashboard propio: alcance objetivo del proyecto

El dashboard propio forma parte de la salida a producción. Su primera versión debe
ser privada, orientada a operación y mostrar:

- salud del bot y última señal externa;
- cola por estado, trabajos atascados y revisiones pendientes;
- errores por etapa y proveedor;
- ingestiones, duplicados, correcciones y feedback;
- OCR/LLM por operación, modelo, usuario, documento, tokens, páginas y reintentos;
- costes fijos configurados y alertas de presupuesto;
- estado y antigüedad del último backup verificado.

Las acciones que cambien datos deben seguir usando las RPC auditadas. No se debe
convertir el dashboard en un acceso directo con `service_role` desde el navegador.

## Qué significa «coste en tiempo real»

El ledger `ai_usage_events` es un registro casi inmediato del **consumo medido por
la aplicación**. Calcula OCR por páginas y LLM por tokens usando la tarifa configurada
en el momento de la llamada. Permite control operativo, atribución y alertas, pero
no es una factura.

`/costes` y el dashboard propio deben etiquetar siempre:

- **IA estimada:** páginas/tokens multiplicados por tarifas configuradas;
- **fijo configurado:** hosting, Supabase y otros importes introducidos por el
  operador;
- **proyección:** extrapolación del ritmo del mes, no compromiso del proveedor;
- **factura real:** importe del panel o factura de Mistral, Supabase y hosting.

La estimación interna no conoce automáticamente créditos, tramos, redondeos,
impuestos, tipo de cambio, descuentos, exceso de almacenamiento o egress, cómputo,
PITR ni otros complementos. La conciliación mensual debe comparar cada proveedor
contra su factura y registrar la diferencia. Hasta integrar fuentes de billing, no
se debe presentar el total interno como «coste real exacto».
