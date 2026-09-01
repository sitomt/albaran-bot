-- Snapshot de solo lectura para el backend del dashboard. El navegador nunca
-- recibe service_role ni consulta tablas directamente: un backend privado invoca
-- esta RPC y entrega únicamente este contrato agregado/sanitizado.
BEGIN;

CREATE OR REPLACE FUNCTION public.dashboard_snapshot_v1()
RETURNS JSONB
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
WITH
bounds AS (
    SELECT
        date_trunc('day', now() AT TIME ZONE 'Europe/Madrid')
            AT TIME ZONE 'Europe/Madrid' AS today_start,
        date_trunc('month', now() AT TIME ZONE 'Europe/Madrid')
            AT TIME ZONE 'Europe/Madrid' AS month_start
),
ingestion_counts AS (
    SELECT COALESCE(jsonb_object_agg(status, amount), '{}'::jsonb) AS value
    FROM (
        SELECT status, count(*) AS amount
        FROM public.ingestions
        GROUP BY status
        ORDER BY status
    ) grouped
),
job_counts AS (
    SELECT COALESCE(jsonb_object_agg(estado, amount), '{}'::jsonb) AS value
    FROM (
        SELECT estado, count(*) AS amount
        FROM public.jobs
        GROUP BY estado
        ORDER BY estado
    ) grouped
),
review_counts AS (
    SELECT COALESCE(jsonb_object_agg(status, amount), '{}'::jsonb) AS value
    FROM (
        SELECT status, count(*) AS amount
        FROM public.review_items
        GROUP BY status
        ORDER BY status
    ) grouped
),
open_reviews_by_reason AS (
    SELECT COALESCE(jsonb_agg(
        jsonb_build_object('reason_code', reason_code, 'count', amount)
        ORDER BY amount DESC, reason_code
    ), '[]'::jsonb) AS value
    FROM (
        SELECT reason_code, count(*) AS amount
        FROM public.review_items
        WHERE status = 'open'
        GROUP BY reason_code
    ) grouped
),
monthly_usage AS (
    SELECT usage.*
    FROM public.ai_usage_events usage
    CROSS JOIN bounds
    WHERE usage.created_at >= bounds.month_start
),
cost_totals AS (
    SELECT jsonb_build_object(
        'currency', 'USD',
        'month_estimated_usd', COALESCE(sum(cost_usd), 0),
        'today_estimated_usd', COALESCE(sum(cost_usd)
            FILTER (WHERE created_at >= bounds.today_start), 0),
        'calls_month', count(monthly_usage.id),
        'calls_today', count(monthly_usage.id)
            FILTER (WHERE monthly_usage.created_at >= bounds.today_start)
    ) AS value
    FROM bounds
    LEFT JOIN monthly_usage ON true
    GROUP BY bounds.today_start
),
cost_breakdown AS (
    SELECT COALESCE(jsonb_agg(
        jsonb_build_object(
            'operation', operation,
            'model', model,
            'calls', calls,
            'input_tokens', input_tokens,
            'output_tokens', output_tokens,
            'pages', pages,
            'retries', retries,
            'month_estimated_usd', month_cost,
            'today_estimated_usd', today_cost
        ) ORDER BY month_cost DESC, operation, model
    ), '[]'::jsonb) AS value
    FROM (
        SELECT
            usage.operation,
            usage.model,
            count(*) AS calls,
            sum(usage.input_tokens) AS input_tokens,
            sum(usage.output_tokens) AS output_tokens,
            sum(usage.pages) AS pages,
            sum(usage.retries) AS retries,
            sum(usage.cost_usd) AS month_cost,
            COALESCE(sum(usage.cost_usd)
                FILTER (WHERE usage.created_at >= bounds.today_start), 0) AS today_cost
        FROM monthly_usage usage
        CROSS JOIN bounds
        GROUP BY usage.operation, usage.model
    ) grouped
),
recent_ingestions AS (
    SELECT COALESCE(jsonb_agg(item ORDER BY received_at DESC, id DESC), '[]'::jsonb) AS value
    FROM (
        SELECT
            jsonb_build_object(
                'id', id,
                'status', status,
                'source_type', source_type,
                'received_at', received_at,
                'updated_at', updated_at,
                'confirmed_at', confirmed_at,
                'failed_at', failed_at,
                'byte_size', byte_size,
                'duplicate_of', duplicate_of,
                'duplicate_reason', duplicate_reason
            ) AS item,
            id,
            received_at
        FROM public.ingestions
        ORDER BY received_at DESC, id DESC
        LIMIT 20
    ) recent
),
recent_audit AS (
    SELECT COALESCE(jsonb_agg(item ORDER BY creado_en DESC, id DESC), '[]'::jsonb) AS value
    FROM (
        SELECT
            jsonb_build_object(
                'id', id,
                'ingestion_id', ingestion_id,
                'albaran_id', albaran_id,
                'job_id', job_id,
                'actor_type', actor_type,
                'actor_id', actor_id,
                'event_type', event_type,
                'created_at', creado_en,
                'feedback_message', CASE WHEN event_type = 'user.feedback'
                    THEN left(COALESCE(data->>'message', ''), 1500) ELSE NULL END
            ) AS item,
            id,
            creado_en
        FROM public.audit_events
        WHERE actor_type <> 'migration'
        ORDER BY creado_en DESC, id DESC
        LIMIT 30
    ) recent
),
recent_feedback AS (
    SELECT COALESCE(jsonb_agg(item ORDER BY creado_en DESC, id DESC), '[]'::jsonb) AS value
    FROM (
        SELECT
            jsonb_build_object(
                'id', id,
                'actor_id', actor_id,
                'message', left(COALESCE(data->>'message', ''), 1500),
                'created_at', creado_en
            ) AS item,
            id,
            creado_en
        FROM public.audit_events
        WHERE event_type = 'user.feedback'
        ORDER BY creado_en DESC, id DESC
        LIMIT 20
    ) recent
),
last_backup AS (
    SELECT COALESCE((
        SELECT jsonb_build_object(
            'event_type', event_type,
            'created_at', creado_en,
            'status', NULLIF(data->>'status', ''),
            'verified', CASE WHEN data ? 'verified' THEN data->'verified' ELSE NULL END,
            'storage_included', CASE WHEN data ? 'storage_included'
                THEN data->'storage_included' ELSE NULL END
        )
        FROM public.audit_events
        WHERE event_type LIKE 'backup.%' OR event_type LIKE 'system.backup.%'
        ORDER BY creado_en DESC, id DESC
        LIMIT 1
    ), 'null'::jsonb) AS value
)
SELECT jsonb_build_object(
    'schema_version', 1,
    'as_of', now(),
    'timezone', 'Europe/Madrid',
    'ingestions', ingestion_counts.value,
    'jobs', job_counts.value,
    'reviews', jsonb_build_object(
        'by_status', review_counts.value,
        'open_by_reason', open_reviews_by_reason.value
    ),
    'ai_costs', cost_totals.value || jsonb_build_object(
        'basis', 'estimated_from_configured_unit_rates',
        'by_operation_model', cost_breakdown.value
    ),
    'recent_ingestions', recent_ingestions.value,
    'recent_audit_events', recent_audit.value,
    'feedback', recent_feedback.value,
    'last_backup', last_backup.value
)
FROM ingestion_counts, job_counts, review_counts, open_reviews_by_reason,
     cost_totals, cost_breakdown, recent_ingestions, recent_audit,
     recent_feedback, last_backup;
$$;

REVOKE ALL ON FUNCTION public.dashboard_snapshot_v1()
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.dashboard_snapshot_v1() TO service_role;

INSERT INTO public.audit_events (actor_type, actor_id, event_type, data)
VALUES ('migration', '008_dashboard_snapshot', 'schema.dashboard_snapshot_enabled',
        jsonb_build_object('schema_version', 1, 'service_role_only', true))
ON CONFLICT DO NOTHING;

COMMIT;
