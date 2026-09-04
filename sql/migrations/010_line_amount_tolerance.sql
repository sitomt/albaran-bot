-- La reconciliación contable en confirm_albaran_v1 (001) exigía un margen fijo
-- de 3 céntimos entre cantidad×precio_unitario e importe_neto, y exigía que la
-- cuota de IVA cuadrase con base×tipo incluso cuando la cuota impresa es 0€.
-- Ambas reglas ya se corrigieron en la validación Python (accounting_validation.py)
-- porque generaban falsos descuadres: productos vendidos por peso (kg) arrastran
-- redondeo entre tarifa/descuento/neto de más de 3 céntimos, y muchos albaranes
-- de entrega (no factura) imprimen el tipo de IVA sin repercutirlo todavía. Pero
-- esta función SQL es una capa de defensa independiente que no se había tocado,
-- así que seguía rechazando en el "Confirmar definitivamente" lo que la revisión
-- de Telegram ya daba por bueno. Replicamos aquí el mismo criterio.
BEGIN;

CREATE OR REPLACE FUNCTION public.confirm_albaran_v1(
    p_ingestion_id UUID,
    p_idempotency_key TEXT,
    p_actor_type TEXT,
    p_actor_id TEXT,
    p_albaran JSONB,
    p_lineas JSONB,
    p_extraction_artifact_id UUID DEFAULT NULL
) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, extensions AS $$
DECLARE
    v_existing public.albaranes%ROWTYPE;
    v_ingestion public.ingestions%ROWTYPE;
    v_albaran_id UUID;
    v_provider_id UUID;
    v_provider_name TEXT;
    v_provider_nif TEXT;
    v_provider_nif_norm TEXT;
    v_payload_hash TEXT;
    v_line JSONB;
    v_line_count INTEGER;
    v_line_sum NUMERIC(14,2);
    v_base NUMERIC(14,2);
    v_iva NUMERIC(14,2);
    v_total NUMERIC(14,2);
    v_tolerance NUMERIC(14,2);
    v_detail JSONB;
    v_detail_base NUMERIC(14,2);
    v_detail_iva NUMERIC(14,2);
BEGIN
    IF p_ingestion_id IS NULL OR btrim(COALESCE(p_idempotency_key, '')) = '' THEN
        RAISE EXCEPTION 'ingestion and idempotency key are required' USING ERRCODE = '22023';
    END IF;
    IF p_actor_type NOT IN ('telegram_user','operator','system') OR btrim(COALESCE(p_actor_id,'')) = '' THEN
        RAISE EXCEPTION 'valid actor is required' USING ERRCODE = '22023';
    END IF;
    IF jsonb_typeof(p_albaran) <> 'object' OR jsonb_typeof(p_lineas) <> 'array'
       OR jsonb_array_length(p_lineas) = 0 THEN
        RAISE EXCEPTION 'header object and at least one line are required' USING ERRCODE = '22023';
    END IF;

    -- Serializes retries with the same logical request.
    PERFORM pg_advisory_xact_lock(hashtextextended(p_idempotency_key, 0));
    v_payload_hash := encode(digest((p_albaran || jsonb_build_object('lineas', p_lineas))::text, 'sha256'), 'hex');

    SELECT * INTO v_existing FROM public.albaranes WHERE idempotency_key = p_idempotency_key;
    IF FOUND THEN
        IF v_existing.confirmed_payload_hash <> v_payload_hash THEN
            RAISE EXCEPTION 'idempotency key reused with a different payload' USING ERRCODE = '23505';
        END IF;
        RETURN jsonb_build_object('albaran_id', v_existing.id, 'status', v_existing.status,
                                  'idempotent', true, 'line_count',
                                  (SELECT count(*) FROM public.lineas_albaran WHERE albaran_id=v_existing.id),
                                  'version', v_existing.version);
    END IF;

    SELECT * INTO v_ingestion FROM public.ingestions WHERE id = p_ingestion_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'ingestion not found' USING ERRCODE = 'P0002'; END IF;
    IF v_ingestion.status IN ('rejected','failed') THEN
        RAISE EXCEPTION 'ingestion is terminal: %', v_ingestion.status USING ERRCODE = '55000';
    END IF;
    IF EXISTS (SELECT 1 FROM public.review_items WHERE ingestion_id=p_ingestion_id AND status='open') THEN
        RAISE EXCEPTION 'open review items must be resolved first' USING ERRCODE = '23514';
    END IF;
    IF p_extraction_artifact_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM public.extraction_artifacts
        WHERE id=p_extraction_artifact_id AND ingestion_id=p_ingestion_id AND complete
    ) THEN
        RAISE EXCEPTION 'complete extraction artifact not found for ingestion' USING ERRCODE = '23503';
    END IF;

    -- A provider is part of the same transaction: no supplier is left orphaned
    -- if validation or line insertion fails. Existing callers may still pass an
    -- explicit proveedor_id.
    v_provider_id := NULLIF(p_albaran->>'proveedor_id','')::uuid;
    IF v_provider_id IS NOT NULL THEN
        IF NOT EXISTS (SELECT 1 FROM public.proveedores WHERE id=v_provider_id) THEN
            RAISE EXCEPTION 'provider not found' USING ERRCODE = '23503';
        END IF;
    ELSE
        v_provider_name := btrim(COALESCE(p_albaran->>'proveedor_nombre',''));
        v_provider_nif := NULLIF(btrim(COALESCE(p_albaran->>'proveedor_nif','')), '');
        v_provider_nif_norm := public.normalize_business_identifier(v_provider_nif);
        IF v_provider_name = '' THEN
            RAISE EXCEPTION 'proveedor_id or proveedor_nombre is required' USING ERRCODE = '23514';
        END IF;

        -- Serializes equivalent names/NIFs inside this RPC. The expression
        -- unique index is the final backstop against other writers.
        PERFORM pg_advisory_xact_lock(hashtextextended(
            'provider:' || COALESCE(NULLIF(v_provider_nif_norm,''), lower(v_provider_name)), 0));
        IF v_provider_nif_norm <> '' THEN
            SELECT id INTO v_provider_id FROM public.proveedores
            WHERE public.normalize_business_identifier(nif)=v_provider_nif_norm LIMIT 1;
            IF v_provider_id IS NULL THEN
                -- A previous document may have created this same named supplier
                -- with a generated placeholder. Safely enrich only placeholders.
                SELECT id INTO v_provider_id FROM public.proveedores
                WHERE nombre_normalizado=lower(v_provider_name)
                  AND public.normalize_business_identifier(nif) LIKE 'DESCONOCIDO%'
                ORDER BY creado_en LIMIT 1 FOR UPDATE;
                IF v_provider_id IS NOT NULL THEN
                    UPDATE public.proveedores SET
                        nif=v_provider_nif,
                        direccion=COALESCE(direccion, NULLIF(p_albaran->>'proveedor_direccion','')),
                        telefono=COALESCE(telefono, NULLIF(p_albaran->>'proveedor_telefono','')),
                        email=COALESCE(email, NULLIF(p_albaran->>'proveedor_email','')),
                        forma_pago_habitual=COALESCE(
                            forma_pago_habitual, NULLIF(p_albaran->>'forma_pago',''))
                    WHERE id=v_provider_id;
                END IF;
            END IF;
        ELSE
            SELECT id INTO v_provider_id FROM public.proveedores
            WHERE nombre_normalizado=lower(v_provider_name)
            ORDER BY creado_en LIMIT 1 FOR UPDATE;
        END IF;

        IF v_provider_id IS NULL THEN
            INSERT INTO public.proveedores (
                nombre, nif, direccion, telefono, email, forma_pago_habitual
            ) VALUES (
                v_provider_name,
                COALESCE(v_provider_nif, 'DESCONOCIDO-' || upper(substr(gen_random_uuid()::text,1,8))),
                NULLIF(p_albaran->>'proveedor_direccion',''),
                NULLIF(p_albaran->>'proveedor_telefono',''),
                NULLIF(p_albaran->>'proveedor_email',''),
                NULLIF(p_albaran->>'forma_pago','')
            ) RETURNING id INTO v_provider_id;
        END IF;
    END IF;
    IF NULLIF(p_albaran->>'fecha','') IS NULL THEN
        RAISE EXCEPTION 'confirmed delivery note requires a date' USING ERRCODE = '23514';
    END IF;

    v_base := NULLIF(p_albaran->>'base_imponible','')::numeric;
    v_iva := NULLIF(p_albaran->>'total_iva','')::numeric;
    v_total := NULLIF(p_albaran->>'total','')::numeric;
    IF v_base IS NULL AND v_total IS NULL THEN
        RAISE EXCEPTION 'confirmed delivery note requires base or total' USING ERRCODE = '23514';
    END IF;
    IF COALESCE(v_base,0) < 0 OR COALESCE(v_iva,0) < 0 OR COALESCE(v_total,0) < 0 THEN
        RAISE EXCEPTION 'negative totals are not valid' USING ERRCODE = '23514';
    END IF;
    IF v_base IS NOT NULL AND v_iva IS NOT NULL AND v_total IS NOT NULL
       AND abs(v_base + v_iva - v_total) > 0.03 THEN
        RAISE EXCEPTION 'base + VAT does not reconcile with total' USING ERRCODE = '23514';
    END IF;

    SELECT count(*), round(sum((x->>'importe_neto')::numeric), 2)
      INTO v_line_count, v_line_sum FROM jsonb_array_elements(p_lineas) x;
    v_tolerance := 0.02 + (v_line_count * 0.01);
    FOR v_line IN SELECT value FROM jsonb_array_elements(p_lineas) LOOP
        IF jsonb_typeof(v_line) <> 'object'
           OR btrim(COALESCE(v_line->>'descripcion_limpia', v_line->>'descripcion_original', '')) = ''
           OR NULLIF(v_line->>'cantidad','') IS NULL
           OR NULLIF(v_line->>'importe_neto','') IS NULL
           OR NULLIF(v_line->>'cantidad','')::numeric <= 0
           OR NULLIF(v_line->>'importe_neto','')::numeric < 0 THEN
            RAISE EXCEPTION 'every line requires description, positive quantity and nonnegative net amount'
                USING ERRCODE = '23514';
        END IF;
        IF NULLIF(v_line->>'producto_catalogo_id','') IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM public.productos_catalogo
            WHERE id=(v_line->>'producto_catalogo_id')::uuid AND proveedor_id=v_provider_id
        ) THEN
            RAISE EXCEPTION 'catalog product does not belong to the delivery note provider'
                USING ERRCODE = '23503';
        END IF;
        -- Productos por peso (kg): la tarifa y el descuento se calculan con más
        -- decimales de los que se imprimen, así que el importe final arrastra
        -- redondeo. Toleramos 3 céntimos o un 0,5% del importe, lo que sea mayor,
        -- igual que accounting_validation._line_amount_tolerance en Python.
        IF NULLIF(v_line->>'precio_unitario','') IS NOT NULL
           AND abs((v_line->>'cantidad')::numeric * (v_line->>'precio_unitario')::numeric
                   - (v_line->>'importe_neto')::numeric)
               > GREATEST(0.03, round((v_line->>'importe_neto')::numeric * 0.005, 2)) THEN
            RAISE EXCEPTION 'line amount does not reconcile with accepted net unit price'
                USING ERRCODE = '23514';
        END IF;
    END LOOP;
    IF v_base IS NOT NULL AND abs(v_line_sum - v_base) > v_tolerance THEN
        RAISE EXCEPTION 'sum of lines does not reconcile with taxable base' USING ERRCODE = '23514';
    END IF;

    v_detail := p_albaran->'detalle_iva';
    IF v_detail IS NOT NULL AND v_detail <> 'null'::jsonb THEN
        IF jsonb_typeof(v_detail) <> 'array' THEN
            RAISE EXCEPTION 'VAT detail must be an array' USING ERRCODE = '22023';
        END IF;
        IF jsonb_array_length(v_detail) = 0 AND COALESCE(v_iva, 0) <> 0 THEN
            RAISE EXCEPTION 'nonzero VAT requires at least one VAT tranche' USING ERRCODE = '23514';
        END IF;
        FOR v_line IN SELECT value FROM jsonb_array_elements(v_detail) LOOP
            -- Cuota impresa en 0 con un tipo distinto de 0 no es un error: es
            -- habitual en albaranes de entrega (no factura) donde el IVA se
            -- indica como referencia pero todavía no se repercute.
            IF jsonb_typeof(v_line) <> 'object'
               OR NULLIF(v_line->>'tipo','') IS NULL
               OR NULLIF(v_line->>'base','') IS NULL
               OR NULLIF(v_line->>'cuota','') IS NULL
               OR (v_line->>'tipo')::numeric < 0 OR (v_line->>'base')::numeric < 0
               OR (v_line->>'cuota')::numeric < 0
               OR ((v_line->>'cuota')::numeric <> 0
                   AND abs((v_line->>'base')::numeric * (v_line->>'tipo')::numeric / 100
                          - (v_line->>'cuota')::numeric) > 0.03) THEN
                RAISE EXCEPTION 'invalid VAT tranche' USING ERRCODE = '23514';
            END IF;
        END LOOP;
        SELECT round(sum((x->>'base')::numeric),2), round(sum((x->>'cuota')::numeric),2)
          INTO v_detail_base, v_detail_iva FROM jsonb_array_elements(v_detail) x;
        IF v_base IS NOT NULL AND abs(v_detail_base-v_base) > 0.03 THEN
            RAISE EXCEPTION 'VAT bases do not reconcile' USING ERRCODE = '23514';
        END IF;
        IF v_iva IS NOT NULL AND abs(v_detail_iva-v_iva) > 0.03 THEN
            RAISE EXCEPTION 'VAT quotas do not reconcile' USING ERRCODE = '23514';
        END IF;
    END IF;

    INSERT INTO public.albaranes (
        ingestion_id, idempotency_key, proveedor_id, numero_albaran, fecha,
        forma_pago, base_imponible, total_iva, total, detalle_iva, imagen_url,
        imagen_hash, origen, status, confirmed_payload_hash, confirmado_por, confirmado_en
    ) VALUES (
        p_ingestion_id, p_idempotency_key, v_provider_id, p_albaran->>'numero_albaran',
        (p_albaran->>'fecha')::date, p_albaran->>'forma_pago', v_base, v_iva, v_total,
        v_detail, v_ingestion.storage_path, v_ingestion.image_hash,
        COALESCE(NULLIF(p_albaran->>'origen',''),'ocr'), 'confirmed', v_payload_hash,
        p_actor_type || ':' || p_actor_id, clock_timestamp()
    ) RETURNING id INTO v_albaran_id;

    INSERT INTO public.lineas_albaran (
        albaran_id, line_no, producto_catalogo_id, descripcion_original,
        descripcion_limpia, cantidad, unidad, peso_unitario_g, unidades_por_envase,
        peso_total_kg, volumen_unitario_l, formato_envase, numero_lote, caducidad,
        precio_unitario, descuento_pct, importe_neto, confianza, requiere_revision,
        estado, valores_observados, valores_calculados, decisiones,
        confirmado_por, confirmado_en
    )
    SELECT v_albaran_id, ordinality::integer,
        NULLIF(x->>'producto_catalogo_id','')::uuid, x->>'descripcion_original',
        COALESCE(x->>'descripcion_limpia', x->>'descripcion_original'),
        (x->>'cantidad')::numeric, x->>'unidad', NULLIF(x->>'peso_unitario_g','')::numeric,
        NULLIF(x->>'unidades_por_envase','')::integer, NULLIF(x->>'peso_total_kg','')::numeric,
        NULLIF(x->>'volumen_unitario_l','')::numeric, x->>'formato_envase', x->>'numero_lote',
        NULLIF(x->>'caducidad','')::date, NULLIF(x->>'precio_unitario','')::numeric,
        NULLIF(x->>'descuento_pct','')::numeric, (x->>'importe_neto')::numeric,
        COALESCE(NULLIF(x->>'confianza','')::integer, 0), false, 'accepted',
        COALESCE(x->'valores_observados','{}'::jsonb),
        COALESCE(x->'valores_calculados','{}'::jsonb),
        COALESCE(x->'decisiones','{}'::jsonb),
        p_actor_type || ':' || p_actor_id, clock_timestamp()
    FROM jsonb_array_elements(p_lineas) WITH ORDINALITY AS a(x, ordinality);

    UPDATE public.ingestions SET status='confirmed', confirmed_at=clock_timestamp()
    WHERE id=p_ingestion_id;
    UPDATE public.jobs SET estado='completado', stage='confirmed', lease_owner=NULL,
        lease_expires_at=NULL, actualizado_en=clock_timestamp()
    WHERE ingestion_id=p_ingestion_id;
    INSERT INTO public.audit_events (
        ingestion_id, albaran_id, actor_type, actor_id, event_type, data
    ) VALUES (
        p_ingestion_id, v_albaran_id, p_actor_type, p_actor_id, 'albaran.confirmed',
        jsonb_build_object('payload_sha256',v_payload_hash,'line_count',v_line_count,
                           'extraction_artifact_id',p_extraction_artifact_id)
    );
    RETURN jsonb_build_object('albaran_id',v_albaran_id,'status','confirmed',
                              'idempotent',false,'line_count',v_line_count,'version',1);
END $$;

REVOKE ALL ON FUNCTION public.confirm_albaran_v1(UUID, TEXT, TEXT, TEXT, JSONB, JSONB, UUID)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.confirm_albaran_v1(UUID, TEXT, TEXT, TEXT, JSONB, JSONB, UUID)
    TO service_role;

INSERT INTO public.audit_events (actor_type,actor_id,event_type,data)
VALUES ('migration','010_line_amount_tolerance','schema.line_amount_tolerance_enabled',
        jsonb_build_object('scope','confirm_albaran_v1'))
ON CONFLICT DO NOTHING;

COMMIT;
