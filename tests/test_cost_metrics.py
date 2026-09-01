from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src import bot
from src import supabase_client as db


class _Query:
    def __init__(self, rows, ranges):
        self.rows = rows
        self.ranges = ranges
        self.start = 0
        self.end = len(rows) - 1
        self.with_count = False

    def select(self, *_args, **kwargs):
        self.with_count = kwargs.get("count") == "exact"
        return self
    def gte(self, *_args, **_kwargs): return self
    def order(self, *_args, **_kwargs): return self
    def limit(self, *_args, **_kwargs): return self
    def range(self, start, end):
        self.start, self.end = start, end
        self.ranges.append((start, end))
        return self

    async def execute(self):
        return SimpleNamespace(
            data=self.rows[self.start:self.end + 1],
            count=len(self.rows) if self.with_count else None,
        )


class _Client:
    def __init__(self, rows):
        self.rows = rows
        self.ranges = []

    def table(self, name):
        assert name == "ai_usage_events"
        return _Query(self.rows, self.ranges)


@pytest.mark.asyncio
async def test_desglose_suma_operaciones_y_mantiene_modelos(monkeypatch):
    monkeypatch.setattr(db.cost_ledger, "pending", lambda: [])
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {"id": "1", "ingestion_id": "doc-1", "operation": "ocr", "provider": "mistral",
         "model": "ocr-model", "input_tokens": 0, "output_tokens": 0, "pages": 2,
         "retries": 0, "cost_usd": 0.008, "metadata": {}, "created_at": now},
        {"id": "2", "ingestion_id": "doc-1", "operation": "extraction", "provider": "mistral",
         "model": "llm-model", "input_tokens": 1000, "output_tokens": 200, "pages": 0,
         "retries": 1, "cost_usd": 0.00027, "metadata": {"outcome": "parse_error"}, "created_at": now},
        {"id": "3", "ingestion_id": None, "operation": "query_response", "provider": "mistral",
         "model": "llm-model", "input_tokens": 500, "output_tokens": 100, "pages": 0,
         "retries": 0, "cost_usd": 0.000135, "metadata": {}, "created_at": now},
    ]

    async def fake_client():
        return _Client(rows)

    monkeypatch.setattr(db, "get_client", fake_client)
    monkeypatch.setattr(db.settings, "HOSTING_MONTHLY_COST_USD", 10.0)
    monkeypatch.setattr(db.settings, "SUPABASE_MONTHLY_COST_USD", 5.0)
    report = await db.desglose_costes_ai()

    assert report["ai_month_usd"] == pytest.approx(0.008405)
    assert report["fixed_monthly_usd"] == 15
    assert report["documents_with_usage"] == 1
    assert {item["operation"] for item in report["breakdown"]} == {
        "ocr", "extraction", "query_response"
    }
    ocr = next(item for item in report["breakdown"] if item["operation"] == "ocr")
    assert ocr["pages"] == 2
    assert ocr["calls_today"] == 1
    extraction = next(item for item in report["breakdown"] if item["operation"] == "extraction")
    assert extraction["retries"] == 1
    assert extraction["invalid_responses"] == 1


@pytest.mark.asyncio
async def test_desglose_no_duplica_evento_reconciliado_que_sigue_en_spool(monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "id": "same-event", "ingestion_id": "doc-1", "user_id": 123,
        "operation": "ocr", "provider": "mistral", "model": "ocr-model",
        "input_tokens": 0, "output_tokens": 0, "pages": 1, "retries": 0,
        "cost_usd": 0.004, "metadata": {}, "created_at": now,
    }

    async def fake_client():
        return _Client([row])

    monkeypatch.setattr(db, "get_client", fake_client)
    monkeypatch.setattr(db.cost_ledger, "pending", lambda: [row.copy()])
    report = await db.desglose_costes_ai()

    assert report["ai_month_usd"] == pytest.approx(0.004)
    assert report["breakdown"][0]["calls"] == 1


@pytest.mark.asyncio
async def test_costes_paginan_mas_de_diez_mil_eventos_sin_truncar(monkeypatch):
    monkeypatch.setattr(db.cost_ledger, "pending", lambda: [])
    now = datetime.now(timezone.utc).isoformat()
    rows = [{
        "id": f"event-{index:05d}", "ingestion_id": f"doc-{index:05d}",
        "user_id": 123, "operation": "ocr", "provider": "mistral",
        "model": "ocr-model", "input_tokens": 0, "output_tokens": 0,
        "pages": 1, "retries": 0, "cost_usd": 0.0001,
        "metadata": {}, "created_at": now,
    } for index in range(10_005)]
    client = _Client(rows)

    async def fake_client():
        return client

    monkeypatch.setattr(db, "get_client", fake_client)

    month_cost = await db.coste_ai_mes_actual()
    report = await db.desglose_costes_ai()

    assert month_cost == pytest.approx(1.0005)
    assert report["ai_month_usd"] == pytest.approx(1.0005)
    assert report["breakdown"][0]["calls"] == 10_005
    assert report["documents_with_usage"] == 10_005
    assert (10_000, 10_999) in client.ranges


def test_informe_muestra_variable_fijo_modelo_tokens_y_ultimas_llamadas():
    report = {
        "as_of": "2026-08-07T12:34:56+02:00", "ai_today_usd": 0.01,
        "ai_month_usd": 0.05, "ai_budget_usd": 25.0, "budget_pct": 0.2,
        "fixed_accrued_usd": 2.0, "total_accrued_usd": 2.05,
        "projected_committed_usd": 15.05, "fixed_monthly_usd": 15.0,
        "total_run_rate_projection_usd": 15.50,
        "documents_with_usage": 2, "average_ai_per_document_usd": 0.025,
        "fixed": {"hosting": 10.0, "supabase": 5.0, "other": 0.0},
        "breakdown": [{
            "operation": "extraction", "model": "modelo-x", "cost_month_usd": 0.04,
            "calls": 2, "calls_today": 1, "pages": 0,
            "average_cost_usd": 0.02,
            "input_tokens": 1200, "output_tokens": 300,
            "retries": 1, "invalid_responses": 1,
        }],
        "latest": [{
            "operation": "extraction", "created_at": "2026-08-07T12:30:00+02:00",
            "cost_usd": 0.02,
        }],
    }
    text = bot._format_cost_report(report)
    assert "Extracción del albarán [modelo-x]" in text
    assert "1,200 in / 300 out tok" in text
    assert "1 llamadas de reintento" in text
    assert "1 respuestas inválidas facturadas" in text
    assert "Hosting" in text and "Supabase" in text
    assert "Últimas llamadas" in text
