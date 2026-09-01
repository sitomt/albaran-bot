#!/usr/bin/env python3
"""Exportación de emergencia de las tablas públicas mediante PostgREST.

No sustituye a pg_dump: conserva filas y el contrato OpenAPI, pero no roles,
triggers, índices ni funciones. Sirve para recuperar el contenido cuando todavía
no está disponible la contraseña PostgreSQL.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import urllib.parse
import urllib.request


PAGE_SIZE = 500


def request_json(url: str, key: str, *, range_header: str | None = None) -> tuple[object, dict[str, str]]:
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }
    if range_header:
        headers["Range-Unit"] = "items"
        headers["Range"] = range_header
        headers["Prefer"] = "count=exact"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.loads(response.read())
        return body, {key.lower(): value for key, value in response.headers.items()}


def discover_tables(base: str, key: str) -> tuple[dict, list[str]]:
    spec, _ = request_json(f"{base}/rest/v1/", key)
    if not isinstance(spec, dict):
        raise RuntimeError("PostgREST no devolvió un contrato OpenAPI")
    paths = spec.get("paths") or {}
    tables = sorted(
        path.removeprefix("/")
        for path in paths
        if path != "/" and path.startswith("/") and path.count("/") == 1
        and not path.startswith("/rpc/")
    )
    return spec, tables


def export_table(base: str, key: str, table: str) -> list[dict]:
    rows: list[dict] = []
    encoded = urllib.parse.quote(table, safe="")
    offset = 0
    while True:
        page, _ = request_json(
            f"{base}/rest/v1/{encoded}?select=*",
            key,
            range_header=f"{offset}-{offset + PAGE_SIZE - 1}",
        )
        if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
            raise RuntimeError(f"respuesta inesperada al exportar {table}")
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
        offset += len(page)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base or not key:
        parser.error("faltan SUPABASE_URL o SUPABASE_SERVICE_ROLE_KEY")

    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    spec, tables = discover_tables(base, key)
    (root / "postgrest-openapi.json").write_text(
        json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    manifest: list[dict[str, object]] = []
    for table in tables:
        rows = export_table(base, key, table)
        destination = root / f"{table}.json"
        destination.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        manifest.append({"table": table, "rows": len(rows), "file": destination.name})
        print(f"{table}: {len(rows)} filas")

    (root / "data-manifest.json").write_text(
        json.dumps(
            {
                "format": "supabase-postgrest-emergency-v1",
                "warning": "No sustituye a pg_dump; no contiene roles, triggers, indices ni funciones.",
                "tables": manifest,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
