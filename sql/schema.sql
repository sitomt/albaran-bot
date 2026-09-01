-- Albaran Bot - bootstrap reproducible de PostgreSQL/Supabase.
--
-- Este archivo es un punto de entrada para `psql`; la fuente de verdad son las
-- migraciones versionadas. `ON_ERROR_STOP` evita una instalación parcial.
-- Desde la raíz del repositorio:
--
--   psql "$DATABASE_URL" -f sql/schema.sql
--
-- En despliegues automatizados, configura el runner para aplicar en orden
-- `sql/migrations/*.sql`; no copies a mano sentencias aisladas al SQL Editor.
\set ON_ERROR_STOP on
\ir migrations/000_legacy_baseline.sql
\ir migrations/001_production_core.sql
\ir migrations/002_manual_ingestions.sql
\ir migrations/003_ai_usage_events.sql
\ir migrations/004_safe_archival.sql
\ir migrations/005_safe_ai_usage_append.sql
\ir migrations/006_atomic_review_transitions.sql
\ir migrations/007_review_updated_at_trigger.sql
\ir migrations/008_dashboard_snapshot.sql
\ir migrations/009_reference_resolvers.sql
