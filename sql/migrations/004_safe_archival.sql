-- Corrección posterior a confirmación: nunca mutar cifras canónicas en sitio.
-- Se archiva el registro equivocado con actor/motivo y se introduce el correcto
-- mediante /manual o una nueva ingesta, conservando ambas trazas.
BEGIN;

CREATE OR REPLACE FUNCTION public.archive_albaran_v1(
    p_albaran_id UUID,
    p_actor_id TEXT,
    p_reason TEXT
) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE v_row public.albaranes%ROWTYPE;
BEGIN
    IF btrim(COALESCE(p_actor_id, '')) = '' OR length(btrim(COALESCE(p_reason, ''))) < 3 THEN
        RAISE EXCEPTION 'actor and meaningful reason are required' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO v_row FROM public.albaranes WHERE id=p_albaran_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'delivery note not found' USING ERRCODE = 'P0002';
    END IF;
    IF v_row.status = 'archived' THEN
        RETURN jsonb_build_object('albaran_id',v_row.id,'status','archived','idempotent',true);
    END IF;
    IF v_row.status <> 'confirmed' THEN
        RAISE EXCEPTION 'only confirmed delivery notes can be archived' USING ERRCODE = '55000';
    END IF;
    UPDATE public.albaranes
    SET status='archived', version=version+1, actualizado_en=clock_timestamp()
    WHERE id=p_albaran_id;
    INSERT INTO public.audit_events (
        ingestion_id, albaran_id, actor_type, actor_id, event_type, data
    ) VALUES (
        v_row.ingestion_id, v_row.id, 'telegram_user', p_actor_id,
        'albaran.archived', jsonb_build_object('reason',left(btrim(p_reason),500),
                                               'previous_version',v_row.version)
    );
    RETURN jsonb_build_object('albaran_id',v_row.id,'status','archived','idempotent',false);
END $$;

REVOKE ALL ON FUNCTION public.archive_albaran_v1(UUID, TEXT, TEXT)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.archive_albaran_v1(UUID, TEXT, TEXT) TO service_role;

INSERT INTO public.audit_events (actor_type, actor_id, event_type, data)
VALUES ('migration', '004_safe_archival', 'schema.safe_archival_enabled',
        jsonb_build_object('in_place_financial_edits', false))
ON CONFLICT DO NOTHING;

COMMIT;
