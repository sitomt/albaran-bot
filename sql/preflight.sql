-- Diagnóstico read-only previo a 001. Falla antes de cualquier DDL si los datos
-- legados requieren una decisión humana. Ejecutar con ON_ERROR_STOP.
\set ON_ERROR_STOP on

DO $$
DECLARE problems TEXT[] := ARRAY[]::TEXT[];
BEGIN
    IF EXISTS (SELECT 1 FROM public.jobs WHERE COALESCE(intentos,0) > 20) THEN
        problems := array_append(problems, 'jobs con más de 20 intentos');
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.lineas_albaran
        WHERE cantidad IS NOT NULL AND cantidad <= 0
           OR precio_unitario IS NOT NULL AND precio_unitario < 0
           OR importe_neto IS NOT NULL AND importe_neto < 0
           OR descuento_pct IS NOT NULL AND descuento_pct NOT BETWEEN 0 AND 100
           OR confianza IS NOT NULL AND confianza NOT BETWEEN 0 AND 100
    ) THEN
        problems := array_append(problems, 'líneas con valores fuera de rango');
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.proveedores
        WHERE upper(regexp_replace(COALESCE(nif,''),'[^[:alnum:]]','','g')) <> ''
          AND upper(regexp_replace(COALESCE(nif,''),'[^[:alnum:]]','','g')) NOT LIKE 'DESCONOCIDO%'
        GROUP BY upper(regexp_replace(nif,'[^[:alnum:]]','','g')) HAVING count(*) > 1
    ) THEN
        problems := array_append(problems, 'proveedores con NIF normalizado duplicado');
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.albaranes
        WHERE numero_albaran_norm <> ''
        GROUP BY proveedor_id, numero_albaran_norm HAVING count(*) > 1
    ) THEN
        problems := array_append(problems, 'albaranes con proveedor+número duplicado');
    END IF;
    IF cardinality(problems) > 0 THEN
        RAISE EXCEPTION 'preflight bloqueado: %', array_to_string(problems, '; ')
            USING ERRCODE='23514';
    END IF;
END $$;

SELECT 'preflight_ok' AS result;
