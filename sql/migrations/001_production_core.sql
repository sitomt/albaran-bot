-- Albaran Bot - production data model
-- Forward-only migration. Apply with a privileged migration role, never with anon.
BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE OR REPLACE FUNCTION public.normalize_business_identifier(p_value TEXT)
RETURNS TEXT LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog AS $$
    SELECT upper(regexp_replace(COALESCE(p_value, ''), '[^[:alnum:]]', '', 'g'))
$$;

-- `nif` itself was unique in the prototype, but differently formatted values
-- (B-123 / B123) could still race into separate suppliers.
CREATE UNIQUE INDEX IF NOT EXISTS uq_proveedores_real_nif_normalized
    ON public.proveedores(public.normalize_business_identifier(nif))
    WHERE public.normalize_business_identifier(nif) <> ''
      AND public.normalize_business_identifier(nif) NOT LIKE 'DESCONOCIDO%';

-- The old fallback considered provider + date + total an identity. It is only a
-- similarity signal: two legitimate deliveries can have the same total.
DROP INDEX IF EXISTS public.idx_albaran_duplicado;

-- Durable receipt. The original object must exist in the private bucket before
-- a row reaches `queued`.
CREATE TABLE IF NOT EXISTS public.ingestions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key TEXT NOT NULL,
    telegram_user_id BIGINT NOT NULL,
    telegram_chat_id BIGINT NOT NULL,
    telegram_file_unique_id TEXT,
    storage_bucket TEXT NOT NULL DEFAULT 'albaranes',
    storage_path TEXT NOT NULL,
    content_type TEXT NOT NULL,
    byte_size BIGINT NOT NULL CHECK (byte_size > 0),
    image_hash TEXT NOT NULL CHECK (image_hash ~ '^[0-9a-f]{64}$'),
    perceptual_hash TEXT,
    status TEXT NOT NULL DEFAULT 'received' CHECK (
        status IN ('received','queued','processing','extracted','needs_review',
                   'confirmed','rejected','failed')
    ),
    duplicate_of UUID REFERENCES public.ingestions(id) ON DELETE RESTRICT,
    duplicate_reason TEXT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    confirmed_at TIMESTAMPTZ,
    failed_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
    CONSTRAINT ingestions_idempotency_key_nonempty CHECK (btrim(idempotency_key) <> ''),
    CONSTRAINT ingestions_storage_path_nonempty CHECK (btrim(storage_path) <> ''),
    CONSTRAINT ingestions_terminal_timestamps CHECK (
        (status <> 'confirmed' OR confirmed_at IS NOT NULL)
        AND (status <> 'failed' OR failed_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ingestions_idempotency_key
    ON public.ingestions(idempotency_key);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ingestions_exact_image
    ON public.ingestions(image_hash);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ingestions_telegram_file
    ON public.ingestions(telegram_user_id, telegram_file_unique_id)
    WHERE telegram_file_unique_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ingestions_status_received
    ON public.ingestions(status, received_at);
CREATE INDEX IF NOT EXISTS idx_ingestions_probable_duplicate
    ON public.ingestions(perceptual_hash) WHERE perceptual_hash IS NOT NULL;

-- Make existing jobs durable and leaseable without destroying compatibility
-- with the former Spanish columns used by old deployments.
ALTER TABLE public.jobs
    ADD COLUMN IF NOT EXISTS ingestion_id UUID REFERENCES public.ingestions(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS storage_path TEXT,
    ADD COLUMN IF NOT EXISTS image_hash TEXT,
    ADD COLUMN IF NOT EXISTS telegram_file_unique_id TEXT,
    ADD COLUMN IF NOT EXISTS lease_owner TEXT,
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 3,
    ADD COLUMN IF NOT EXISTS stage TEXT NOT NULL DEFAULT 'received',
    ADD COLUMN IF NOT EXISTS error_code TEXT,
    ADD COLUMN IF NOT EXISTS correlation_id UUID NOT NULL DEFAULT gen_random_uuid();

UPDATE public.jobs
SET attempts = LEAST(COALESCE(intentos, 0), 20),
    max_attempts = GREATEST(max_attempts, LEAST(COALESCE(intentos, 0), 20))
WHERE attempts = 0;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'jobs_attempts_valid') THEN
        ALTER TABLE public.jobs ADD CONSTRAINT jobs_attempts_valid
            CHECK (attempts >= 0 AND max_attempts BETWEEN 1 AND 20 AND attempts <= max_attempts);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'jobs_hash_valid') THEN
        ALTER TABLE public.jobs ADD CONSTRAINT jobs_hash_valid
            CHECK (image_hash IS NULL OR image_hash ~ '^[0-9a-f]{64}$');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'jobs_lease_complete') THEN
        ALTER TABLE public.jobs ADD CONSTRAINT jobs_lease_complete CHECK (
            (lease_owner IS NULL AND lease_expires_at IS NULL)
            OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        );
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_ingestion ON public.jobs(ingestion_id)
    WHERE ingestion_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_jobs_claim
    ON public.jobs(estado, lease_expires_at, creado_en)
    WHERE estado IN ('pendiente','procesando');

-- Canonical documents have an explicit publication lifecycle. Existing rows
-- are treated as confirmed to preserve reporting semantics during migration.
ALTER TABLE public.albaranes
    ADD COLUMN IF NOT EXISTS ingestion_id UUID REFERENCES public.ingestions(id) ON DELETE RESTRICT,
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'confirmed',
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT,
    ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS confirmed_payload_hash TEXT,
    ADD COLUMN IF NOT EXISTS confirmado_por TEXT,
    ADD COLUMN IF NOT EXISTS confirmado_en TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS actualizado_en TIMESTAMPTZ NOT NULL DEFAULT now();

UPDATE public.albaranes
SET confirmado_en = COALESCE(confirmado_en, creado_en),
    confirmado_por = COALESCE(confirmado_por, 'migration')
WHERE status = 'confirmed';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'albaranes_status_valid') THEN
        ALTER TABLE public.albaranes ADD CONSTRAINT albaranes_status_valid
            CHECK (status IN ('draft','needs_review','confirmed','rejected','archived'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'albaranes_amounts_nonnegative') THEN
        ALTER TABLE public.albaranes ADD CONSTRAINT albaranes_amounts_nonnegative CHECK (
            (base_imponible IS NULL OR base_imponible >= 0)
            AND (total_iva IS NULL OR total_iva >= 0)
            AND (total IS NULL OR total >= 0)
        );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'albaranes_confirmation_complete') THEN
        ALTER TABLE public.albaranes ADD CONSTRAINT albaranes_confirmation_complete CHECK (
            status <> 'confirmed' OR (confirmado_en IS NOT NULL AND confirmado_por IS NOT NULL)
        );
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_albaranes_ingestion
    ON public.albaranes(ingestion_id) WHERE ingestion_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_albaranes_idempotency
    ON public.albaranes(idempotency_key) WHERE idempotency_key IS NOT NULL;

DROP INDEX IF EXISTS public.uniq_albaran_prov_numnorm;
CREATE UNIQUE INDEX uniq_albaran_prov_numnorm
    ON public.albaranes(proveedor_id, numero_albaran_norm)
    WHERE numero_albaran_norm <> '' AND status NOT IN ('rejected','archived');

ALTER TABLE public.lineas_albaran
    ALTER COLUMN albaran_id SET NOT NULL,
    ADD COLUMN IF NOT EXISTS line_no INTEGER,
    ADD COLUMN IF NOT EXISTS estado TEXT NOT NULL DEFAULT 'accepted',
    ADD COLUMN IF NOT EXISTS valores_observados JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS valores_calculados JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS decisiones JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS confirmado_por TEXT,
    ADD COLUMN IF NOT EXISTS confirmado_en TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS creado_en TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS actualizado_en TIMESTAMPTZ NOT NULL DEFAULT now();

WITH numbered AS (
    SELECT id, row_number() OVER (PARTITION BY albaran_id ORDER BY id)::integer AS n
    FROM public.lineas_albaran WHERE line_no IS NULL
)
UPDATE public.lineas_albaran l SET line_no = numbered.n
FROM numbered WHERE numbered.id = l.id;
ALTER TABLE public.lineas_albaran ALTER COLUMN line_no SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'lineas_line_no_valid') THEN
        ALTER TABLE public.lineas_albaran ADD CONSTRAINT lineas_line_no_valid CHECK (line_no > 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'lineas_estado_valid') THEN
        ALTER TABLE public.lineas_albaran ADD CONSTRAINT lineas_estado_valid
            CHECK (estado IN ('candidate','accepted','rejected'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'lineas_confianza_valid') THEN
        ALTER TABLE public.lineas_albaran ADD CONSTRAINT lineas_confianza_valid
            CHECK (confianza BETWEEN 0 AND 100);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'lineas_numeric_valid') THEN
        ALTER TABLE public.lineas_albaran ADD CONSTRAINT lineas_numeric_valid CHECK (
            (cantidad IS NULL OR cantidad > 0)
            AND (precio_unitario IS NULL OR precio_unitario >= 0)
            AND (importe_neto IS NULL OR importe_neto >= 0)
            AND (descuento_pct IS NULL OR descuento_pct BETWEEN 0 AND 100)
            AND (unidades_por_envase IS NULL OR unidades_por_envase > 0)
        );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'lineas_provenance_objects') THEN
        ALTER TABLE public.lineas_albaran ADD CONSTRAINT lineas_provenance_objects CHECK (
            jsonb_typeof(valores_observados) = 'object'
            AND jsonb_typeof(valores_calculados) = 'object'
            AND jsonb_typeof(decisiones) = 'object'
        );
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_lineas_albaran_line_no
    ON public.lineas_albaran(albaran_id, line_no);

-- Immutable, versioned OCR/LLM evidence. Nothing here is treated as canonical
-- until confirm_albaran_v1 succeeds.
CREATE TABLE IF NOT EXISTS public.extraction_artifacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ingestion_id UUID NOT NULL REFERENCES public.ingestions(id) ON DELETE CASCADE,
    attempt INTEGER NOT NULL CHECK (attempt > 0),
    artifact_type TEXT NOT NULL CHECK (
        artifact_type IN ('ocr_raw','ocr_layout','llm_raw','candidate','validation','thumbnail')
    ),
    model_name TEXT,
    model_version TEXT,
    prompt_version TEXT,
    payload JSONB NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    pages INTEGER CHECK (pages IS NULL OR pages > 0),
    cost_usd NUMERIC(12,6) CHECK (cost_usd IS NULL OR cost_usd >= 0),
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    complete BOOLEAN NOT NULL DEFAULT true,
    creado_en TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, ingestion_id),
    UNIQUE (ingestion_id, attempt, artifact_type, payload_sha256)
);
CREATE INDEX IF NOT EXISTS idx_extraction_artifacts_ingestion
    ON public.extraction_artifacts(ingestion_id, attempt, creado_en);

-- One durable review per field. A user can have several documents pending at
-- once; no process memory is part of this contract.
CREATE TABLE IF NOT EXISTS public.review_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ingestion_id UUID NOT NULL REFERENCES public.ingestions(id) ON DELETE CASCADE,
    extraction_artifact_id UUID NOT NULL,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('document','vat','line')),
    entity_key TEXT NOT NULL,
    field_name TEXT NOT NULL,
    observed_value JSONB,
    calculated_value JSONB,
    proposed_value JSONB,
    accepted_value JSONB,
    reason_code TEXT NOT NULL,
    confidence NUMERIC(5,2) CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 100),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','accepted','corrected','rejected')),
    resolved_by TEXT,
    resolved_at TIMESTAMPTZ,
    resolution_note TEXT,
    creado_en TIMESTAMPTZ NOT NULL DEFAULT now(),
    actualizado_en TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT review_resolution_complete CHECK (
        (status = 'open' AND resolved_by IS NULL AND resolved_at IS NULL)
        OR (status <> 'open' AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL)
    ),
    CONSTRAINT review_artifact_same_ingestion FOREIGN KEY (extraction_artifact_id, ingestion_id)
        REFERENCES public.extraction_artifacts(id, ingestion_id) ON DELETE RESTRICT,
    UNIQUE (extraction_artifact_id, entity_type, entity_key, field_name)
);
CREATE INDEX IF NOT EXISTS idx_review_items_open
    ON public.review_items(ingestion_id, creado_en) WHERE status = 'open';

-- Append-only event stream. The former auditoria table remains as a legacy
-- compatibility view of operational metrics until application migration ends.
CREATE TABLE IF NOT EXISTS public.audit_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ingestion_id UUID REFERENCES public.ingestions(id) ON DELETE SET NULL,
    albaran_id UUID REFERENCES public.albaranes(id) ON DELETE SET NULL,
    job_id UUID REFERENCES public.jobs(id) ON DELETE SET NULL,
    correlation_id UUID,
    actor_type TEXT NOT NULL CHECK (actor_type IN ('system','telegram_user','operator','migration')),
    actor_id TEXT,
    event_type TEXT NOT NULL,
    event_version INTEGER NOT NULL DEFAULT 1 CHECK (event_version > 0),
    data JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(data) = 'object'),
    creado_en TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_events_ingestion
    ON public.audit_events(ingestion_id, creado_en);
CREATE INDEX IF NOT EXISTS idx_audit_events_albaran
    ON public.audit_events(albaran_id, creado_en);
CREATE UNIQUE INDEX IF NOT EXISTS uq_audit_migration_event_once
    ON public.audit_events(actor_id, event_type)
    WHERE actor_type = 'migration';

CREATE OR REPLACE FUNCTION public.set_updated_at()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
    NEW.updated_at = clock_timestamp();
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_ingestions_updated_at ON public.ingestions;
CREATE TRIGGER trg_ingestions_updated_at BEFORE UPDATE ON public.ingestions
FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();
DROP TRIGGER IF EXISTS trg_review_items_updated_at ON public.review_items;
CREATE TRIGGER trg_review_items_updated_at BEFORE UPDATE ON public.review_items
FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE OR REPLACE FUNCTION public.prevent_audit_mutation()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only' USING ERRCODE = '55000';
END $$;
DROP TRIGGER IF EXISTS trg_audit_events_immutable ON public.audit_events;
CREATE TRIGGER trg_audit_events_immutable BEFORE UPDATE OR DELETE ON public.audit_events
FOR EACH ROW EXECUTE FUNCTION public.prevent_audit_mutation();

-- Dynamic SQL is not an acceptable public RPC, even when it begins with SELECT.
DROP FUNCTION IF EXISTS public.execute_select(text);

-- Atomic worker claim using SKIP LOCKED. Returns no row when there is no work.
CREATE OR REPLACE FUNCTION public.claim_ingestion_job_v1(
    p_worker_id TEXT,
    p_lease_seconds INTEGER DEFAULT 300
) RETURNS SETOF public.jobs
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, extensions AS $$
DECLARE v_job public.jobs%ROWTYPE;
BEGIN
    IF btrim(COALESCE(p_worker_id, '')) = '' OR p_lease_seconds NOT BETWEEN 30 AND 3600 THEN
        RAISE EXCEPTION 'invalid lease arguments' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO v_job FROM public.jobs
    WHERE estado IN ('pendiente','procesando')
      AND attempts < max_attempts
      AND (lease_expires_at IS NULL OR lease_expires_at < clock_timestamp())
    ORDER BY creado_en
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
END $$;

-- Atomic canonical publication. `precio_unitario` is the accepted net unit
-- price; discount is provenance only. Tolerances cover decimal rounding, not a
-- percentage of the invoice.
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
        IF NULLIF(v_line->>'precio_unitario','') IS NOT NULL
           AND abs((v_line->>'cantidad')::numeric * (v_line->>'precio_unitario')::numeric
                   - (v_line->>'importe_neto')::numeric) > 0.03 THEN
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
            IF jsonb_typeof(v_line) <> 'object'
               OR NULLIF(v_line->>'tipo','') IS NULL
               OR NULLIF(v_line->>'base','') IS NULL
               OR NULLIF(v_line->>'cuota','') IS NULL
               OR (v_line->>'tipo')::numeric < 0 OR (v_line->>'base')::numeric < 0
               OR (v_line->>'cuota')::numeric < 0
               OR abs((v_line->>'base')::numeric * (v_line->>'tipo')::numeric / 100
                      - (v_line->>'cuota')::numeric) > 0.03 THEN
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

-- Backend-only database. Telegram identities are not Supabase Auth identities;
-- therefore the process must use a server-side service role and clients get no
-- direct table/RPC access.
ALTER TABLE public.proveedores ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.productos_catalogo ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.albaranes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.lineas_albaran ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.auditoria ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.correcciones ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ingestions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.extraction_artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.review_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.audit_events ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.proveedores, public.productos_catalogo, public.albaranes,
    public.lineas_albaran, public.auditoria, public.jobs, public.correcciones,
    public.ingestions, public.extraction_artifacts, public.review_items,
    public.audit_events FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.claim_ingestion_job_v1(TEXT, INTEGER) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.confirm_albaran_v1(UUID, TEXT, TEXT, TEXT, JSONB, JSONB, UUID)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.normalize_business_identifier(TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.claim_ingestion_job_v1(TEXT, INTEGER) TO service_role;
GRANT EXECUTE ON FUNCTION public.confirm_albaran_v1(UUID, TEXT, TEXT, TEXT, JSONB, JSONB, UUID)
    TO service_role;
GRANT EXECUTE ON FUNCTION public.normalize_business_identifier(TEXT) TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.proveedores, public.productos_catalogo,
    public.albaranes, public.lineas_albaran, public.auditoria, public.jobs,
    public.correcciones, public.ingestions, public.extraction_artifacts,
    public.review_items TO service_role;
GRANT SELECT, INSERT ON TABLE public.audit_events TO service_role;

-- Prevent Supabase's common default grants from exposing future objects created
-- by the migration owner. Explicit backend grants remain required.
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC, anon, authenticated;

-- Supabase Storage: private object delivery only (signed URLs or backend bytes).
DO $storage$
BEGIN
    IF to_regclass('storage.buckets') IS NOT NULL THEN
        EXECUTE $sql$INSERT INTO storage.buckets (id, name, public)
                     VALUES ('albaranes','albaranes',false)
                     ON CONFLICT (id) DO UPDATE SET public=false$sql$;
        EXECUTE 'REVOKE ALL ON TABLE storage.buckets FROM anon, authenticated';
    END IF;
    IF to_regclass('storage.objects') IS NOT NULL THEN
        EXECUTE 'REVOKE ALL ON TABLE storage.objects FROM anon, authenticated';
    END IF;
END $storage$;

COMMIT;
