"""Un token de Telegram solo admite un consumidor de getUpdates."""
from __future__ import annotations

import multiprocessing as mp
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import bot


def _adquirir_en_subproceso(runtime_dir: str, salida) -> None:
    """El cerrojo es por proceso: comprobarlo dentro del mismo no prueba nada."""
    from src import bot as bot_hijo

    bot_hijo.settings.RUNTIME_DIR = runtime_dir
    try:
        bot_hijo._adquirir_cerrojo_de_instancia()
        salida.put("adquirido")
    except SystemExit as exc:
        salida.put(f"rechazado:{exc.code}")


def test_segunda_instancia_no_arranca(tmp_path, monkeypatch):
    monkeypatch.setattr(bot.settings, "RUNTIME_DIR", str(tmp_path))
    bot._adquirir_cerrojo_de_instancia()

    assert (tmp_path / "bot.lock").read_text().strip() == str(os.getpid())

    contexto = mp.get_context("spawn")
    salida = contexto.Queue()
    segundo = contexto.Process(target=_adquirir_en_subproceso, args=(str(tmp_path), salida))
    segundo.start()
    segundo.join(timeout=30)

    assert salida.get(timeout=5) == "rechazado:69"


def test_cerrojo_se_libera_al_morir_el_proceso(tmp_path):
    contexto = mp.get_context("spawn")
    salida = contexto.Queue()
    primero = contexto.Process(target=_adquirir_en_subproceso, args=(str(tmp_path), salida))
    primero.start()
    primero.join(timeout=30)
    assert salida.get(timeout=5) == "adquirido"

    # El sistema operativo suelta el cerrojo aunque nadie lo libere a mano.
    segundo = contexto.Process(target=_adquirir_en_subproceso, args=(str(tmp_path), salida))
    segundo.start()
    segundo.join(timeout=30)
    assert salida.get(timeout=5) == "adquirido"


@pytest.mark.asyncio
async def test_error_no_capturado_avisa_a_quien_envio_el_mensaje(monkeypatch):
    monkeypatch.setattr(bot.settings, "TELEGRAM_ADMIN_CHAT_ID", "")
    enviar = AsyncMock()
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=555))
    monkeypatch.setattr(bot, "Update", SimpleNamespace)  # el update real es un tipo cerrado
    contexto = SimpleNamespace(error=RuntimeError("boom"), bot=SimpleNamespace(send_message=enviar))

    await bot.on_error(update, contexto)

    assert enviar.await_args.kwargs["chat_id"] == 555
    assert "sigue guardado" in enviar.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_fallo_al_avisar_no_tumba_el_handler(monkeypatch):
    monkeypatch.setattr(bot.settings, "TELEGRAM_ADMIN_CHAT_ID", "")
    monkeypatch.setattr(bot, "Update", SimpleNamespace)
    contexto = SimpleNamespace(
        error=RuntimeError("boom"),
        bot=SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("telegram caído"))),
    )

    # No debe propagar: sería un error dentro del manejador de errores.
    await bot.on_error(SimpleNamespace(effective_chat=SimpleNamespace(id=555)), contexto)


@pytest.mark.asyncio
async def test_sin_chat_no_intenta_responder(monkeypatch):
    monkeypatch.setattr(bot.settings, "TELEGRAM_ADMIN_CHAT_ID", "")
    enviar = AsyncMock()
    contexto = SimpleNamespace(error=RuntimeError("boom"), bot=SimpleNamespace(send_message=enviar))

    await bot.on_error(object(), contexto)

    enviar.assert_not_awaited()
