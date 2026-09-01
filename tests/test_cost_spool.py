from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src import cost_ledger
from src import supabase_client as db
from src.config import settings


class _UsageQuery:
    def __init__(self, *, fail_insert: bool = False):
        self.fail_insert = fail_insert
        self.mode = ""
        self.rows: list[dict] = []

    async def execute(self):
        if self.fail_insert:
            raise RuntimeError("database unavailable")
        return object()


class _UsageClient:
    def __init__(self, *, fail_insert: bool = False):
        self.query = _UsageQuery(fail_insert=fail_insert)

    def rpc(self, name, params):
        assert name == "append_ai_usage_event_v1"
        assert set(params) == {"p_event"}
        self.query.mode = "rpc"
        self.query.rows.append(params["p_event"])
        return self.query


@pytest.mark.asyncio
async def test_coste_no_se_pierde_si_supabase_falla_y_se_reconcilia(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RUNTIME_DIR", str(tmp_path))
    failed_client = _UsageClient(fail_insert=True)
    monkeypatch.setattr(db, "get_client", AsyncMock(return_value=failed_client))

    await db.registrar_uso_ai(
        operation="ocr", model="mistral-ocr-4-0", cost_usd=0.004,
        ingestion_id="a" * 36, user_id=123, pages=1, page_unit_price=0.004,
    )

    pending = cost_ledger.pending()
    assert len(pending) == 1
    assert pending[0]["cost_usd"] == 0.004

    healthy_client = _UsageClient()
    monkeypatch.setattr(db, "get_client", AsyncMock(return_value=healthy_client))
    await db.registrar_uso_ai(
        operation="classification", model="mistral-small-2603", cost_usd=0.00001,
        user_id=123, input_tokens=50, output_tokens=10,
    )

    assert cost_ledger.pending() == []
    assert len(healthy_client.query.rows) == 2  # evento nuevo + reconciliado


@pytest.mark.asyncio
async def test_spool_se_reconcilia_al_arrancar_sin_generar_otro_coste(tmp_path, monkeypatch):
    monkeypatch.setattr(db.settings, "RUNTIME_DIR", str(tmp_path))
    row = {
        "id": "00000000-0000-0000-0000-000000000099",
        "operation": "ocr",
        "provider": "mistral",
        "model": "mistral-ocr-4-0",
        "input_tokens": 0,
        "output_tokens": 0,
        "pages": 1,
        "retries": 0,
        "input_unit_price_usd": 0,
        "output_unit_price_usd": 0,
        "page_unit_price_usd": 0.004,
        "cost_usd": 0.004,
        "metadata": {},
        "created_at": "2026-08-07T00:00:00+00:00",
    }
    cost_ledger.append(row)
    healthy_client = _UsageClient()
    monkeypatch.setattr(db, "get_client", AsyncMock(return_value=healthy_client))

    assert await db.reconciliar_spool_costes() == 1
    assert cost_ledger.pending() == []
    assert healthy_client.query.rows == [row]


@pytest.mark.asyncio
async def test_replay_del_spool_usa_rpc_insert_only_y_se_considera_reconciliado(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(db.settings, "RUNTIME_DIR", str(tmp_path))
    row = {
        "id": "00000000-0000-0000-0000-000000000100",
        "request_id": "provider-request-already-persisted",
        "operation": "ocr", "provider": "mistral", "model": "ocr-model",
        "input_tokens": 0, "output_tokens": 0, "pages": 1, "retries": 0,
        "input_unit_price_usd": 0, "output_unit_price_usd": 0,
        "page_unit_price_usd": 0.004, "cost_usd": 0.004,
        "metadata": {}, "created_at": "2026-08-07T00:00:00+00:00",
    }
    cost_ledger.append(row)
    client = _UsageClient()
    monkeypatch.setattr(db, "get_client", AsyncMock(return_value=client))

    assert await db.reconciliar_spool_costes() == 1
    assert cost_ledger.pending() == []
    assert client.query.mode == "rpc"
    assert client.query.rows == [row]


@pytest.mark.asyncio
async def test_coste_se_guarda_si_falla_la_creacion_del_cliente(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(db, "get_client", AsyncMock(side_effect=RuntimeError("sin cliente")))

    await db.registrar_uso_ai(
        operation="ocr", model="mistral-ocr-4-0", cost_usd=0.004,
        ingestion_id="b" * 36, user_id=123, pages=1, page_unit_price=0.004,
    )

    pending = cost_ledger.pending()
    assert len(pending) == 1
    assert pending[0]["operation"] == "ocr"
