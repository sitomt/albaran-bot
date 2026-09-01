-- Immutable AI usage ledger. Unit prices are captured at call time so historic
-- costs remain reproducible after a provider changes its public tariff.
BEGIN;

CREATE TABLE IF NOT EXISTS public.ai_usage_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ingestion_id UUID REFERENCES public.ingestions(id) ON DELETE RESTRICT,
    request_id TEXT,
    operation TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens BIGINT NOT NULL DEFAULT 0,
    output_tokens BIGINT NOT NULL DEFAULT 0,
    pages INTEGER NOT NULL DEFAULT 0,
    retries INTEGER NOT NULL DEFAULT 0,
    input_unit_price_usd NUMERIC(18,8) NOT NULL DEFAULT 0,
    output_unit_price_usd NUMERIC(18,8) NOT NULL DEFAULT 0,
    page_unit_price_usd NUMERIC(18,8) NOT NULL DEFAULT 0,
    cost_usd NUMERIC(18,8) NOT NULL,
    user_id BIGINT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ai_usage_operation_nonempty CHECK (btrim(operation) <> ''),
    CONSTRAINT ai_usage_provider_nonempty CHECK (btrim(provider) <> ''),
    CONSTRAINT ai_usage_model_nonempty CHECK (btrim(model) <> ''),
    CONSTRAINT ai_usage_counters_nonnegative CHECK (
        input_tokens >= 0 AND output_tokens >= 0 AND pages >= 0 AND retries >= 0
    ),
    CONSTRAINT ai_usage_prices_nonnegative CHECK (
        input_unit_price_usd >= 0 AND output_unit_price_usd >= 0
        AND page_unit_price_usd >= 0 AND cost_usd >= 0
    ),
    CONSTRAINT ai_usage_has_measured_work CHECK (
        input_tokens > 0 OR output_tokens > 0 OR pages > 0 OR cost_usd > 0
    ),
    CONSTRAINT ai_usage_metadata_object CHECK (jsonb_typeof(metadata) = 'object'),
    CONSTRAINT ai_usage_request_id_nonempty CHECK (
        request_id IS NULL OR btrim(request_id) <> ''
    )
);

-- When the provider supplies a request identifier, this backstop prevents a
-- retry of our persistence code from charging the same remote call twice.
CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_usage_provider_request
    ON public.ai_usage_events(provider, request_id) WHERE request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ai_usage_ingestion_created
    ON public.ai_usage_events(ingestion_id, created_at)
    WHERE ingestion_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ai_usage_created
    ON public.ai_usage_events(created_at);
CREATE INDEX IF NOT EXISTS idx_ai_usage_user_created
    ON public.ai_usage_events(user_id, created_at) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ai_usage_provider_model_created
    ON public.ai_usage_events(provider, model, created_at);

CREATE OR REPLACE FUNCTION public.prevent_ai_usage_mutation()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog AS $$
BEGIN
    RAISE EXCEPTION 'ai_usage_events is append-only' USING ERRCODE = '55000';
END $$;

DROP TRIGGER IF EXISTS trg_ai_usage_events_immutable ON public.ai_usage_events;
CREATE TRIGGER trg_ai_usage_events_immutable
BEFORE UPDATE OR DELETE ON public.ai_usage_events
FOR EACH ROW EXECUTE FUNCTION public.prevent_ai_usage_mutation();

ALTER TABLE public.ai_usage_events ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.ai_usage_events FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.prevent_ai_usage_mutation()
    FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT ON TABLE public.ai_usage_events TO service_role;

INSERT INTO public.audit_events (actor_type, actor_id, event_type, data)
VALUES ('migration', '003_ai_usage_events', 'schema.ai_usage_ledger_enabled',
        jsonb_build_object('append_only', true, 'currency', 'USD'))
ON CONFLICT DO NOTHING;

COMMIT;
