#!/bin/sh
set -eu

target_url=${1:-${RESTORE_TARGET_DB_URL:-}}
[ -n "$target_url" ] || {
  echo "Uso: scripts/verify_restored_database.sh URL_DESTINO" >&2
  exit 64
}
command -v psql >/dev/null 2>&1 || { echo "Falta psql" >&2; exit 69; }

echo "Comprobando tablas, claves huérfanas y conteos en la base restaurada"
psql "$target_url" --set=ON_ERROR_STOP=1 --no-psqlrc --tuples-only <<'SQL'
SELECT 'proveedores=' || count(*) FROM public.proveedores;
SELECT 'productos_catalogo=' || count(*) FROM public.productos_catalogo;
SELECT 'albaranes=' || count(*) FROM public.albaranes;
SELECT 'lineas_albaran=' || count(*) FROM public.lineas_albaran;
SELECT 'auditoria=' || count(*) FROM public.auditoria;
SELECT 'jobs=' || count(*) FROM public.jobs;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM public.albaranes a
    LEFT JOIN public.proveedores p ON p.id = a.proveedor_id
    WHERE p.id IS NULL
  ) THEN
    RAISE EXCEPTION 'Hay albaranes con proveedor inexistente';
  END IF;
  IF EXISTS (
    SELECT 1 FROM public.lineas_albaran l
    LEFT JOIN public.albaranes a ON a.id = l.albaran_id
    WHERE a.id IS NULL
  ) THEN
    RAISE EXCEPTION 'Hay líneas con albarán inexistente';
  END IF;
END $$;
SQL
echo "OK: restauración coherente a nivel estructural"
