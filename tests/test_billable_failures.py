from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.ingestion_service import BillableExtractionError, _classify, _extract


class _Chat:
    def __init__(self, response): self.response = response
    async def complete_async(self, **_kwargs): return self.response


class _Client:
    def __init__(self, response): self.chat = _Chat(response)


def _response(content: str):
    return SimpleNamespace(
        id="request-1",
        usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30),
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
    )


@pytest.mark.asyncio
async def test_clasificacion_json_invalido_conserva_uso_facturable():
    result = await _classify(b"image", _Client(_response("no-json")))
    assert result.document_type == "unknown"
    assert result.raw["outcome"] == "parse_error"
    assert result.usage.input_tokens == 120
    assert result.usage.output_tokens == 30


@pytest.mark.asyncio
async def test_extraccion_json_irrecuperable_transporta_uso_para_ledger():
    with pytest.raises(BillableExtractionError) as caught:
        await _extract("ocr", _Client(_response("{definitivamente no es json")))
    assert caught.value.usage.input_tokens == 120
    assert caught.value.usage.output_tokens == 30
    assert caught.value.raw_text.startswith("{")
