#!/usr/bin/env python3
"""Copia íntegra y autenticada de un bucket privado de Supabase Storage."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import sys
import urllib.parse
import urllib.request


def api_request(url: str, key: str, *, data: bytes | None = None) -> bytes:
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, headers=headers, data=data)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def safe_destination(root: Path, object_name: str) -> Path:
    parts = PurePosixPath(object_name).parts
    if not parts or object_name.startswith("/") or any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"ruta de objeto no segura: {object_name!r}")
    destination = root.joinpath(*parts)
    destination.relative_to(root)
    return destination


def list_prefix(base: str, key: str, bucket: str, prefix: str = "") -> list[str]:
    result: list[str] = []
    offset = 0
    while True:
        payload = json.dumps({"prefix": prefix, "limit": 100, "offset": offset, "sortBy": {"column": "name", "order": "asc"}}).encode()
        url = f"{base}/storage/v1/object/list/{urllib.parse.quote(bucket, safe='')}"
        items = json.loads(api_request(url, key, data=payload))
        for item in items:
            name = item.get("name")
            if not name:
                continue
            object_name = f"{prefix}/{name}" if prefix else name
            if item.get("id") is None:
                result.extend(list_prefix(base, key, bucket, object_name))
            else:
                result.append(object_name)
        if len(items) < 100:
            break
        offset += len(items)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--bucket", default="albaranes")
    args = parser.parse_args()

    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base or not key:
        parser.error("faltan SUPABASE_URL o SUPABASE_SERVICE_ROLE_KEY")

    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    objects = list_prefix(base, key, args.bucket)
    manifest: list[dict[str, object]] = []
    for index, object_name in enumerate(objects, start=1):
        destination = safe_destination(root, object_name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        encoded_path = urllib.parse.quote(object_name, safe="/")
        url = f"{base}/storage/v1/object/authenticated/{urllib.parse.quote(args.bucket, safe='')}/{encoded_path}"
        content = api_request(url, key)
        temporary = destination.with_name(destination.name + ".part")
        temporary.write_bytes(content)
        temporary.replace(destination)
        manifest.append({"path": object_name, "bytes": len(content)})
        print(f"[{index}/{len(objects)}] {object_name}")

    (root / "storage-manifest.json").write_text(
        json.dumps({"bucket": args.bucket, "count": len(manifest), "objects": manifest}, indent=2),
        encoding="utf-8",
    )
    print(f"Copiados {len(manifest)} objetos")
    return 0


if __name__ == "__main__":
    sys.exit(main())
