-- Backoff de reintentos en la cola durable.
--
-- Hasta ahora un fallo transitorio (429 del proveedor, 502, timeout) devolvía el
-- job a 'pendiente' y el siguiente ciclo del worker lo reclamaba de inmediato:
-- los tres intentos se consumían en segundos, justo cuando lo que hacía falta
-- era esperar. `available_at` permite programar cuándo vuelve a ser reclamable
-- sin bloquear al worker ni mantener el lease abierto.
BEGIN;

ALTER TABLE public.jobs
    ADD COLUMN IF NOT EXISTS available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp();

COMMENT ON COLUMN public.jobs.available_at IS
    'Instante a partir del cual el job puede reclamarse. Lo adelanta un reintento '
    'manual y lo retrasa el backoff ante fallos transitorios del proveedor.';

CREATE INDEX IF NOT EXISTS jobs_claim_order_idx
    ON public.jobs (available_at, creado_en)
    WHERE estado IN ('pendiente', 'procesando');

-- Reclamar respeta la espera programada y atiende primero al job que lleva más
-- tiempo disponible, no al más antiguo en absoluto: si no, un job penalizado por
-- backoff adelantaría siempre a los que sí pueden ejecutarse ya.
CREATE OR REPLACE FUNCTION public.claim_ingestion_job_v1(p_worker_id text, p_lease_seconds integer DEFAULT 300)
RETURNS SETOF public.jobs
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'pg_catalog', 'public', 'extensions'
AS $function$
DECLARE v_job public.jobs%ROWTYPE;
BEGIN
    IF btrim(COALESCE(p_worker_id, '')) = '' OR p_lease_seconds NOT BETWEEN 30 AND 3600 THEN
        RAISE EXCEPTION 'invalid lease arguments' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO v_job FROM public.jobs
    WHERE estado IN ('pendiente','procesando')
      AND attempts < max_attempts
      AND available_at <= clock_timestamp()
      AND (lease_expires_at IS NULL OR lease_expires_at < clock_timestamp())
    ORDER BY available_at, creado_en
    FOR UPDATE SKIP LOCKED LIMIT 1;
    IF NOT FOUND THEN RETURN; END IF;

    UPDATE public.jobs SET
        estado = 'procesando',
        lease_owner = p_worker_id,
        lease_expires_at = clock_timestamp() + make_interval(secs => p_lease_seconds),
        attempts = attempts + 1,
        intentos = attempts + 1,
        actualizado_en = clock_timestamp()
    WHERE id = v_job.id RETURNING * INTO v_job;

    UPDATE public.ingestions SET status = 'processing'
    WHERE id = v_job.ingestion_id AND status IN ('received','queued','processing');
    RETURN NEXT v_job;
END $function$;

-- Un reintento pedido por una persona no arrastra la penalización acumulada por
-- el backoff automático: se ejecuta en cuanto haya worker libre.
CREATE OR REPLACE FUNCTION public.retry_ingestion_v1(p_ingestion_id uuid, p_actor_id text)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'pg_catalog', 'public'
AS $function$
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
        lease_expires_at=NULL, available_at=clock_timestamp(),
        actualizado_en=clock_timestamp()
    WHERE id=v_job.id;
    UPDATE public.ingestions
    SET status='queued', failed_at=NULL WHERE id=p_ingestion_id;
    INSERT INTO public.audit_events (ingestion_id,job_id,actor_type,actor_id,event_type,data)
    VALUES (p_ingestion_id,v_job.id,'telegram_user',p_actor_id,
            'ingestion.manual_retry','{}'::jsonb);
    RETURN jsonb_build_object('ingestion_id',p_ingestion_id,'job_id',v_job.id,
                              'status','queued');
END $function$;

-- La huella de la migración es lo que permite auditar qué esquema corre en cada
-- entorno; el índice parcial `uq_audit_migration_event_once` la mantiene única
-- aunque la migración se reaplique.
INSERT INTO public.audit_events (actor_type,actor_id,event_type,data)
VALUES ('migration','011_retry_backoff','schema.retry_backoff_enabled',
        jsonb_build_object('column','jobs.available_at','claim_respects_backoff',true))
ON CONFLICT DO NOTHING;

COMMIT;
