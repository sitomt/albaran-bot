import asyncio
import io
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from src import intake_service as intake


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def durable_db(monkeypatch):
    mocks = {}
    defaults = {
        "buscar_ingestion_exacta": None,
        "contar_ingestions_abiertas": 0,
        "subir_original_privado": None,
        "crear_ingestion_durable": {"id": "created"},
        "actualizar_ingestion": {},
        "crear_job_ingestion": {"id": "job"},
        "registrar_evento_auditoria": None,
        "borrar_original_privado": None,
    }
    for name, result in defaults.items():
        mocks[name] = AsyncMock(return_value=result)
        monkeypatch.setattr(intake.db, name, mocks[name])
    return mocks


@pytest.mark.asyncio
async def test_original_se_guarda_antes_de_confirmar_recepcion(durable_db):
    result = await intake.receive_image(
        data=_png(), telegram_user_id=10, telegram_chat_id=20, file_unique_id="file-1"
    )
    assert not result.duplicate
    assert durable_db["subir_original_privado"].await_count == 1
    assert durable_db["crear_ingestion_durable"].await_count == 1
    assert durable_db["crear_job_ingestion"].await_count == 1
    assert durable_db["registrar_evento_auditoria"].await_count == 1


@pytest.mark.asyncio
async def test_reenvio_exacto_no_vuelve_a_subir(durable_db):
    durable_db["buscar_ingestion_exacta"].return_value = {
        "id": "11111111-1111-1111-1111-111111111111", "status": "needs_review"
    }
    result = await intake.receive_image(
        data=_png(), telegram_user_id=10, telegram_chat_id=20, file_unique_id="file-1"
    )
    assert result.duplicate
    durable_db["subir_original_privado"].assert_not_awaited()
    durable_db["crear_job_ingestion"].assert_not_awaited()


@pytest.mark.asyncio
async def test_fallo_al_crear_job_conserva_original_y_marca_ingesta(durable_db):
    durable_db["crear_job_ingestion"].side_effect = RuntimeError("database unavailable")
    with pytest.raises(RuntimeError):
        await intake.receive_image(
            data=_png(), telegram_user_id=10, telegram_chat_id=20, file_unique_id="file-1"
        )
    durable_db["borrar_original_privado"].assert_not_awaited()
    assert any(
        call.kwargs.get("status") == "failed"
        for call in durable_db["actualizar_ingestion"].await_args_list
    )


@pytest.mark.asyncio
async def test_fallo_antes_de_fila_durable_retira_objeto_huerfano(durable_db):
    durable_db["crear_ingestion_durable"].side_effect = RuntimeError("insert failed")
    with pytest.raises(RuntimeError):
        await intake.receive_image(
            data=_png(), telegram_user_id=10, telegram_chat_id=20, file_unique_id="file-1"
        )
    durable_db["borrar_original_privado"].assert_awaited_once()


@pytest.mark.asyncio
async def test_carrera_de_duplicado_devuelve_la_ingesta_ganadora(durable_db):
    winner = {
        "id": "22222222-2222-2222-2222-222222222222", "status": "queued"
    }
    durable_db["buscar_ingestion_exacta"].side_effect = [None, None, winner]
    durable_db["crear_ingestion_durable"].side_effect = RuntimeError("unique violation")
    result = await intake.receive_image(
        data=_png(), telegram_user_id=10, telegram_chat_id=20, file_unique_id="file-2"
    )
    assert result.duplicate
    assert result.ingestion_id == winner["id"]
    durable_db["borrar_original_privado"].assert_awaited_once()


@pytest.mark.asyncio
async def test_ingesta_masiva_se_frena_por_usuario_antes_de_subir(durable_db, monkeypatch):
    monkeypatch.setattr(intake.settings, "MAX_PENDING_PER_USER", 3)
    monkeypatch.setattr(intake.settings, "MAX_PENDING_GLOBAL", 10)
    durable_db["contar_ingestions_abiertas"].side_effect = [3, 3]

    with pytest.raises(ValueError, match="3 documentos pendientes"):
        await intake.receive_image(
            data=_png(), telegram_user_id=10, telegram_chat_id=20, file_unique_id="mass-1"
        )

    durable_db["subir_original_privado"].assert_not_awaited()


@pytest.mark.asyncio
async def test_ingesta_masiva_se_frena_globalmente_antes_de_subir(durable_db, monkeypatch):
    monkeypatch.setattr(intake.settings, "MAX_PENDING_PER_USER", 3)
    monkeypatch.setattr(intake.settings, "MAX_PENDING_GLOBAL", 4)
    durable_db["contar_ingestions_abiertas"].side_effect = [1, 4]

    with pytest.raises(ValueError, match="cola está completa"):
        await intake.receive_image(
            data=_png(), telegram_user_id=10, telegram_chat_id=20, file_unique_id="mass-2"
        )

    durable_db["subir_original_privado"].assert_not_awaited()


@pytest.mark.asyncio
async def test_rafaga_concurrente_respeta_el_limite_sin_subir_excedentes(
    durable_db, monkeypatch
):
    monkeypatch.setattr(intake.settings, "MAX_PENDING_PER_USER", 3)
    monkeypatch.setattr(intake.settings, "MAX_PENDING_GLOBAL", 10)
    state = {"pending": 0}

    async def count_open(user_id=None):
        return state["pending"]

    async def create_durable(**_kwargs):
        state["pending"] += 1
        return {"id": "created"}

    durable_db["contar_ingestions_abiertas"].side_effect = count_open
    durable_db["crear_ingestion_durable"].side_effect = create_durable

    results = await asyncio.gather(*(
        intake.receive_image(
            data=_png(), telegram_user_id=10, telegram_chat_id=20,
            file_unique_id=f"burst-{index}",
        )
        for index in range(8)
    ), return_exceptions=True)

    accepted = [result for result in results if isinstance(result, intake.IntakeResult)]
    rejected = [result for result in results if isinstance(result, ValueError)]
    assert len(accepted) == 3
    assert len(rejected) == 5
    assert state["pending"] == 3
    assert durable_db["subir_original_privado"].await_count == 3


def test_hash_perceptual_tolera_cambio_pequeno_pero_no_es_identidad():
    original = int(intake.perceptual_hash(_png()), 16)
    recompressed = f"{original ^ 0b10101:016x}"
    assert (original ^ int(recompressed, 16)).bit_count() == 3


def test_hash_perceptual_es_invariante_a_rotacion():
    image = Image.new("RGB", (40, 20), "white")
    for x in range(5, 15):
        for y in range(3, 10):
            image.putpixel((x, y), (0, 0, 0))
    first = io.BytesIO()
    second = io.BytesIO()
    image.save(first, format="PNG")
    image.rotate(90, expand=True).save(second, format="PNG")
    assert intake.perceptual_hash(first.getvalue()) == intake.perceptual_hash(second.getvalue())
