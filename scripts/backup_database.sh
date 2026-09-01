#!/bin/sh
set -eu
umask 077

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(dirname "$script_dir")

usage() {
  cat <<'EOF'
Uso: scripts/backup_database.sh [--output-dir DIRECTORIO] [--skip-storage]

Variables requeridas:
  SUPABASE_DB_URL              URL directa de PostgreSQL con sslmode=require

Para copiar también los originales del bucket privado:
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY    solo en la máquina de backup, nunca en el bot
  STORAGE_BUCKET               opcional; por defecto: albaranes
EOF
}

output_dir="${BACKUP_ROOT:-./backups}"
skip_storage=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --output-dir)
      [ "$#" -ge 2 ] || { usage >&2; exit 64; }
      output_dir=$2
      shift 2
      ;;
    --skip-storage)
      skip_storage=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Argumento desconocido: $1" >&2
      usage >&2
      exit 64
      ;;
  esac
done

: "${SUPABASE_DB_URL:?Falta SUPABASE_DB_URL}"
command -v pg_dump >/dev/null 2>&1 || { echo "Falta pg_dump" >&2; exit 69; }
command -v pg_restore >/dev/null 2>&1 || { echo "Falta pg_restore" >&2; exit 69; }
command -v psql >/dev/null 2>&1 || { echo "Falta psql" >&2; exit 69; }
# Obliga TLS incluso si la URI se copió desde el panel sin query string.
export PGSSLMODE="${PGSSLMODE:-require}"

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_dir="${output_dir%/}/$timestamp"
if [ -e "$backup_dir" ]; then
  echo "El destino ya existe: $backup_dir" >&2
  exit 73
fi
mkdir -p "$backup_dir"
printf '%s\n' "INCOMPLETE" > "$backup_dir/STATUS"

echo "Creando copia lógica PostgreSQL en $backup_dir/database.dump"
pg_dump \
  --dbname="$SUPABASE_DB_URL" \
  --format=custom \
  --compress=9 \
  --schema=public \
  --no-owner \
  --no-privileges \
  --file="$backup_dir/database.dump"
pg_restore --list "$backup_dir/database.dump" >/dev/null

if [ "$skip_storage" = false ]; then
  if [ -n "${SUPABASE_URL:-}" ] && [ -n "${SUPABASE_SERVICE_ROLE_KEY:-}" ]; then
    echo "Copiando objetos del bucket ${STORAGE_BUCKET:-albaranes}"
    python3 "$project_root/ops/backup_storage.py" \
      --output "$backup_dir/storage" \
      --bucket "${STORAGE_BUCKET:-albaranes}"
  else
    echo "AVISO: Storage no se ha copiado; faltan SUPABASE_URL o SUPABASE_SERVICE_ROLE_KEY." >&2
    echo "Use --skip-storage para aceptar explícitamente una copia solo de base de datos." >&2
    exit 78
  fi
fi

{
  printf 'created_at_utc=%s\n' "$timestamp"
  printf 'database_format=postgres_custom\n'
  printf 'storage_included=%s\n' "$([ "$skip_storage" = false ] && echo true || echo false)"
  printf 'git_revision=%s\n' "$(git rev-parse --verify HEAD 2>/dev/null || echo unknown)"
} > "$backup_dir/metadata.txt"

(
  cd "$backup_dir"
  if command -v sha256sum >/dev/null 2>&1; then
    find . -type f ! -name SHA256SUMS ! -name STATUS -exec sha256sum {} \; | sort > SHA256SUMS
  else
    find . -type f ! -name SHA256SUMS ! -name STATUS -exec shasum -a 256 {} \; | sort > SHA256SUMS
  fi
)
printf '%s\n' "COMPLETE" > "$backup_dir/STATUS"

echo "Verificando la copia"
"$script_dir/verify_backup.sh" "$backup_dir"

# El dashboard solo recibe esta señal sanitizada; nunca se persiste la ruta,
# la URL de conexión ni información sobre objetos concretos del bucket.
storage_included=$([ "$skip_storage" = false ] && echo true || echo false)
psql "$SUPABASE_DB_URL" --no-psqlrc --set=ON_ERROR_STOP=1 \
  --set=backup_timestamp="$timestamp" \
  --set=storage_included="$storage_included" <<'SQL'
INSERT INTO public.audit_events (
  actor_type, actor_id, event_type, data
) VALUES (
  'system', 'backup_database.sh', 'system.backup.completed',
  jsonb_build_object(
    'status', 'complete',
    'verified', true,
    'storage_included', :storage_included,
    'created_at_utc', :'backup_timestamp'
  )
);
SQL
echo "Backup completo: $backup_dir"
