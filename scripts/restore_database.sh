#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

usage() {
  cat <<'EOF'
Uso:
  scripts/restore_database.sh BACKUP_DIR --target-url URL
  scripts/restore_database.sh BACKUP_DIR --target-url URL --execute [--clean]

Sin --execute solo valida y muestra el plan. La restauración debe hacerse primero
en una base vacía y aislada. --clean elimina objetos existentes y requiere además
--allow-destructive-clean. Para un entorno marcado ENVIRONMENT=production se
requiere --allow-production.
EOF
}

[ "$#" -ge 1 ] || { usage >&2; exit 64; }
case "$1" in
  -h|--help) usage; exit 0 ;;
esac
backup_dir=${1%/}
shift
target_url=""
execute=false
clean=false
allow_clean=false
allow_production=false

while [ "$#" -gt 0 ]; do
  case "$1" in
    --target-url)
      [ "$#" -ge 2 ] || { usage >&2; exit 64; }
      target_url=$2
      shift 2
      ;;
    --execute) execute=true; shift ;;
    --clean) clean=true; shift ;;
    --allow-destructive-clean) allow_clean=true; shift ;;
    --allow-production) allow_production=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Argumento desconocido: $1" >&2; usage >&2; exit 64 ;;
  esac
done

[ -n "$target_url" ] || { echo "Falta --target-url" >&2; exit 64; }
"$script_dir/verify_backup.sh" "$backup_dir"

if [ "$clean" = true ] && [ "$allow_clean" != true ]; then
  echo "--clean requiere --allow-destructive-clean" >&2
  exit 77
fi
if [ "${ENVIRONMENT:-}" = "production" ] && [ "$allow_production" != true ]; then
  echo "Restauración bloqueada: ENVIRONMENT=production requiere --allow-production" >&2
  exit 77
fi
if [ -n "${SUPABASE_DB_URL:-}" ] && [ "$target_url" = "$SUPABASE_DB_URL" ] && [ "$allow_production" != true ]; then
  echo "Restauración bloqueada: el destino coincide con SUPABASE_DB_URL." >&2
  echo "Use una base aislada o añada --allow-production tras verificar el destino." >&2
  exit 77
fi

echo "Backup: $backup_dir/database.dump"
echo "Destino: URL proporcionada (credenciales ocultas)"
echo "Modo clean: $clean"
if [ "$execute" != true ]; then
  echo "DRY RUN: no se ha modificado ninguna base. Añada --execute para restaurar."
  exit 0
fi

command -v pg_restore >/dev/null 2>&1 || { echo "Falta pg_restore" >&2; exit 69; }
set -- \
  --dbname="$target_url" \
  --exit-on-error \
  --single-transaction \
  --no-owner \
  --no-privileges
if [ "$clean" = true ]; then
  set -- "$@" --clean --if-exists
fi

echo "Iniciando restauración. No interrumpir el proceso."
pg_restore "$@" "$backup_dir/database.dump"
echo "Restauración completada. Ejecute scripts/verify_restored_database.sh sobre el destino."
