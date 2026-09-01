\set ON_ERROR_STOP on

BEGIN;

DO $$
DECLARE
    v_table TEXT;
BEGIN
    FOREACH v_table IN ARRAY ARRAY[
        'ingestions','jobs','extraction_artifacts','review_items','albaranes',
        'lineas_albaran','audit_events','ai_usage_events'
    ] LOOP
        IF to_regclass('public.' || v_table) IS NULL THEN
            RAISE EXCEPTION 'missing production table: %', v_table;
        END IF;
        IF NOT (SELECT relrowsecurity FROM pg_class WHERE oid=('public.' || v_table)::regclass) THEN
            RAISE EXCEPTION 'RLS disabled on %', v_table;
        END IF;
        IF has_table_privilege('anon', 'public.' || v_table, 'SELECT')
           OR has_table_privilege('authenticated', 'public.' || v_table, 'SELECT') THEN
            RAISE EXCEPTION 'client role can read %', v_table;
        END IF;
    END LOOP;

    IF to_regprocedure('public.execute_select(text)') IS NOT NULL THEN
        RAISE EXCEPTION 'unsafe execute_select still exists';
    END IF;
    IF has_function_privilege(
        'anon', 'public.confirm_albaran_v1(uuid,text,text,text,jsonb,jsonb,uuid)', 'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'anon can execute confirmation RPC';
    END IF;
    IF has_function_privilege(
        'anon', 'public.append_ai_usage_event_v1(jsonb)', 'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'anon can append AI usage events';
    END IF;
    IF has_function_privilege(
        'anon', 'public.accept_confirm_candidate_v1(uuid,uuid,text,text,jsonb,jsonb)', 'EXECUTE'
    ) OR has_function_privilege(
        'anon', 'public.reject_ingestion_v1(uuid,uuid,text,boolean)', 'EXECUTE'
    ) OR has_function_privilege(
        'anon', 'public.retry_ingestion_v1(uuid,text)', 'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'anon can execute an atomic review transition';
    END IF;
    IF has_function_privilege(
        'anon', 'public.resolve_ingestion_reference_v1(text)', 'EXECUTE'
    ) OR has_function_privilege(
        'authenticated', 'public.resolve_albaran_reference_v1(text)', 'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'client role can resolve private references';
    END IF;
    IF has_function_privilege(
        'anon', 'public.dashboard_snapshot_v1()', 'EXECUTE'
    ) OR has_function_privilege(
        'authenticated', 'public.dashboard_snapshot_v1()', 'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'client role can execute dashboard snapshot';
    END IF;
    IF NOT has_function_privilege(
        'service_role', 'public.dashboard_snapshot_v1()', 'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'service role cannot execute dashboard snapshot';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM storage.buckets WHERE id='albaranes' AND public=false) THEN
        RAISE EXCEPTION 'albaranes bucket is not private';
    END IF;
END $$;

-- A real manual candidate can be published atomically and a replay is
-- idempotent. All rows are rolled back at the end of this contract test.
INSERT INTO public.ingestions (
    id, idempotency_key, telegram_user_id, telegram_chat_id, source_type,
    byte_size, status, metadata
) VALUES (
    '10000000-0000-0000-0000-000000000001', 'ci:valid:1', 1, 1, 'manual',
    0, 'needs_review', '{}'::jsonb
);

SELECT public.confirm_albaran_v1(
    '10000000-0000-0000-0000-000000000001', 'ci:confirm:1',
    'operator', 'ci',
    '{"proveedor_nombre":"Proveedor CI","proveedor_nif":"B12345678","numero_albaran":"CI-1","fecha":"2026-08-01","base_imponible":100,"total_iva":10,"total":110,"detalle_iva":[{"tipo":10,"base":100,"cuota":10}],"origen":"manual"}'::jsonb,
    '[{"descripcion_original":"Tomate","descripcion_limpia":"Tomate","cantidad":10,"precio_unitario":10,"importe_neto":100,"confianza":100,"valores_observados":{"cantidad":10,"precio_neto":10,"importe_neto":100}}]'::jsonb,
    NULL
);

DO $$
DECLARE v_replay JSONB;
BEGIN
    SELECT public.confirm_albaran_v1(
        '10000000-0000-0000-0000-000000000001', 'ci:confirm:1',
        'operator', 'ci',
        '{"proveedor_nombre":"Proveedor CI","proveedor_nif":"B12345678","numero_albaran":"CI-1","fecha":"2026-08-01","base_imponible":100,"total_iva":10,"total":110,"detalle_iva":[{"tipo":10,"base":100,"cuota":10}],"origen":"manual"}'::jsonb,
        '[{"descripcion_original":"Tomate","descripcion_limpia":"Tomate","cantidad":10,"precio_unitario":10,"importe_neto":100,"confianza":100,"valores_observados":{"cantidad":10,"precio_neto":10,"importe_neto":100}}]'::jsonb,
        NULL
    ) INTO v_replay;
    IF COALESCE((v_replay->>'idempotent')::boolean, false) IS NOT TRUE THEN
        RAISE EXCEPTION 'confirmation replay was not idempotent: %', v_replay;
    END IF;
    IF (SELECT status FROM public.ingestions WHERE id='10000000-0000-0000-0000-000000000001') <> 'confirmed' THEN
        RAISE EXCEPTION 'ingestion did not become confirmed';
    END IF;
    IF (SELECT count(*) FROM public.lineas_albaran l JOIN public.albaranes a ON a.id=l.albaran_id
        WHERE a.idempotency_key='ci:confirm:1') <> 1 THEN
        RAISE EXCEPTION 'confirmation did not publish exactly one line';
    END IF;
END $$;

-- A stale owner cannot reject an ingestion after the other owner confirmed it.
DO $$
BEGIN
    BEGIN
        PERFORM public.reject_ingestion_v1(
            '10000000-0000-0000-0000-000000000001',
            '20000000-0000-0000-0000-000000000002', 'stale-owner', false
        );
        RAISE EXCEPTION 'stale rejection changed a confirmed ingestion';
    EXCEPTION WHEN serialization_failure THEN
        NULL;
    END;
    IF (SELECT status FROM public.ingestions
        WHERE id='10000000-0000-0000-0000-000000000001') <> 'confirmed' THEN
        RAISE EXCEPTION 'stale rejection corrupted confirmed ingestion state';
    END IF;
END $$;

-- Candidate A was displayed, then owner B produced candidate B. Buttons for A
-- must fail their compare-and-swap without resolving or publishing B.
INSERT INTO public.ingestions (
    id,idempotency_key,telegram_user_id,telegram_chat_id,source_type,
    byte_size,status,metadata
) VALUES (
    '10000000-0000-0000-0000-000000000006','ci:cas:6',1,1,'manual',0,
    'needs_review',
    '{"candidate_artifact_id":"20000000-0000-0000-0000-000000000007"}'::jsonb
);
INSERT INTO public.extraction_artifacts (
    id,ingestion_id,attempt,artifact_type,payload,payload_sha256,complete
) VALUES
    ('20000000-0000-0000-0000-000000000006','10000000-0000-0000-0000-000000000006',
     1,'candidate','{}'::jsonb,repeat('b',64),true),
    ('20000000-0000-0000-0000-000000000007','10000000-0000-0000-0000-000000000006',
     2,'candidate',
     '{"header":{"proveedor_nombre":"Proveedor CAS","numero_albaran":"CAS-6","fecha":"2026-08-01","base_imponible":10,"total_iva":1,"total":11,"detalle_iva":[{"tipo":10,"base":10,"cuota":1}]},"lines":[{"descripcion_limpia":"Tomate","cantidad":1,"precio_unitario":10,"importe_neto":10}]}'::jsonb,
     repeat('c',64),true);
INSERT INTO public.review_items (
    ingestion_id,extraction_artifact_id,entity_type,entity_key,field_name,
    reason_code,status
) VALUES (
    '10000000-0000-0000-0000-000000000006',
    '20000000-0000-0000-0000-000000000007','document','header',
    'human_confirmation','human_confirmation_required','open'
);

-- Regression for a clean restore from 000 + every migration: 001 originally
-- attached set_updated_at() (NEW.updated_at) to a table whose real column is
-- actualizado_en. This ordinary UPDATE must execute the replacement trigger.
UPDATE public.review_items
SET resolution_note='review trigger regression probe'
WHERE ingestion_id='10000000-0000-0000-0000-000000000006'
  AND extraction_artifact_id='20000000-0000-0000-0000-000000000007';
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.review_items
        WHERE ingestion_id='10000000-0000-0000-0000-000000000006'
          AND actualizado_en IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'review actualizado_en trigger did not run';
    END IF;
END $$;
DO $$
BEGIN
    BEGIN
        PERFORM public.accept_confirm_candidate_v1(
            '10000000-0000-0000-0000-000000000006',
            '20000000-0000-0000-0000-000000000006','ci:cas:confirm:6','stale-owner',
            '{"proveedor_nombre":"Proveedor CAS","fecha":"2026-08-01","base_imponible":10,"total_iva":1,"total":11,"detalle_iva":[{"tipo":10,"base":10,"cuota":1}]}'::jsonb,
            '[{"descripcion_limpia":"Tomate","cantidad":1,"precio_unitario":10,"importe_neto":10}]'::jsonb
        );
        RAISE EXCEPTION 'stale candidate was confirmed';
    EXCEPTION WHEN serialization_failure THEN NULL;
    END;
    BEGIN
        PERFORM public.reject_ingestion_v1(
            '10000000-0000-0000-0000-000000000006',
            '20000000-0000-0000-0000-000000000006','stale-owner',false
        );
        RAISE EXCEPTION 'stale candidate was rejected';
    EXCEPTION WHEN serialization_failure THEN NULL;
    END;
    IF (SELECT status FROM public.ingestions
        WHERE id='10000000-0000-0000-0000-000000000006') <> 'needs_review'
       OR (SELECT count(*) FROM public.review_items
           WHERE ingestion_id='10000000-0000-0000-0000-000000000006'
             AND status='open') <> 1 THEN
        RAISE EXCEPTION 'stale CAS mutated the current candidate';
    END IF;
END $$;

-- The current candidate can be accepted and published in one transaction.
SELECT public.accept_confirm_candidate_v1(
    '10000000-0000-0000-0000-000000000006',
    '20000000-0000-0000-0000-000000000007','ci:cas:6','owner-current',
    '{"proveedor_nombre":"Proveedor CAS","numero_albaran":"CAS-6","fecha":"2026-08-01","base_imponible":10,"total_iva":1,"total":11,"detalle_iva":[{"tipo":10,"base":10,"cuota":1}]}'::jsonb,
    '[{"descripcion_limpia":"Tomate","cantidad":1,"precio_unitario":10,"importe_neto":10}]'::jsonb
);
DO $$
BEGIN
    IF (SELECT status FROM public.ingestions
        WHERE id='10000000-0000-0000-0000-000000000006') <> 'confirmed'
       OR EXISTS (SELECT 1 FROM public.review_items
                  WHERE ingestion_id='10000000-0000-0000-0000-000000000006'
                    AND status='open') THEN
        RAISE EXCEPTION 'atomic candidate confirmation was incomplete';
    END IF;
END $$;

-- Retry updates the job and ingestion in the same transaction and a replay is stale.
INSERT INTO public.ingestions (
    id,idempotency_key,telegram_user_id,telegram_chat_id,source_type,
    byte_size,status,failed_at,metadata
) VALUES (
    '10000000-0000-0000-0000-000000000008','ci:retry:8',1,1,'manual',0,
    'failed',clock_timestamp(),'{}'::jsonb
);
INSERT INTO public.jobs (id,telegram_user_id,ingestion_id,estado,stage,attempts,max_attempts)
VALUES ('40000000-0000-0000-0000-000000000008',1,
        '10000000-0000-0000-0000-000000000008','error','failed',3,3);
SELECT public.retry_ingestion_v1(
    '10000000-0000-0000-0000-000000000008','owner-1'
);
DO $$
BEGIN
    IF (SELECT status FROM public.ingestions
        WHERE id='10000000-0000-0000-0000-000000000008') <> 'queued'
       OR (SELECT estado FROM public.jobs
           WHERE id='40000000-0000-0000-0000-000000000008') <> 'pendiente' THEN
        RAISE EXCEPTION 'atomic retry did not update both rows';
    END IF;
    BEGIN
        PERFORM public.retry_ingestion_v1(
            '10000000-0000-0000-0000-000000000008','owner-2'
        );
        RAISE EXCEPTION 'retry replay was accepted';
    EXCEPTION WHEN serialization_failure THEN NULL;
    END;
END $$;

-- Open review items must block publication.
INSERT INTO public.ingestions (
    id, idempotency_key, telegram_user_id, telegram_chat_id, source_type,
    byte_size, status, metadata
) VALUES (
    '10000000-0000-0000-0000-000000000002', 'ci:review:1', 1, 1, 'manual',
    0, 'needs_review', '{}'::jsonb
);
INSERT INTO public.extraction_artifacts (
    id, ingestion_id, attempt, artifact_type, payload, payload_sha256, complete
) VALUES (
    '20000000-0000-0000-0000-000000000002',
    '10000000-0000-0000-0000-000000000002', 1, 'candidate', '{}'::jsonb,
    repeat('a',64), true
);
INSERT INTO public.review_items (
    ingestion_id, extraction_artifact_id, entity_type, entity_key, field_name,
    reason_code, status
) VALUES (
    '10000000-0000-0000-0000-000000000002',
    '20000000-0000-0000-0000-000000000002', 'document', 'header', 'total',
    'document_total_mismatch', 'open'
);

DO $$
BEGIN
    BEGIN
        PERFORM public.confirm_albaran_v1(
            '10000000-0000-0000-0000-000000000002', 'ci:confirm:blocked',
            'operator', 'ci',
            '{"proveedor_nombre":"Proveedor CI","fecha":"2026-08-01","base_imponible":100,"total_iva":10,"total":110,"detalle_iva":[{"tipo":10,"base":100,"cuota":10}]}'::jsonb,
            '[{"descripcion_limpia":"Tomate","cantidad":10,"precio_unitario":10,"importe_neto":100}]'::jsonb,
            '20000000-0000-0000-0000-000000000002'
        );
        RAISE EXCEPTION 'open review unexpectedly allowed confirmation';
    EXCEPTION WHEN check_violation THEN
        IF SQLERRM NOT LIKE '%open review items%' THEN RAISE; END IF;
    END;
END $$;

-- Reference resolution searches the complete tables and fails closed on an
-- ambiguous or malformed prefix.
INSERT INTO public.ingestions (
    id,idempotency_key,telegram_user_id,telegram_chat_id,source_type,
    byte_size,status,metadata
) VALUES (
    'abcdef12-0000-0000-0000-000000000009','ci:reference:9',1,1,'manual',0,
    'rejected','{}'::jsonb
);
INSERT INTO public.albaranes (
    id,proveedor_id,numero_albaran,fecha,status,origen,version,actualizado_en
)
SELECT fixture.id, source.proveedor_id, NULL, '2026-08-01', 'archived', 'manual', 1, now()
FROM (
    VALUES
      ('abc12345-0000-0000-0000-000000000009'::uuid),
      ('deadbe01-0000-0000-0000-000000000009'::uuid),
      ('deadbe02-0000-0000-0000-000000000009'::uuid)
) AS fixture(id)
CROSS JOIN LATERAL (
    SELECT proveedor_id FROM public.albaranes WHERE proveedor_id IS NOT NULL LIMIT 1
) AS source;
DO $$
DECLARE
    v_ingestion JSONB := public.resolve_ingestion_reference_v1('ABCDEF12');
    v_albaran JSONB := public.resolve_albaran_reference_v1('abc123');
BEGIN
    IF v_ingestion->>'id' <> 'abcdef12-0000-0000-0000-000000000009'
       OR v_albaran->>'id' <> 'abc12345-0000-0000-0000-000000000009'
       OR v_albaran#>>'{proveedores,nombre}' IS NULL THEN
        RAISE EXCEPTION 'unique reference was not resolved: %, %',v_ingestion,v_albaran;
    END IF;
    IF public.resolve_ingestion_reference_v1('100000') IS NOT NULL
       OR public.resolve_albaran_reference_v1('deadbe') IS NOT NULL
       OR public.resolve_ingestion_reference_v1('../bad') IS NOT NULL THEN
        RAISE EXCEPTION 'ambiguous or malformed reference did not fail closed';
    END IF;
END $$;

-- Financial history and AI costs are append-only. Replays by local UUID and by
-- provider request ID must be successful no-ops, never UPDATEs against the
-- immutability trigger.
SELECT public.append_ai_usage_event_v1(
    '{"id":"30000000-0000-0000-0000-000000000003","request_id":"ci-provider-request-3","operation":"ocr","provider":"mistral","model":"mistral-ocr-4-0","pages":1,"page_unit_price_usd":0.004,"cost_usd":0.004,"metadata":{}}'::jsonb
);
SELECT public.append_ai_usage_event_v1(
    '{"id":"30000000-0000-0000-0000-000000000003","request_id":"ci-provider-request-3","operation":"ocr","provider":"mistral","model":"mistral-ocr-4-0","pages":1,"page_unit_price_usd":0.004,"cost_usd":0.004,"metadata":{}}'::jsonb
);
SELECT public.append_ai_usage_event_v1(
    '{"id":"30000000-0000-0000-0000-000000000004","request_id":"ci-provider-request-3","operation":"ocr","provider":"mistral","model":"mistral-ocr-4-0","pages":1,"page_unit_price_usd":0.004,"cost_usd":0.004,"metadata":{}}'::jsonb
);
DO $$
BEGIN
    IF (SELECT count(*) FROM public.ai_usage_events
        WHERE request_id='ci-provider-request-3') <> 1 THEN
        RAISE EXCEPTION 'AI usage replay created a duplicate';
    END IF;
    BEGIN
        UPDATE public.ai_usage_events SET cost_usd=0 WHERE id='30000000-0000-0000-0000-000000000003';
        RAISE EXCEPTION 'AI usage event was mutable';
    EXCEPTION WHEN object_not_in_prerequisite_state THEN
        IF SQLERRM NOT LIKE '%append-only%' THEN RAISE; END IF;
    END;
END $$;

-- The dashboard contract exposes aggregates and explicitly sanitised activity,
-- never ingestion metadata, storage paths or extraction artifacts.
INSERT INTO public.audit_events (actor_type,actor_id,event_type,data)
VALUES
    ('telegram_user','owner-ci','user.feedback',
     jsonb_build_object('message','Feedback visible en dashboard','chat_id',999)),
    ('system','backup-ci','backup.completed',
     jsonb_build_object('status','complete','verified',true,'storage_included',true,
                        'path','/secret/backup/location'));
DO $$
DECLARE v_snapshot JSONB := public.dashboard_snapshot_v1();
BEGIN
    IF COALESCE((v_snapshot->>'schema_version')::integer, 0) <> 1
       OR jsonb_typeof(v_snapshot->'ingestions') <> 'object'
       OR jsonb_typeof(v_snapshot->'jobs') <> 'object'
       OR jsonb_typeof(v_snapshot#>'{reviews,by_status}') <> 'object'
       OR jsonb_typeof(v_snapshot#>'{ai_costs,by_operation_model}') <> 'array'
       OR jsonb_typeof(v_snapshot->'recent_ingestions') <> 'array'
       OR jsonb_typeof(v_snapshot->'recent_audit_events') <> 'array' THEN
        RAISE EXCEPTION 'dashboard snapshot shape is incomplete: %', v_snapshot;
    END IF;
    IF COALESCE((v_snapshot#>>'{ai_costs,month_estimated_usd}')::numeric, 0) < 0.004
       OR jsonb_array_length(v_snapshot#>'{ai_costs,by_operation_model}') < 1 THEN
        RAISE EXCEPTION 'dashboard snapshot omitted AI usage: %', v_snapshot->'ai_costs';
    END IF;
    IF v_snapshot#>>'{feedback,0,message}' <> 'Feedback visible en dashboard'
       OR v_snapshot#>>'{last_backup,event_type}' <> 'backup.completed'
       OR COALESCE((v_snapshot#>>'{last_backup,verified}')::boolean, false) IS NOT TRUE THEN
        RAISE EXCEPTION 'dashboard feedback/backup signal is incomplete: %', v_snapshot;
    END IF;
    IF v_snapshot::text LIKE '%/secret/backup/location%'
       OR v_snapshot::text LIKE '%"chat_id": 999%'
       OR v_snapshot::text LIKE '%storage_path%'
       OR v_snapshot::text LIKE '%idempotency_key%' THEN
        RAISE EXCEPTION 'dashboard leaked a forbidden field: %', v_snapshot;
    END IF;
END $$;

ROLLBACK;

SELECT 'production_contract_ok' AS result;
