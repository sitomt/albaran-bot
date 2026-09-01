#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "Uso: scripts/verify_backup.sh DIRECTORIO_BACKUP" >&2
  exit 64
fi

backup_dir=${1%/}
[ -d "$backup_dir" ] || { echo "No existe el directorio: $backup_dir" >&2; exit 66; }
[ "$(cat "$backup_dir/STATUS" 2>/dev/null || true)" = "COMPLETE" ] || {
  echo "La copia no está marcada como COMPLETE" >&2
  exit 65
}
[ -s "$backup_dir/database.dump" ] || { echo "Falta database.dump" >&2; exit 65; }
[ -s "$backup_dir/SHA256SUMS" ] || { echo "Falta SHA256SUMS" >&2; exit 65; }
command -v pg_restore >/dev/null 2>&1 || { echo "Falta pg_restore" >&2; exit 69; }

(
  cd "$backup_dir"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum --check SHA256SUMS
  else
    shasum -a 256 --check SHA256SUMS
  fi
)

toc_file=$(mktemp "${TMPDIR:-/tmp}/albaran-toc.XXXXXX")
trap 'rm -f "$toc_file"' EXIT HUP INT TERM
pg_restore --list "$backup_dir/database.dump" > "$toc_file"

for table_name in proveedores productos_catalogo albaranes lineas_albaran auditoria jobs ingestions extraction_artifacts review_items audit_events ai_usage_events; do
  if ! grep -Eq " TABLE (public )?${table_name} " "$toc_file"; then
    echo "La copia no contiene la tabla esperada: $table_name" >&2
    exit 65
  fi
done

echo "OK: checksums, formato y tablas esenciales verificados"
