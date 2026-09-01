#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "Uso: scripts/restore_emergency_api.sh DIRECTORIO_BACKUP" >&2
  echo "Requiere SUPABASE_DB_URL y un destino vacío con el baseline aplicado." >&2
  exit 64
fi

: "${SUPABASE_DB_URL:?Falta SUPABASE_DB_URL}"
command -v psql >/dev/null 2>&1 || { echo "Falta psql" >&2; exit 69; }

backup_dir=${1%/}
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(dirname "$script_dir")

"$script_dir/verify_emergency_backup.sh" "$backup_dir"
python3 "$project_root/ops/restore_data_api.py" --input "$backup_dir/data" \
  | psql "$SUPABASE_DB_URL" -X -v ON_ERROR_STOP=1

echo "Restauración de filas de emergencia completada y verificada."
