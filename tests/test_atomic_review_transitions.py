from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import review_service
from src import supabase_client as db
from src.config import settings


INGESTION_ID = "10000000-0000-0000-0000-000000000010"
ARTIFACT_ID = "20000000-0000-0000-0000-000000000010"


def _candidate() -> dict:
    return {
        "header": {
            "proveedor_nombre": "Proveedor", "fecha": "2026-08-01",
            "base_imponible": 10, "total_iva": 1, "total": 11,
        },
        "lines": [{
            "descripcion_limpia": "Tomate", "cantidad": 1,
            "precio_unitario": 10, "importe_neto": 10,
        }],
    }


@pytest.mark.asyncio
async def test_aprobacion_envia_version_y_payload_a_una_sola_rpc(monkeypatch):
    ingestion = {
        "id": INGESTION_ID, "idempotency_key": "telegram:owner:file",
        "metadata": {"candidate_artifact_id": ARTIFACT_ID},
    }
    monkeypatch.setattr(review_service.db, "obtener_ingestion", AsyncMock(return_value=ingestion))
    monkeypatch.setattr(
        review_service, "load_candidate", AsyncMock(return_value=(_candidate(), ARTIFACT_ID))
    )
    monkeypatch.setattr(
        review_service.db, "listar_revisiones_ingestion",
        AsyncMock(return_value=[{
            "reason_code": "human_confirmation_required", "entity_type": "document",
            "entity_key": "header", "field_name": "human_confirmation",
            "observed_value": True, "calculated_value": None,
        }]),
    )
    atomic = AsyncMock(return_value={"albaran_id": "a" * 36})
    monkeypatch.setattr(review_service.db, "aceptar_y_confirmar_candidato_atomico", atomic)

    result = await review_service.approve_all(
        INGESTION_ID, settings.allowed_users[0],
        expected_artifact_prefix=ARTIFACT_ID[:12],
    )

    assert result["albaran_id"] == "a" * 36
    atomic.assert_awaited_once()
    args = atomic.await_args.kwargs
    assert args["candidate_artifact_id"] == ARTIFACT_ID
    assert args["albaran"] == _candidate()["header"]
    assert args["lineas"] == _candidate()["lines"]


@pytest.mark.asyncio
async def test_boton_de_aprobacion_de_version_anterior_no_llama_rpc(monkeypatch):
    ingestion = {
        "id": INGESTION_ID, "idempotency_key": "telegram:owner:file",
        "metadata": {"candidate_artifact_id": ARTIFACT_ID},
    }
    monkeypatch.setattr(review_service.db, "obtener_ingestion", AsyncMock(return_value=ingestion))
    monkeypatch.setattr(
        review_service, "load_candidate", AsyncMock(return_value=(_candidate(), ARTIFACT_ID))
    )
    monkeypatch.setattr(
        review_service.db, "listar_revisiones_ingestion", AsyncMock(return_value=[])
    )
    atomic = AsyncMock()
    monkeypatch.setattr(review_service.db, "aceptar_y_confirmar_candidato_atomico", atomic)

    with pytest.raises(ValueError, match="revisión ha cambiado"):
        await review_service.approve_all(
            INGESTION_ID, settings.allowed_users[0],
            expected_artifact_prefix="ffffffffffff",
        )
    atomic.assert_not_awaited()


@pytest.mark.asyncio
async def test_rechazo_usa_cas_del_candidato_actual(monkeypatch):
    ingestion = {
        "id": INGESTION_ID, "status": "needs_review",
        "metadata": {"candidate_artifact_id": ARTIFACT_ID},
    }
    monkeypatch.setattr(review_service.db, "obtener_ingestion", AsyncMock(return_value=ingestion))
    atomic = AsyncMock(return_value={"status": "rejected"})
    monkeypatch.setattr(review_service.db, "rechazar_ingestion_atomico", atomic)

    await review_service.reject_ingestion(
        INGESTION_ID, settings.allowed_users[0], as_duplicate=True,
        expected_artifact_prefix=ARTIFACT_ID[:12],
    )

    atomic.assert_awaited_once_with(
        ingestion_id=INGESTION_ID, candidate_artifact_id=ARTIFACT_ID,
        actor_id=str(settings.allowed_users[0]), as_duplicate=True,
    )


class _RpcCall:
    def __init__(self, data=None, error: Exception | None = None):
        self.data = data
        self.error = error

    async def execute(self):
        if self.error:
            raise self.error
        return SimpleNamespace(data=self.data)


class _RpcClient:
    def __init__(self, call: _RpcCall):
        self.call = call
        self.name = None
        self.params = None

    def rpc(self, name, params):
        self.name, self.params = name, params
        return self.call


class _StaleRpcError(RuntimeError):
    code = "40001"


@pytest.mark.asyncio
async def test_reintento_delega_transicion_completa_en_rpc(monkeypatch):
    client = _RpcClient(_RpcCall({"status": "queued"}))
    monkeypatch.setattr(db, "get_client", AsyncMock(return_value=client))

    assert await db.reintentar_ingestion_fallida(INGESTION_ID, settings.allowed_users[0]) is True
    assert client.name == "retry_ingestion_v1"
    assert client.params == {
        "p_ingestion_id": INGESTION_ID,
        "p_actor_id": str(settings.allowed_users[0]),
    }


@pytest.mark.asyncio
async def test_reintento_atrasado_devuelve_false(monkeypatch):
    client = _RpcClient(_RpcCall(error=_StaleRpcError("stale")))
    monkeypatch.setattr(db, "get_client", AsyncMock(return_value=client))

    assert await db.reintentar_ingestion_fallida(INGESTION_ID, settings.allowed_users[0]) is False


@pytest.mark.asyncio
async def test_reintento_no_oculta_una_caida_de_red(monkeypatch):
    client = _RpcClient(_RpcCall(error=RuntimeError("network unavailable")))
    monkeypatch.setattr(db, "get_client", AsyncMock(return_value=client))

    with pytest.raises(RuntimeError, match="network unavailable"):
        await db.reintentar_ingestion_fallida(INGESTION_ID, settings.allowed_users[0])
