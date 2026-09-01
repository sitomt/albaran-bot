-- Transiciones humanas con compare-and-swap sobre la versión exacta del
-- candidato. Un botón atrasado nunca puede confirmar, rechazar o reintentar un
-- estado que ya cambió en otra sesión.
BEGIN;

CREATE OR REPLACE FUNCTION public.accept_confirm_candidate_v1(
    p_ingestion_id UUID,
    p_candidate_artifact_id UUID,
    p_idempotency_key TEXT,
    p_actor_id TEXT,
    p_albaran JSONB,
    p_lineas JSONB
) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, extensions AS $$
DECLARE
    v_ingestion public.ingestions%ROWTYPE;
    v_candidate JSONB;
    v_result JSONB;
BEGIN
    IF p_candidate_artifact_id IS NULL OR btrim(COALESCE(p_actor_id, '')) = '' THEN
        RAISE EXCEPTION 'candidate artifact and actor are required' USING ERRCODE = '22023';
    END IF;

    SELECT * INTO v_ingestion FROM public.ingestions
    WHERE id=p_ingestion_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ingestion not found' USING ERRCODE = 'P0002';
    END IF;
    IF v_ingestion.status NOT IN ('extracted','needs_review') THEN
        RAISE EXCEPTION 'stale review: ingestion state is %', v_ingestion.status
            USING ERRCODE = '40001';
    END IF;
    IF COALESCE(v_ingestion.metadata->>'candidate_artifact_id', '')
       <> p_candidate_artifact_id::text THEN
        RAISE EXCEPTION 'stale review: candidate version changed' USING ERRCODE = '40001';
    END IF;
    IF p_idempotency_key IS DISTINCT FROM v_ingestion.idempotency_key THEN
        RAISE EXCEPTION 'idempotency key does not belong to ingestion' USING ERRCODE = '22023';
    END IF;
    SELECT payload INTO v_candidate FROM public.extraction_artifacts
        WHERE id=p_candidate_artifact_id AND ingestion_id=p_ingestion_id AND complete
          AND artifact_type='candidate';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'complete candidate artifact not found' USING ERRCODE = '23503';
    END IF;
    IF v_candidate->'header' IS DISTINCT FROM p_albaran
       OR v_candidate->'lines' IS DISTINCT FROM p_lineas THEN
        RAISE EXCEPTION 'candidate payload does not match immutable artifact'
            USING ERRCODE = '23514';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.review_items
        WHERE ingestion_id=p_ingestion_id AND status='open'
          AND extraction_artifact_id<>p_candidate_artifact_id
    ) THEN
        RAISE EXCEPTION 'stale review: another candidate has open reviews'
            USING ERRCODE = '40001';
    END IF;

    UPDATE public.review_items
    SET status='accepted',
        accepted_value=CASE
            WHEN proposed_value IS NOT NULL THEN proposed_value ELSE observed_value
        END,
        resolved_by='telegram_user:' || p_actor_id,
        resolved_at=clock_timestamp(),
        resolution_note='Confirmación completa desde Telegram',
        actualizado_en=clock_timestamp()
    WHERE ingestion_id=p_ingestion_id
      AND extraction_artifact_id=p_candidate_artifact_id
      AND status='open';

    SELECT public.confirm_albaran_v1(
        p_ingestion_id, p_idempotency_key, 'telegram_user', p_actor_id,
        p_albaran, p_lineas, p_candidate_artifact_id
    ) INTO v_result;
    RETURN v_result || jsonb_build_object('candidate_artifact_id',p_candidate_artifact_id);
END $$;

CREATE OR REPLACE FUNCTION public.reject_ingestion_v1(
    p_ingestion_id UUID,
    p_candidate_artifact_id UUID,
    p_actor_id TEXT,
    p_as_duplicate BOOLEAN DEFAULT false
) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE v_ingestion public.ingestions%ROWTYPE;
BEGIN
    IF p_candidate_artifact_id IS NULL OR btrim(COALESCE(p_actor_id, '')) = '' THEN
        RAISE EXCEPTION 'candidate artifact and actor are required' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO v_ingestion FROM public.ingestions
    WHERE id=p_ingestion_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ingestion not found' USING ERRCODE = 'P0002';
    END IF;
    IF v_ingestion.status NOT IN ('extracted','needs_review')
       OR EXISTS (SELECT 1 FROM public.albaranes WHERE ingestion_id=p_ingestion_id) THEN
        RAISE EXCEPTION 'stale rejection: ingestion is no longer rejectable'
            USING ERRCODE = '40001';
    END IF;
    IF COALESCE(v_ingestion.metadata->>'candidate_artifact_id', '')
       <> p_candidate_artifact_id::text THEN
        RAISE EXCEPTION 'stale rejection: candidate version changed' USING ERRCODE = '40001';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.review_items
        WHERE ingestion_id=p_ingestion_id AND status='open'
          AND extraction_artifact_id<>p_candidate_artifact_id
    ) THEN
        RAISE EXCEPTION 'stale rejection: another candidate has open reviews'
            USING ERRCODE = '40001';
    END IF;

    UPDATE public.review_items
    SET status='rejected', accepted_value=observed_value,
        resolved_by='telegram_user:' || p_actor_id,
        resolved_at=clock_timestamp(),
        resolution_note=CASE WHEN p_as_duplicate THEN 'Marcado como duplicado'
                             ELSE 'Rechazado por el usuario' END,
        actualizado_en=clock_timestamp()
    WHERE ingestion_id=p_ingestion_id
      AND extraction_artifact_id=p_candidate_artifact_id
      AND status='open';
    UPDATE public.ingestions
    SET status='rejected',
        duplicate_reason=CASE WHEN p_as_duplicate THEN 'confirmed_by_user'
                              ELSE 'rejected_by_user' END
    WHERE id=p_ingestion_id;
    INSERT INTO public.audit_events (
        ingestion_id, actor_type, actor_id, event_type, data
    ) VALUES (
        p_ingestion_id, 'telegram_user', p_actor_id,
        CASE WHEN p_as_duplicate THEN 'ingestion.duplicate_confirmed'
             ELSE 'ingestion.rejected' END,
        jsonb_build_object('candidate_artifact_id',p_candidate_artifact_id)
    );
    RETURN jsonb_build_object('ingestion_id',p_ingestion_id,'status','rejected',
                              'candidate_artifact_id',p_candidate_artifact_id);
END $$;

CREATE OR REPLACE FUNCTION public.retry_ingestion_v1(
    p_ingestion_id UUID,
    p_actor_id TEXT
) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE
    v_ingestion public.ingestions%ROWTYPE;
    v_job public.jobs%ROWTYPE;
BEGIN
    IF btrim(COALESCE(p_actor_id, '')) = '' THEN
        RAISE EXCEPTION 'actor is required' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO v_ingestion FROM public.ingestions
    WHERE id=p_ingestion_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ingestion not found' USING ERRCODE = 'P0002';
    END IF;
    SELECT * INTO v_job FROM public.jobs
    WHERE ingestion_id=p_ingestion_id FOR UPDATE;
    IF NOT FOUND OR v_ingestion.status<>'failed' OR v_job.estado<>'error' THEN
        RAISE EXCEPTION 'ingestion is no longer retryable' USING ERRCODE = '40001';
    END IF;

    UPDATE public.jobs SET
        estado='pendiente', stage='manual_retry', attempts=0, intentos=0,
        error_code=NULL, error_detalle=NULL, lease_owner=NULL,
        lease_expires_at=NULL, actualizado_en=clock_timestamp()
    WHERE id=v_job.id;
    UPDATE public.ingestions
    SET status='queued', failed_at=NULL WHERE id=p_ingestion_id;
    INSERT INTO public.audit_events (ingestion_id,job_id,actor_type,actor_id,event_type,data)
    VALUES (p_ingestion_id,v_job.id,'telegram_user',p_actor_id,
            'ingestion.manual_retry','{}'::jsonb);
    RETURN jsonb_build_object('ingestion_id',p_ingestion_id,'job_id',v_job.id,
                              'status','queued');
END $$;

REVOKE ALL ON FUNCTION public.accept_confirm_candidate_v1(UUID,UUID,TEXT,TEXT,JSONB,JSONB)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.reject_ingestion_v1(UUID,UUID,TEXT,BOOLEAN)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.retry_ingestion_v1(UUID,TEXT)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.accept_confirm_candidate_v1(UUID,UUID,TEXT,TEXT,JSONB,JSONB)
    TO service_role;
GRANT EXECUTE ON FUNCTION public.reject_ingestion_v1(UUID,UUID,TEXT,BOOLEAN)
    TO service_role;
GRANT EXECUTE ON FUNCTION public.retry_ingestion_v1(UUID,TEXT) TO service_role;

INSERT INTO public.audit_events (actor_type,actor_id,event_type,data)
VALUES ('migration','006_atomic_review_transitions','schema.atomic_review_transitions_enabled',
        jsonb_build_object('candidate_cas',true,'atomic_retry',true))
ON CONFLICT DO NOTHING;

COMMIT;
