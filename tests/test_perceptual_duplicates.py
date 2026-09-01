from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import supabase_client as db
from src import ingestion_service


class _Query:
    def __init__(self, rows): self.rows = rows
    def select(self, *_args, **_kwargs): return self
    def eq(self, *_args, **_kwargs): return self
    def in_(self, *_args, **_kwargs): return self
    def neq(self, *_args, **_kwargs): return self
    def order(self, *_args, **_kwargs): return self
    def limit(self, *_args, **_kwargs): return self
    async def execute(self): return SimpleNamespace(data=self.rows)


class _Client:
    def __init__(self, rows): self.rows = rows
    def table(self, name):
        assert name == "ingestions"
        return _Query(self.rows)


@pytest.mark.asyncio
async def test_similitud_perceptual_elige_mas_cercano_sin_autodescartar(monkeypatch):
    rows = [
        {"id": "far", "perceptual_hash": "00000000000000ff", "received_at": "2026-01-01"},
        {"id": "near", "perceptual_hash": "0000000000000003", "received_at": "2026-01-02"},
    ]
    async def client(): return _Client(rows)
    monkeypatch.setattr(db, "get_client", client)

    match = await db.buscar_ingestion_similar_perceptual(
        "0000000000000001", exclude_ingestion_id="current", max_distance=5
    )

    assert match["id"] == "near"
    assert match["perceptual_distance"] == 1


@pytest.mark.asyncio
async def test_similitud_perceptual_fuera_de_umbral_no_marca(monkeypatch):
    async def client():
        return _Client([{"id": "other", "perceptual_hash": "ffffffffffffffff"}])
    monkeypatch.setattr(db, "get_client", client)
    assert await db.buscar_ingestion_similar_perceptual(
        "0000000000000000", exclude_ingestion_id="current", max_distance=5
    ) is None


@pytest.mark.asyncio
async def test_hash_visual_funciona_aunque_ocr_no_leyera_cabecera(monkeypatch):
    visual = {
        "id": "previous", "metadata": {"provider": "P", "number": "A1"},
        "perceptual_distance": 2,
    }
    lookup = AsyncMock(return_value=visual)
    monkeypatch.setattr(ingestion_service.db, "buscar_ingestion_similar_perceptual", lookup)

    result = await ingestion_service._find_probable_duplicate(
        {"header": {"proveedor_nombre": None, "fecha": None, "total": None}},
        perceptual_hash="0000000000000001", exclude_ingestion_id="current",
    )

    assert result["match_type"] == "perceptual_hash"
    assert result["id"] == "previous"
