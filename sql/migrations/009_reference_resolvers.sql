-- Resolución global de referencias cortas. No depende de ventanas de las 100/200
-- filas más recientes y solo devuelve una fila cuando el prefijo es inequívoco.
BEGIN;

CREATE OR REPLACE FUNCTION public.normalize_uuid_reference(p_reference TEXT)
RETURNS TEXT
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog AS $$
DECLARE v_hex TEXT;
BEGIN
    IF p_reference IS NULL
       OR btrim(p_reference) !~ '^[0-9A-Fa-f-]{6,36}$' THEN
        RETURN NULL;
    END IF;
    v_hex := replace(lower(btrim(p_reference)), '-', '');
    IF v_hex !~ '^[0-9a-f]{6,32}$' THEN
        RETURN NULL;
    END IF;
    RETURN v_hex;
END $$;

CREATE OR REPLACE FUNCTION public.resolve_ingestion_reference_v1(p_reference TEXT)
RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE
    v_reference TEXT := public.normalize_uuid_reference(p_reference);
    v_count BIGINT;
    v_result JSONB;
BEGIN
    IF v_reference IS NULL THEN RETURN NULL; END IF;
    SELECT count(*), jsonb_agg(to_jsonb(i))->0
    INTO v_count, v_result
    FROM (
        SELECT * FROM public.ingestions
        WHERE replace(id::text, '-', '') LIKE v_reference || '%'
        LIMIT 2
    ) i;
    RETURN CASE WHEN v_count = 1 THEN v_result ELSE NULL END;
END $$;

CREATE OR REPLACE FUNCTION public.resolve_albaran_reference_v1(p_reference TEXT)
RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE
    v_reference TEXT := public.normalize_uuid_reference(p_reference);
    v_count BIGINT;
    v_result JSONB;
BEGIN
    IF v_reference IS NULL THEN RETURN NULL; END IF;
    SELECT count(*), jsonb_agg(
        jsonb_build_object(
            'id',a.id,
            'numero_albaran',a.numero_albaran,
            'fecha',a.fecha,
            'total',a.total,
            'base_imponible',a.base_imponible,
            'total_iva',a.total_iva,
            'detalle_iva',a.detalle_iva,
            'forma_pago',a.forma_pago,
            'version',a.version,
            'ingestion_id',a.ingestion_id,
            'status',a.status,
            'origen',a.origen,
            'creado_en',a.creado_en,
            'proveedores',CASE WHEN p.id IS NULL THEN NULL ELSE
                jsonb_build_object('nombre',p.nombre,'nif',p.nif) END
        )
    )->0
    INTO v_count, v_result
    FROM (
        SELECT * FROM public.albaranes
        WHERE replace(id::text, '-', '') LIKE v_reference || '%'
        LIMIT 2
    ) a
    LEFT JOIN public.proveedores p ON p.id=a.proveedor_id
    ;
    RETURN CASE WHEN v_count = 1 THEN v_result ELSE NULL END;
END $$;

REVOKE ALL ON FUNCTION public.normalize_uuid_reference(TEXT)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.resolve_ingestion_reference_v1(TEXT)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.resolve_albaran_reference_v1(TEXT)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.resolve_ingestion_reference_v1(TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.resolve_albaran_reference_v1(TEXT) TO service_role;

INSERT INTO public.audit_events (actor_type,actor_id,event_type,data)
VALUES ('migration','009_reference_resolvers','schema.reference_resolvers_enabled',
        jsonb_build_object('minimum_prefix_length',6,'unique_only',true))
ON CONFLICT DO NOTHING;

COMMIT;
