#!/bin/sh
set -eu

backup_dir=${1:-}
[ -n "$backup_dir" ] || { echo "Uso: $0 DIRECTORIO_BACKUP" >&2; exit 64; }
[ -d "$backup_dir" ] || { echo "No existe: $backup_dir" >&2; exit 66; }
[ "$(cat "$backup_dir/STATUS" 2>/dev/null || true)" = COMPLETE ] || {
  echo "El backup no está marcado COMPLETE" >&2; exit 65;
}
[ -s "$backup_dir/data/data-manifest.json" ] || { echo "Falta el manifiesto de datos" >&2; exit 65; }
[ -s "$backup_dir/storage/storage-manifest.json" ] || { echo "Falta el manifiesto de Storage" >&2; exit 65; }
(
  cd "$backup_dir"
  if command -v sha256sum >/dev/null 2>&1; then sha256sum -c SHA256SUMS;
  else shasum -a 256 -c SHA256SUMS; fi
)
echo "Backup de emergencia verificado: $backup_dir"
