#!/bin/sh
set -eu
umask 077

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(dirname "$script_dir")
output_root=${1:-"$project_root/backups"}

: "${SUPABASE_URL:?Falta SUPABASE_URL}"
: "${SUPABASE_SERVICE_ROLE_KEY:?Falta SUPABASE_SERVICE_ROLE_KEY}"

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_dir="${output_root%/}/emergency-$timestamp"
mkdir -p "$backup_dir"
printf '%s\n' INCOMPLETE > "$backup_dir/STATUS"

python3 "$project_root/ops/backup_data_api.py" --output "$backup_dir/data"
python3 "$project_root/ops/backup_storage.py" \
  --output "$backup_dir/storage" --bucket "${STORAGE_BUCKET:-albaranes}"

{
  printf 'created_at_utc=%s\n' "$timestamp"
  printf 'database_format=supabase-postgrest-emergency-v1\n'
  printf 'storage_included=true\n'
  printf 'not_a_pg_dump=true\n'
} > "$backup_dir/metadata.txt"

(
  cd "$backup_dir"
  if command -v sha256sum >/dev/null 2>&1; then
    find . -type f ! -name SHA256SUMS ! -name STATUS -exec sha256sum {} \; | sort > SHA256SUMS
  else
    find . -type f ! -name SHA256SUMS ! -name STATUS -exec shasum -a 256 {} \; | sort > SHA256SUMS
  fi
)
printf '%s\n' COMPLETE > "$backup_dir/STATUS"
chmod -R go-rwx "$backup_dir"
echo "Backup de emergencia completo: $backup_dir"
