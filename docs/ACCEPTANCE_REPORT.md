# Informe de aceptación con albaranes reales

Fecha: 2026-08-07.

## Alcance

Se revisaron visualmente 11 fotografías privadas y se creó una verdad de
referencia versionada en `tests/fixtures/golden_albaranes.json`. Las imágenes no
se incluyen en Git. El conjunto cubre documentos impresos, manuscritos, mezcla de
ambos, tickets largos, varios tipos de IVA, tarifa/descuento/neto, precio por peso,
cargos adicionales y documentos sin totales rellenados.

## Incidencias encontradas e iteración

La primera ejecución con OCR 4 dejó las tablas como enlaces `tbl-N.html`; el LLM
no recibía cantidades, precios ni IVA. Se corrigió para conservar tablas inline y
se añadió una regresión automática.

La siguiente iteración detectó:

- números confundidos con registros sanitarios; el prompt ahora solo acepta los
  campos etiquetados como albarán/factura y los casos Lucas pasaron de
  `40.20059/MU` a `3.950` y `4.960`;
- peso confundido con unidades; el caso de cordero ahora conserva 2 bultos y usa
  18,60 kg como cantidad facturable, reconciliando 18,60 × 15,10 = 280,86;
- importes calculados con el neto impreso redondeado; la validación admite como
  comprobación secundaria tarifa × descuento, sin modificar el importe observado;
- totales inventados cuando el pie no estaba presente; toda cifra de cabecera que
  no aparece literalmente en el OCR queda marcada como calculada/inferida;
- un falso negativo de manuscrito en una muestra mixta; producción prohíbe la
  autoconfirmación, por lo que esta señal nunca puede publicar datos por sí sola.

Un número de Fragma sigue siendo visualmente ambiguo (`C-04532` frente a lectura
OCR `C-044532`). La contabilidad cuadra, pero la confirmación humana obligatoria
debe comparar ese número con la fotografía. El sistema no pretende ocultar esta
limitación.

## Resultado de seguridad

- 11/11 documentos terminaron en una ruta segura.
- 0 llamadas OCR/LLM fallaron en la pasada completa.
- 0 documentos manuscritos o contablemente inconsistentes pudieron publicarse.
- `AUTO_CONFIRM_CLEAN=true` está prohibido por configuración en producción.
- Los documentos sin importes/totales permiten reutilizar la foto en `/manual`.
- Las discrepancias duras no muestran el botón de confirmación hasta corregirse.

El corpus demuestra ausencia de publicación silenciosa en esta muestra, no una
exactitud universal del modelo. Por eso se mantienen evidencia original,
validaciones deterministas, revisión humana y auditoría versionada.

## Coste medido de las iteraciones

La iteración inicial produjo 73 eventos por 0,13310360 USD:

- 23 OCR de corpus, 0,09200000 USD (incluye un ajuste append-only de 0,044 USD
  por 11 llamadas inicialmente clasificadas con la unidad de precio incorrecta);
- 23 clasificaciones, 0,00860835 USD;
- 26 extracciones/reextracciones, 0,03249525 USD;
- 1 evento de ajuste ya incluido en el total OCR anterior.

Los 73 eventos ya se reconciliaron de forma idempotente con `ai_usage_events`.
También se registraron correctamente llamadas posteriores y el spool quedó sin
eventos pendientes. El valor mostrado continúa siendo una estimación reproducible
según páginas, tokens y tarifas configuradas; la factura del proveedor es la fuente
final para conciliación económica.

## Aceptación técnica remota completada

- Las migraciones `000`–`009` están aplicadas en Supabase y
  `production_contract.sql` termina correctamente contra la instancia remota.
- El bucket `albaranes` es privado y las 12 tablas comprobadas tienen RLS activo.
- El backup completo se restauró y verificó de nuevo tras aplicar `000`–`009` en
  un destino aislado: 9 proveedores, 67 productos, 10 albaranes y 72 líneas, sin
  huérfanos.
- El proceso de backup registra `system.backup.completed` y ya existe un evento
  real verificable para alimentar la señal operativa.
- La imagen final del bot está construida y el healthcheck profundo valida Telegram,
  contrato de Supabase, RPC y privacidad del bucket.
- El dashboard v2 privado está desplegado, conectado a la RPC agregada
  `dashboard_snapshot_v1` y se actualiza cada 30 segundos; la `service_role`
  permanece en backend.
- La suite actual contiene 123 pruebas correctas.

## Pendiente antes del go-live

La aceptación técnica anterior no equivale todavía a autorización de producción.
Falta:

1. añadir el segundo propietario a `TELEGRAM_ALLOWED_USERS`;
2. elegir y preparar el host de la única réplica del bot, incluyendo alertas
   externas que funcionen aunque el proceso o el host estén caídos;
3. configurar los costes fijos reales de hosting, Supabase y otros, y conciliarlos
   con las facturas;
4. ejecutar la aceptación funcional con ambos propietarios: duplicado,
   manuscrito/manual, corrección, confirmación, archivo, métricas, costes y feedback.
