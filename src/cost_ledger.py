"""Spool local durable para que una caída de Supabase no pierda gasto de IA."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import settings


def _path() -> Path:
    root = Path(settings.RUNTIME_DIR).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root / "ai-usage-spool.jsonl"


def append(row: dict[str, Any]) -> None:
    path = _path()
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        payload = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def pending() -> list[dict[str, Any]]:
    path = _path()
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("id"):
            rows.append(value)
    # Una caída durante un reintento puede repetir una fila; el UUID la hace idempotente.
    return list({str(row["id"]): row for row in rows}.values())


def clear() -> None:
    path = _path()
    if path.exists():
        path.write_text("", encoding="utf-8")
        path.chmod(0o600)
