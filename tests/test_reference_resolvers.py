from __future__ import annotations

from types import SimpleNamespace

import pytest

from src import supabase_client as db
from src.config import settings


class _RpcCall:
    def __init__(self, data):
        self.data = data

    async def execute(self):
        return SimpleNamespace(data=self.data)


class _Client:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def rpc(self, name, params):
        self.calls.append((name, params))
        return _RpcCall(self.data)


@pytest.mark.asyncio
async def test_ingestion_antigua_se_resuelve_por_rpc_sin_ventana(monkeypatch):
    row = {"id": "abcdef12-0000-0000-0000-000000000009", "status": "rejected"}
    client = _Client(row)

    async def get_client():
        return client

    monkeypatch.setattr(db, "get_client", get_client)
    result = await db.buscar_ingestion_por_referencia("ABCDEF12", settings.allowed_users[0])

    assert result == row
    assert client.calls == [(
        "resolve_ingestion_reference_v1", {"p_reference": "ABCDEF12"}
    )]


@pytest.mark.asyncio
async def test_ingestion_no_consulta_rpc_para_usuario_no_autorizado(monkeypatch):
    async def forbidden_client():
        raise AssertionError("no debe abrir cliente Supabase")

    monkeypatch.setattr(db, "get_client", forbidden_client)
    assert await db.buscar_ingestion_por_referencia("abcdef12", -999) is None


@pytest.mark.asyncio
async def test_albaran_usa_rpc_y_ambiguedad_devuelve_none(monkeypatch):
    client = _Client(None)

    async def get_client():
        return client

    monkeypatch.setattr(db, "get_client", get_client)
    assert await db.buscar_albaran_por_referencia("deadbe") is None
    assert client.calls == [(
        "resolve_albaran_reference_v1", {"p_reference": "deadbe"}
    )]


@pytest.mark.asyncio
async def test_referencia_malformada_falla_antes_de_la_base(monkeypatch):
    async def forbidden_client():
        raise AssertionError("no debe abrir cliente Supabase")

    monkeypatch.setattr(db, "get_client", forbidden_client)
    assert await db.buscar_albaran_por_referencia("../bad") is None
    assert await db.buscar_ingestion_por_referencia("abc", settings.allowed_users[0]) is None
