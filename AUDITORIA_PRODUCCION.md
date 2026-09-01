# Auditoría de producción — Albarán Bot

> **Documento histórico de hallazgos iniciales.** El veredicto y las referencias
> de líneas siguientes describen el estado anterior a la reconstrucción del 6–7
> de agosto de 2026; no representan el código actual. Los P0 se abordaron mediante
> ingesta durable, candidatos separados, RPC transaccional, RLS, bucket privado,
> validación contable, revisión durable, límites, ledger de costes, Docker, CI y
> backups. El estado vigente y los bloqueos remotos están en
> `docs/PRODUCTION_READINESS.md`.

Fecha: 6 de agosto de 2026

## Veredicto ejecutivo

El proyecto contiene un prototipo funcional y varias defensas útiles, pero **no está listo para producción**. Los 32 tests actuales pasan, y existen pruebas reales de OCR sobre 11 imágenes, pero el sistema aún puede:

- perder fotografías que ya confirmó como recibidas;
- guardar un albarán a medias y rechazar después el reintento como duplicado;
- insertar datos dudosos antes de que el usuario los confirme;
- fabricar coherencia matemática recalculando valores que el OCR pudo haber leído mal;
- perder o cruzar revisiones durante una ráfaga de imágenes;
- exponer documentos mediante un bucket público y una base sin controles suficientes;
- operar sin backups restaurados, despliegue reproducible ni medición real de costes.

La decisión recomendada es **no desplegar el estado actual como sistema contable definitivo**. Puede seguir usándose en desarrollo con datos de prueba.

## Qué sí está bien encaminado

- Código separado por responsabilidades básicas: bot, cola, procesador, base de datos, normalización y entrada manual.
- Hash SHA-256 de imagen para reenvíos de bytes idénticos.
- Número de albarán normalizado e índices únicos como última defensa.
- Validación de algunas relaciones entre precio, cantidad e importe.
- Campo de confianza y marca `requiere_revision`.
- Flujo manual y reutilización de la foto cuando falla el OCR.
- Registro básico de jobs, auditoría y correcciones.
- 32 tests unitarios correctos y compilación estática correcta.

Estas piezas son aprovechables. No hace falta rehacer absolutamente todo, pero sí cambiar la unidad de trabajo y el criterio de confianza.

## Arquitectura actual reconstruida

```text
Telegram
  -> descarga de la foto completa
  -> job en Supabase sin foto persistida
  -> cola asyncio en RAM con los bytes
  -> OCR Mistral
  -> extracción LLM desde el texto OCR
  -> reglas y cálculos Python
  -> creación/búsqueda de proveedor
  -> detección de duplicado
  -> INSERT de cabecera
  -> subida de imagen
  -> normalización de catálogo y actualización de precios
  -> INSERT de líneas
  -> job/auditoría
  -> posible confirmación guardada solo en RAM
```

La debilidad estructural es que **recepción, extracción, revisión y dato contable definitivo están mezclados**. No existe un commit atómico que garantice “todo o nada”.

## Hallazgos P0 — bloquean producción

### P0.1. Pérdida de fotos y trabajos en reinicios

`handle_photo` crea el job sin URL y encola los bytes únicamente en RAM (`src/bot.py:383-390`, `src/queue_manager.py:23-38`). La imagen se sube después de insertar el albarán (`src/albaran_processor.py:889-895`). Al reiniciar, solo se recuperan jobs que ya tengan `imagen_url`; los demás pasan a error (`src/queue_manager.py:319-342`).

Escenario reproducible por inspección:

1. El bot responde “Recibido, procesando”.
2. La foto queda en la memoria del proceso.
3. Se reinicia o despliega el bot.
4. La base conserva un job, pero no la fotografía.
5. El documento no puede reprocesarse.

Solución: persistir primero el original en almacenamiento privado, confirmar recepción después y encolar solo una referencia durable.

### P0.2. Escrituras parciales y reintentos bloqueados como duplicados

Se inserta la cabecera antes que las líneas (`src/albaran_processor.py:843-986`). Entre ambos pasos también se sube la imagen y se modifica el catálogo. No existe transacción ni rollback. El flujo manual repite el patrón (`src/manual_albaran.py:499-531`).

Si falla la inserción de líneas, queda una cabecera válida para los índices de deduplicación. Al reenviar el documento, el sistema responde “duplicado” y no completa las líneas.

Solución: commit transaccional e idempotente de cabecera, líneas, estado y auditoría. Los datos deben pasar por estados `received -> extracted -> review -> confirmed`.

### P0.3. La revisión ocurre después de contaminar los datos

Las líneas dudosas y sus precios ya están guardados antes de preguntar al usuario (`src/albaran_processor.py:951-986`). Responder `OK` solo envía “Albarán confirmado”; no cambia el estado ni limpia correctamente las revisiones (`src/bot.py:239-244`). Corregir un único campo marca la línea completa como revisada aunque otros campos sigan dudosos (`src/bot.py:264-276`).

Solución: almacenar candidatos separados de los datos confirmados. Una aprobación debe volver a ejecutar todas las reglas contables, registrar actor y fecha, y solo entonces publicar el dato para consultas y métricas.

### P0.4. El sistema valida cálculos propios, no fidelidad visual

`_resolver_precio_neto` puede sustituir el importe observado por uno calculado (`src/albaran_processor.py:267-308`). Si el OCR desplaza las columnas TARIFA, DESCUENTO, NETO o IMPORTE, el código puede construir una línea internamente perfecta pero distinta del papel.

Debe conservarse por separado:

- valor observado;
- columna y región de origen;
- valor calculado;
- regla aplicada;
- discrepancia;
- valor finalmente aceptado y quién lo aceptó.

Nunca se debe sobrescribir silenciosamente el valor observado.

### P0.5. Reconciliación de IVA, base y total insuficiente

La suma de líneas se acepta si está a menos del 5 % de cualquiera de varios objetivos, incluido el total con IVA (`src/albaran_processor.py:338-367`). En un albarán de 1.000 €, eso tolera 50 €. Después puede sustituirse la base imponible por esa suma (`src/albaran_processor.py:771-784`).

Faltan invariantes obligatorios:

```text
sum(importes_netos) ~= base_imponible
sum(detalle_iva.base) ~= base_imponible
sum(detalle_iva.cuota) ~= total_iva
base_de_tramo * tipo / 100 ~= cuota_de_tramo
base_imponible + total_iva ~= total
```

La tolerancia debe cubrir céntimos y redondeos documentados, no un porcentaje amplio.

### P0.6. Manuscritos tratados como fiables sin evidencia suficiente

El extractor estructurado recibe únicamente el texto OCR, no la imagen (`src/albaran_processor.py:687-699`). No puede evaluar de manera fiable tachaduras, columnas o calidad visual. Si la confianza no se puede parsear, se convierte en 100 (`src/albaran_processor.py:93-100`).

La inspección de los dos manuscritos del repositorio mostró:

- `albaran-problematico1.JPG`: no contiene importes ni total escritos. El resultado guardado calculó cuatro importes y asignó confianza 100 a todas las líneas.
- `albaran-problematico2.JPG`: una línea se extrajo como `5 Henda CIP` con confianza 70. La revisión se activa solo por debajo de 70. Además, las cifras registradas `421,00 + 47,10 = 463,10` no cumplen la suma aritmética.

Solución: manuscrito o baja calidad implica revisión forzada; confianza ausente equivale a 0. La extracción debe usar imagen y regiones visuales, y mostrar al usuario el recorte asociado a cada campo dudoso.

### P0.7. Seguridad fail-open y documentos públicos

- Si `TELEGRAM_ALLOWED_USERS` está vacío, el bot permite a cualquier usuario (`src/config.py:42-46`, `src/bot.py:37-42`).
- La whitelist no es obligatoria al arrancar.
- Se usa la clave `anon`, no hay RLS/policies versionadas y el README pide un bucket público.
- `execute_select(query text)` ejecuta SQL dinámico con `SECURITY DEFINER` (`sql/schema.sql:146-159`). Comprobar que empieza por `SELECT` no lo convierte en seguro.

Solución: autorización obligatoria y fail-closed, bucket privado, privilegios mínimos, RLS, revocación de `PUBLIC/anon` y sustitución de la RPC genérica por consultas permitidas y parametrizadas.

### P0.8. No hay backup ni restauración probada

No existe en el repositorio política de backup, copia de objetos, retención, RPO/RTO, runbook o simulacro de restauración. Un backup no se considera válido hasta restaurarlo en un entorno aislado y verificar conteos, hashes y relaciones.

## Hallazgos P1 — fiabilidad operativa

### Revisiones concurrentes

Solo existe una confirmación pendiente por `chat_id` y vive en RAM (`src/queue_manager.py:28,236-241`). Tres workers pueden procesar varios albaranes del mismo propietario y sobrescribir revisiones anteriores.

### Ingesta masiva sin control

La cola no tiene límite y almacena imágenes completas. No hay límite por usuario, tamaño, lote, coste, pausa ni cancelación. Una ráfaga puede consumir memoria y gasto de API sin control.

### Deduplicación con falsos positivos

Python rechaza mismo proveedor, fecha y total con tolerancia de ±0,50 € (`src/supabase_client.py:268-284`). La base prohíbe exactamente proveedor, fecha y total (`sql/schema.sql:132-136`). Dos entregas legítimas del mismo día y mismo importe no pueden coexistir.

Fecha+total debe crear un **candidato a duplicado**, nunca un rechazo automático. Solo identidades fuertes verificadas deben ser únicas.

### Deduplicación con falsos negativos

SHA-256 solo detecta bytes idénticos. Un recorte, rotación, compresión o nueva foto lo evade. Hace falta una firma compuesta: hash exacto, hash perceptual, proveedor/NIF, número, fecha, total, firma de líneas y similitud OCR.

### Fechas falseadas silenciosamente

Una fecha ausente o no reconocida se sustituye por la fecha actual (`src/albaran_processor.py:143-154`). Esto altera históricos y deduplicación. Debe quedar nula y requerir revisión.

### JSON truncado aceptado parcialmente

El recuperador conserva líneas completas y descarta la última cortada (`src/albaran_processor.py:611-680`). Sin total impreso, la reconciliación acepta la suma. Un documento largo puede perder líneas y parecer correcto.

Toda recuperación de truncación debe quedar `extraction_complete=false` y reprocesarse por páginas o bloques.

### Métricas mezcladas entre propietarios

Las estadísticas de cola son globales, no por usuario o lote (`src/queue_manager.py:24-27`). El resumen combinado se envía al chat del último trabajo terminado.

### Media de precios incorrecta

`precio_medio_historico = (media_anterior + precio_nuevo) / 2` no es una media histórica (`src/supabase_client.py:220-244`). La fecha usada es la de procesamiento, no la del albarán. Los agregados deben derivarse de líneas confirmadas.

### Entrada manual incompleta

Solo solicita nombre, cantidad y precio neto; no modela descuento, importe observado, base ni IVA (`src/manual_albaran.py:304-335`, `src/manual_albaran.py:499-509`). Esto no resuelve el caso principal de columnas ambiguas.

## Hallazgos P2 — madurez y mantenibilidad

- No hay migraciones versionadas: `schema.sql` con `CREATE TABLE IF NOT EXISTS` no actualiza instalaciones antiguas.
- Dependencias abiertas con `>=` y sin lock.
- Alias mutable `mistral-ocr-latest`.
- Sin despliegue reproducible, CI/CD, healthcheck, readiness, shutdown ordenado o rollback.
- Logs de texto sin métricas, alertas o correlation IDs consistentes.
- Errores internos se muestran a usuarios en varios handlers.
- Auditoría sin OCR bruto, JSON bruto, versión de prompt, decisiones ni correcciones de cabecera.
- `tokens_totales` permanece siempre en cero; el control de costes actual no funciona.
- Correcciones implementadas por caminos distintos y con auditoría desigual.
- Código de deduplicación y formateo repetido.
- `_stats['errores']` se incrementa, pero no aparece en el resumen.

## Pruebas y simulaciones realizadas

### Resultado de la suite

- Entorno temporal aislado creado fuera del repositorio.
- `pytest`: **32 passed**.
- `compileall`: correcto.
- No se modificó ni consultó la base real.

### Limitación de la suite actual

Las pruebas usan mocks casi por completo. No cubren:

- reinicio con trabajos pendientes;
- fallo después de insertar cabecera;
- varias fotos dudosas simultáneas del mismo usuario;
- dos usuarios y métricas separadas;
- RLS, permisos o RPC;
- backup y restauración;
- migraciones desde una versión anterior;
- carga sostenida y límites de memoria/coste;
- exactitud end-to-end contra verdad visual anotada.

### Validez del dataset OCR existente

Hay 11 imágenes, incluidas dos manuscritas. Los informes declaran fidelidad de 9/9 y 68/68 líneas en los no manuscritos, pero parte de la “verdad” se reconstruyó desde el propio OCR. Esto no es una evaluación independiente.

Debe crearse un dataset dorado anonimizado con transcripción humana doble y campos por columna: proveedor, número, fecha, cantidad, unidad, tarifa, descuento, neto, importe, bases, tipos de IVA, cuotas y total.

## Arquitectura objetivo mínima

```text
Telegram
  -> validar usuario, tamaño y tipo
  -> persistir original privado
  -> crear ingestion durable con idempotency key
  -> ACK al usuario
  -> worker reclama job con lease atómico
  -> OCR + bloques/coordenadas/confianzas
  -> extracción candidata versionada
  -> validaciones contables deterministas
  -> dedup: exacto / probable / nuevo
  -> revisión durable por albarán y campo
  -> commit SQL transaccional
  -> catálogo y métricas derivados de datos confirmados
  -> auditoría, coste y feedback

## Estado de implementación posterior a la auditoría

La arquitectura objetivo anterior ya está implementada en el repositorio. Los
hallazgos de este documento describen el punto de partida y se conservan como
trazabilidad; no deben interpretarse como estado actual sin contrastarlos con las
migraciones y la suite.

Se han cerrado en código: cola RAM, confirmaciones en memoria, ACK previo al
almacenamiento, inserción no transaccional, SQL generado, bucket público, acceso
`anon`, fecha inventada, JSON truncado tratado como completo, deduplicación débil
automática, falta de control contable, dependencias sin lock, ausencia de CI,
contenedor, backup/restauración, coste, métricas, feedback y reintento.

La salida remota sigue condicionada a tareas externas que el repositorio no puede
realizar sin credenciales y destino: backup de la instancia elegida, aplicación de
migraciones, restauración aislada, despliegue del contenedor y prueba de aceptación
con documentos reales anotados por una persona distinta del OCR.
```

Estados propuestos:

```text
received -> processing -> extracted
                       -> needs_review -> confirmed
                       -> probable_duplicate -> confirmed/rejected
                       -> failed_retryable / failed_final
```

Los datos en `received`, `extracted` o `needs_review` no deben aparecer en consultas contables.

## Modelo de revisión recomendado

Cada documento debe tener una ficha de revisión con:

- foto original y recorte del campo;
- valor observado;
- valor calculado;
- confianza real del OCR;
- motivo de la alerta;
- efecto sobre base, IVA y total;
- botones `Confirmar`, `Corregir`, `No se lee`, `Duplicado`;
- actor, fecha y versión confirmada.

Reglas iniciales:

- manuscrito: revisión obligatoria;
- confianza desconocida: revisión obligatoria;
- fecha o proveedor dudosos: revisión obligatoria;
- cualquier invariante contable roto: revisión obligatoria;
- candidato débil a duplicado: decisión humana;
- documento completamente reconciliado y proveedor/formato conocido: confirmación rápida de resumen.

## Entrada manual rápida propuesta

Flujo de seis pasos, con autocompletado:

1. Proveedor.
2. Número y fecha.
3. Líneas: producto, cantidad/unidad, tarifa, descuento, neto e importe; solo se muestran las columnas necesarias.
4. Base y tramos de IVA sugeridos automáticamente.
5. Total impreso obligatorio.
6. Pantalla de diferencias y confirmación.

Debe permitir editar cualquier línea, no solo borrar la última. Productos y proveedores habituales deben ofrecerse como botones o búsqueda incremental.

## Estrategia de deduplicación

Clasificación, no booleano único:

| Resultado | Evidencia | Acción |
|---|---|---|
| Duplicado exacto | mismo `telegram_file_unique_id`, hash exacto o identidad fuerte confirmada | rechazar automáticamente |
| Duplicado probable | fecha/total, hash perceptual o firma de líneas parecida | mostrar ambos y pedir decisión |
| Nuevo | sin coincidencia suficiente | continuar |

Eliminar el `UNIQUE(proveedor, fecha, total)`. Mantener claves fuertes y una `idempotency_key` por intento.

## Coste y límites

El código actual no mide consumo: `tokens_totales` nunca se incrementa y no incluye OCR, normalización ni consultas.

Según la documentación oficial consultada el 6 de agosto de 2026, OCR 4 figura a 4 USD por 1.000 páginas, o 5 USD por 1.000 páginas anotadas; Mistral Small 4 figura a 0,15 USD por millón de tokens de entrada y 0,60 USD por millón de salida. Estos precios deben ser configuración versionada, no constantes asumidas para siempre.

Por cada llamada se debe guardar:

```text
provider, model, operation, pages,
input_tokens, output_tokens, retries,
unit_price_version, estimated_cost,
job_id, albaran_id, user_id, timestamp
```

Controles:

- máximo de imágenes simultáneas por usuario;
- máximo de páginas y tamaño por documento;
- presupuesto diario y mensual;
- alerta al 70/90/100 %;
- circuit breaker al alcanzar el límite;
- caché por hash para no pagar dos veces;
- reintento solo de 429, timeout y 5xx, con `Retry-After` y jitter.

Sin conocer el volumen mensual no procede fijar un presupuesto absoluto. El panel debe mostrar coste por documento y proyectar el mes con el promedio móvil real.

## Backup y recuperación

Mínimo exigible:

- backup diario de PostgreSQL;
- versionado o copia de objetos privados;
- copia en una segunda ubicación/proyecto;
- retención diaria/semanal/mensual definida;
- cifrado y acceso restringido;
- restauración mensual en entorno aislado;
- verificación de conteos, FKs y hashes de imágenes;
- RPO y RTO acordados.

Objetivo inicial razonable para dos usuarios: RPO de 24 horas como máximo, preferiblemente menor para ingestas, y RTO documentado de pocas horas.

## Feedback y observabilidad

Panel mínimo:

- trabajos por estado y atascados;
- longitud y edad de la cola;
- documentos pendientes de revisión;
- duplicados exactos/probables;
- discrepancias por proveedor/formato;
- tasa de corrección por campo;
- exactitud sobre dataset dorado;
- latencia OCR/extracción/revisión;
- coste por documento, usuario y mes;
- errores y reintentos por proveedor externo.

Feedback desde Telegram:

- `Dato incorrecto`;
- `Falta una línea`;
- `Es duplicado`;
- `Documento ilegible`;
- comentario libre.

Todo feedback debe quedar asociado a documento, versión, usuario y campo.

## Plan priorizado

### Fase 0 — congelar riesgos

- No usar datos no revisados como definitivos.
- Hacer obligatoria la whitelist.
- Cerrar bucket y RPC privilegiada.
- Preparar backup y comprobar restauración.

### Fase 1 — ingesta durable y atómica

- Persistir originales antes del ACK.
- Cola durable, acotada y con lease.
- Estados de ingesta.
- Commit transaccional e idempotente.
- No descartar updates de Telegram.

### Fase 2 — verdad contable y revisión

- Separar observado, calculado y aceptado.
- Reglas estrictas de descuento/IVA/total.
- Revisiones persistentes por documento/campo.
- Modo manuscrito forzado.
- Dedup exacto + probable.

### Fase 3 — operación

- Migraciones, lock de dependencias y despliegue reproducible.
- Métricas, alertas, costes y runbooks.
- Correcciones auditadas con reconciliación.
- Panel y feedback.

### Fase 4 — validación de salida

- Dataset dorado representativo por proveedor.
- Pruebas end-to-end y de fallos parciales.
- Ráfagas, reinicios, duplicados visuales y dos usuarios.
- Restore y rollback ensayados.

## Criterios de salida a producción

No se considerará listo hasta demostrar:

- cero pérdida en una prueba de reinicio con jobs en cada etapa;
- cero albaranes parciales después de fallos inyectados;
- revisiones independientes para múltiples documentos y usuarios;
- duplicados exactos bloqueados y candidatos débiles revisables;
- invariantes de base, IVA y total verificadas;
- manuscritos nunca autoaprobados sin evidencia;
- backup restaurado correctamente;
- autorización fail-closed y documentos privados;
- costes reales visibles y límites activos;
- despliegue y rollback reproducibles;
- exactitud acordada sobre el dataset dorado, medida por campo y no solo por documento.

## Código superfluo o repetido

No se recomienda una limpieza amplia antes de estabilizar la arquitectura. La reducción segura posterior incluye:

- unificar deduplicación de OCR y manual;
- unificar formateo numérico/fechas;
- sustituir acceso a globales privados de la cola por servicios explícitos;
- retirar retorno de teclados que siempre es `None`;
- eliminar constantes de coste muertas cuando exista medición real;
- mostrar o retirar `_stats['errores']`;
- dividir el prompt monolítico y versionarlo.

La prioridad no es reducir líneas ahora, sino evitar que una limpieza oculte fallos de integridad.

## Conclusión

La debilidad percibida por el propietario es real. El sistema ha mejorado casos concretos de OCR, pero todavía confunde “los números cuadran entre sí” con “los números son los del documento”. La reconstrucción debe convertir el bot en un flujo de ingesta verificable: primero preservar la evidencia, después extraer candidatos, luego validar y revisar, y solo al final publicar datos contables.
