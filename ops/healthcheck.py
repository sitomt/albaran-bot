#!/usr/bin/env python3
"""Comprobación local de vida y, opcionalmente, dependencias externas."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request


def check_process() -> None:
    """Dentro del contenedor debe existir el proceso Python del bot."""
    if os.environ.get("CONTAINERIZED") != "1":
        return
    proc = Path("/proc")
    if not proc.exists():
        raise RuntimeError("/proc no está disponible")
    for cmdline_path in proc.glob("[0-9]*/cmdline"):
        try:
            cmdline = cmdline_path.read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if "python" in cmdline.lower() and "src.bot" in cmdline:
            return
    raise RuntimeError("no se encontró el proceso python -m src.bot")


def request_json(url: str, headers: dict[str, str] | None = None) -> dict:
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(request, timeout=4) as response:
        if response.status >= 400:
            raise RuntimeError(f"HTTP {response.status}")
        payload = response.read(64 * 1024)
    return json.loads(payload or b"{}")


def check_telegram() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN ausente")
    payload = request_json(f"https://api.telegram.org/bot{token}/getMe")
    if payload.get("ok") is not True:
        raise RuntimeError("Telegram getMe devolvió ok=false")


def check_supabase() -> None:
    base_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    api_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base_url or not api_key:
        raise RuntimeError("configuración de Supabase ausente")
    headers = {"apikey": api_key, "Authorization": f"Bearer {api_key}"}
    request = urllib.request.Request(
        f"{base_url}/rest/v1/",
        headers=headers,
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=4) as response:
        if response.status >= 400:
            raise RuntimeError(f"Supabase HTTP {response.status}")
        spec = json.loads(response.read(512 * 1024))
    paths = set((spec.get("paths") or {}).keys())
    required = {
        "/ingestions", "/extraction_artifacts", "/review_items", "/audit_events",
        "/ai_usage_events", "/rpc/claim_ingestion_job_v1",
        "/rpc/confirm_albaran_v1", "/rpc/archive_albaran_v1",
        "/rpc/append_ai_usage_event_v1",
        "/rpc/accept_confirm_candidate_v1", "/rpc/reject_ingestion_v1",
        "/rpc/retry_ingestion_v1",
        "/rpc/dashboard_snapshot_v1",
        "/rpc/resolve_ingestion_reference_v1", "/rpc/resolve_albaran_reference_v1",
    }
    missing = sorted(required - paths)
    if missing:
        raise RuntimeError("contrato incompleto: " + ", ".join(missing))

    request = urllib.request.Request(
        f"{base_url}/storage/v1/bucket",
        headers={"apikey": api_key, "Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=4) as response:
        if response.status >= 400:
            raise RuntimeError(f"Supabase HTTP {response.status}")
        buckets = json.loads(response.read(64 * 1024))
    bucket_name = os.environ.get("STORAGE_BUCKET", "albaranes")
    bucket = next((item for item in buckets if item.get("name") == bucket_name), None)
    if not bucket:
        raise RuntimeError(f"bucket ausente: {bucket_name}")
    if bucket.get("public") is not False:
        raise RuntimeError(f"bucket público: {bucket_name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--deep",
        action="store_true",
        help="comprueba también Telegram y Supabase (para diagnóstico, no para liveness)",
    )
    args = parser.parse_args()

    checks = [("process", check_process)]
    if args.deep:
        checks.extend([("telegram", check_telegram), ("supabase", check_supabase)])

    failures: list[str] = []
    for name, check in checks:
        try:
            check()
        except (OSError, RuntimeError, ValueError, urllib.error.URLError) as exc:
            failures.append(f"{name}: {exc}")

    if failures:
        print("UNHEALTHY " + "; ".join(failures), file=sys.stderr)
        return 1
    print("OK " + ",".join(name for name, _ in checks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
