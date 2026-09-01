#!/usr/bin/env python3
"""Restaura un backup de Storage únicamente sobre un bucket vacío."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
from pathlib import Path
import sys
import urllib.parse
import urllib.request

from backup_storage import list_prefix, safe_destination


def upload(base: str, key: str, bucket: str, object_name: str, source: Path) -> None:
    encoded_path = urllib.parse.quote(object_name, safe="/")
    url = f"{base}/storage/v1/object/{urllib.parse.quote(bucket, safe='')}/{encoded_path}"
    content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    request = urllib.request.Request(
        url,
        data=source.read_bytes(),
        method="POST",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": content_type,
            "x-upsert": "false",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        response.read(1024)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--bucket", default="albaranes")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    root = args.source.resolve()
    manifest_path = root / "storage-manifest.json"
    if not manifest_path.is_file():
        parser.error(f"no existe {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest.get("objects")
    if not isinstance(entries, list):
        parser.error("manifest inválido: falta objects")

    files: list[tuple[str, Path]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            parser.error("manifest inválido: entrada sin path")
        object_name = entry["path"]
        source = safe_destination(root, object_name)
        if not source.is_file() or source.stat().st_size != entry.get("bytes"):
            parser.error(f"fichero ausente o tamaño incorrecto: {object_name}")
        files.append((object_name, source))

    print(f"Backup local válido: {len(files)} objetos")
    if not args.execute:
        print("DRY RUN: no se ha subido nada. Añada --execute para restaurar.")
        return 0

    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base or not key:
        parser.error("faltan SUPABASE_URL o SUPABASE_SERVICE_ROLE_KEY del destino")

    existing = list_prefix(base, key, args.bucket)
    if existing:
        parser.error(
            f"el bucket de destino no está vacío ({len(existing)} objetos); "
            "se bloquea la restauración para no sobrescribir datos"
        )

    for index, (object_name, source) in enumerate(files, start=1):
        upload(base, key, args.bucket, object_name, source)
        print(f"[{index}/{len(files)}] {object_name}")

    restored = set(list_prefix(base, key, args.bucket))
    expected = {name for name, _ in files}
    if restored != expected:
        missing = len(expected - restored)
        extra = len(restored - expected)
        raise RuntimeError(f"verificación remota fallida: faltan={missing}, sobran={extra}")
    print("OK: Storage restaurado y paths remotos verificados")
    return 0


if __name__ == "__main__":
    sys.exit(main())
