"""
Historial conversacional en memoria por chat_id.
Permite que el motor de consultas resuelva referencias a turnos anteriores.
"""
from __future__ import annotations

from collections import deque

MAX_HISTORY_TURNS = 5  # pares pregunta+respuesta por chat

_history: dict[int, deque[dict]] = {}


def agregar_turno(chat_id: int, pregunta: str, respuesta: str) -> None:
    if chat_id not in _history:
        _history[chat_id] = deque(maxlen=MAX_HISTORY_TURNS)
    _history[chat_id].append({"pregunta": pregunta, "respuesta": respuesta})


def obtener_historial(chat_id: int) -> list[dict]:
    return list(_history.get(chat_id, []))


def limpiar_historial(chat_id: int) -> None:
    _history.pop(chat_id, None)
