from pathlib import Path

import pytest

from src.config import Settings


ROOT = Path(__file__).resolve().parent.parent


def test_no_existe_rpc_de_sql_dinamico_en_codigo_de_aplicacion():
    source = "\n".join(path.read_text() for path in (ROOT / "src").glob("*.py"))
    assert "execute_select" not in source
    assert "ejecutar_sql" not in source


def test_migracion_elimina_rpc_y_no_hace_publico_el_bucket():
    migration = (ROOT / "sql/migrations/001_production_core.sql").read_text()
    assert "DROP FUNCTION IF EXISTS public.execute_select(text)" in migration
    assert "VALUES ('albaranes','albaranes',false)" in migration
    assert "FROM PUBLIC, anon, authenticated" in migration


def test_duplicado_de_proveedor_fecha_total_no_es_constraint_unico():
    migration = (ROOT / "sql/migrations/001_production_core.sql").read_text()
    assert "DROP INDEX IF EXISTS public.idx_albaran_duplicado" in migration
    assert "UNIQUE(proveedor_id, fecha, total)" not in migration


def test_publicacion_atomica_bloquea_revisiones_abiertas():
    migration = (ROOT / "sql/migrations/001_production_core.sql").read_text()
    assert "open review items must be resolved first" in migration
    assert "sum of lines does not reconcile with taxable base" in migration
    assert "base + VAT does not reconcile with total" in migration


def test_eventos_de_migracion_no_se_duplican_al_reintentar():
    core = (ROOT / "sql/migrations/001_production_core.sql").read_text()
    assert "uq_audit_migration_event_once" in core
    for name in (
        "002_manual_ingestions.sql", "003_ai_usage_events.sql",
        "004_safe_archival.sql", "005_safe_ai_usage_append.sql",
        "006_atomic_review_transitions.sql",
        "007_review_updated_at_trigger.sql",
        "008_dashboard_snapshot.sql",
        "009_reference_resolvers.sql",
    ):
        migration = (ROOT / "sql/migrations" / name).read_text()
        assert "ON CONFLICT DO NOTHING" in migration


def test_reconciliacion_costes_es_insert_only_y_backend_only():
    migration = (ROOT / "sql/migrations/005_safe_ai_usage_append.sql").read_text()
    assert "ON CONFLICT DO NOTHING" in migration
    assert "ON CONFLICT DO UPDATE" not in migration.upper()
    assert "FROM PUBLIC, anon, authenticated" in migration
    assert "TO service_role" in migration
    assert "UPDATE public.ai_usage_events" not in migration


def test_transiciones_humanas_son_atomicas_y_versionadas():
    migration = (ROOT / "sql/migrations/006_atomic_review_transitions.sql").read_text()
    assert "candidate_artifact_id" in migration
    assert "stale review: candidate version changed" in migration
    assert "stale rejection: ingestion is no longer rejectable" in migration
    assert "retry_ingestion_v1" in migration
    assert "FOR UPDATE" in migration
    assert "FROM PUBLIC, anon, authenticated" in migration


def test_restore_desde_baseline_reemplaza_trigger_incompatible_de_revisiones():
    schema = (ROOT / "sql/schema.sql").read_text()
    migration = (ROOT / "sql/migrations/007_review_updated_at_trigger.sql").read_text()
    contract = (ROOT / "sql/tests/production_contract.sql").read_text()

    assert schema.index("001_production_core.sql") < schema.index(
        "007_review_updated_at_trigger.sql"
    )
    assert "NEW.actualizado_en = clock_timestamp()" in migration
    assert "DROP TRIGGER IF EXISTS trg_review_items_updated_at" in migration
    assert "EXECUTE FUNCTION public.set_actualizado_en()" in migration
    assert "review trigger regression probe" in contract


def test_correccion_posterior_archiva_sin_borrar_historia():
    migration = (ROOT / "sql/migrations/004_safe_archival.sql").read_text()
    assert "SET status='archived', version=version+1" in migration
    assert "albaran.archived" in migration
    assert "DELETE FROM PUBLIC.ALBARANES" not in migration.upper()


def test_dashboard_snapshot_es_agregado_y_solo_backend():
    migration = (ROOT / "sql/migrations/008_dashboard_snapshot.sql").read_text()
    contract = (ROOT / "sql/tests/production_contract.sql").read_text()

    assert "SECURITY DEFINER" in migration
    assert "FROM PUBLIC, anon, authenticated" in migration
    assert "TO service_role" in migration
    assert "public.extraction_artifacts" not in migration
    assert "storage_path" not in migration
    assert "idempotency_key" not in migration
    assert "estimated_from_configured_unit_rates" in migration
    assert "client role can execute dashboard snapshot" in contract
    assert "/secret/backup/location" in contract


def test_resolutores_de_referencias_son_globales_unicos_y_privados():
    migration = (ROOT / "sql/migrations/009_reference_resolvers.sql").read_text()
    contract = (ROOT / "sql/tests/production_contract.sql").read_text()

    assert "resolve_ingestion_reference_v1" in migration
    assert "resolve_albaran_reference_v1" in migration
    assert "v_count = 1" in migration
    assert "EXECUTE" not in migration.split("AS $$", 1)[1].split("END $$", 1)[0]
    assert "FROM PUBLIC, anon, authenticated" in migration
    assert "TO service_role" in migration
    assert "client role can resolve private references" in contract
    assert "ambiguous or malformed reference did not fail closed" in contract


def test_produccion_prohibe_autoconfirmacion_silenciosa(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AUTO_CONFIRM_CLEAN", "true")
    monkeypatch.setenv("MISTRAL_API_KEY", "test")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "1")
    with pytest.raises(ValueError, match="AUTO_CONFIRM_CLEAN"):
        Settings(_env_file=None)
