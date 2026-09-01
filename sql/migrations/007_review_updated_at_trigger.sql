-- 001 reutilizó set_updated_at() para review_items, pero esa tabla se diseñó
-- con la columna española actualizado_en. Cualquier UPDATE de una revisión
-- fallaba antes de llegar a las transiciones atómicas de 006.
BEGIN;

CREATE OR REPLACE FUNCTION public.set_actualizado_en()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog AS $$
BEGIN
    NEW.actualizado_en = clock_timestamp();
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_review_items_updated_at ON public.review_items;
CREATE TRIGGER trg_review_items_updated_at
BEFORE UPDATE ON public.review_items
FOR EACH ROW EXECUTE FUNCTION public.set_actualizado_en();

REVOKE ALL ON FUNCTION public.set_actualizado_en()
    FROM PUBLIC, anon, authenticated;

INSERT INTO public.audit_events (actor_type,actor_id,event_type,data)
VALUES ('migration','007_review_updated_at_trigger','schema.review_updated_at_trigger_fixed',
        jsonb_build_object('column','actualizado_en'))
ON CONFLICT DO NOTHING;

COMMIT;
