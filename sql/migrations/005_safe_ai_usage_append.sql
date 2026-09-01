-- Inserción idempotente del ledger de IA sin convertir un replay en UPDATE.
-- El trigger de 003 mantiene la tabla append-only; esta RPC absorbe conflictos
-- tanto por UUID local como por provider + request_id del proveedor.
BEGIN;

CREATE OR REPLACE FUNCTION public.append_ai_usage_event_v1(p_event JSONB)
RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE
    v_inserted_id UUID;
BEGIN
    IF jsonb_typeof(p_event) <> 'object' THEN
        RAISE EXCEPTION 'AI usage event must be an object' USING ERRCODE = '22023';
    END IF;

    INSERT INTO public.ai_usage_events (
        id, ingestion_id, request_id, operation, provider, model,
        input_tokens, output_tokens, pages, retries,
        input_unit_price_usd, output_unit_price_usd, page_unit_price_usd,
        cost_usd, user_id, metadata, created_at
    ) VALUES (
        NULLIF(p_event->>'id', '')::uuid,
        NULLIF(p_event->>'ingestion_id', '')::uuid,
        NULLIF(p_event->>'request_id', ''),
        p_event->>'operation',
        p_event->>'provider',
        p_event->>'model',
        COALESCE(NULLIF(p_event->>'input_tokens', '')::bigint, 0),
        COALESCE(NULLIF(p_event->>'output_tokens', '')::bigint, 0),
        COALESCE(NULLIF(p_event->>'pages', '')::integer, 0),
        COALESCE(NULLIF(p_event->>'retries', '')::integer, 0),
        COALESCE(NULLIF(p_event->>'input_unit_price_usd', '')::numeric, 0),
        COALESCE(NULLIF(p_event->>'output_unit_price_usd', '')::numeric, 0),
        COALESCE(NULLIF(p_event->>'page_unit_price_usd', '')::numeric, 0),
        NULLIF(p_event->>'cost_usd', '')::numeric,
        NULLIF(p_event->>'user_id', '')::bigint,
        COALESCE(p_event->'metadata', '{}'::jsonb),
        COALESCE(NULLIF(p_event->>'created_at', '')::timestamptz, clock_timestamp())
    )
    ON CONFLICT DO NOTHING
    RETURNING id INTO v_inserted_id;

    RETURN jsonb_build_object(
        'accepted', true,
        'inserted', v_inserted_id IS NOT NULL,
        'event_id', COALESCE(v_inserted_id::text, p_event->>'id')
    );
END $$;

REVOKE ALL ON FUNCTION public.append_ai_usage_event_v1(JSONB)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.append_ai_usage_event_v1(JSONB) TO service_role;

INSERT INTO public.audit_events (actor_type, actor_id, event_type, data)
VALUES ('migration', '005_safe_ai_usage_append', 'schema.safe_ai_usage_append_enabled',
        jsonb_build_object('conflict_strategy', 'insert_only'))
ON CONFLICT DO NOTHING;

COMMIT;
