#!/usr/bin/env python3
"""Genera una restauración transaccional desde un backup PostgREST de emergencia.

La salida SQL se canaliza a ``psql``. El destino debe tener aplicado el baseline
legado y estar vacío; así nunca mezcla accidentalmente dos restaurantes ni pisa
filas existentes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys


TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "proveedores": (
        "id", "nombre", "nif", "direccion", "telefono", "email",
        "forma_pago_habitual", "creado_en",
    ),
    "productos_catalogo": (
        "id", "nombre_normalizado", "proveedor_id", "variantes", "unidad_base",
        "formato_habitual", "precio_ultima_compra", "precio_medio_historico",
        "ultima_compra_fecha", "creado_en",
    ),
    "albaranes": (
        "id", "numero_albaran", "fecha", "proveedor_id", "forma_pago",
        "base_imponible", "total_iva", "detalle_iva", "total", "imagen_url",
        "imagen_hash", "origen", "creado_en",
    ),
    "lineas_albaran": (
        "id", "albaran_id", "producto_catalogo_id", "descripcion_original",
        "descripcion_limpia", "cantidad", "unidad", "peso_unitario_g",
        "unidades_por_envase", "peso_total_kg", "volumen_unitario_l",
        "formato_envase", "numero_lote", "caducidad", "precio_unitario",
        "descuento_pct", "importe_neto", "confianza", "requiere_revision",
    ),
    "auditoria": (
        "id", "tipo", "albaran_id", "telegram_user_id", "imagen_url",
        "modelo_ocr", "modelo_llm", "tokens_consumidos", "coste_estimado_usd",
        "resultado", "detalle", "creado_en",
    ),
    "jobs": (
        "id", "telegram_user_id", "imagen_url", "estado", "intentos",
        "error_detalle", "creado_en", "actualizado_en",
    ),
    "correcciones": (
        "id", "linea_albaran_id", "campo", "valor_original", "valor_corregido",
        "corregido_por", "creado_en",
    ),
}

RESTORE_ORDER = tuple(TABLE_COLUMNS)


def _load_backup(root: Path) -> dict[str, list[dict]]:
    manifest_path = root / "data-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != "supabase-postgrest-emergency-v1":
        raise ValueError("formato de backup de emergencia no reconocido")

    declared = {
        str(item.get("table")): item
        for item in manifest.get("tables", [])
        if isinstance(item, dict)
    }
    unknown_nonempty: list[str] = []
    result: dict[str, list[dict]] = {}
    for table, item in declared.items():
        file_name = str(item.get("file") or "")
        path = root / file_name
        if not file_name or path.parent != root or not path.is_file():
            raise ValueError(f"archivo no válido para {table}")
        rows = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"filas no válidas en {table}")
        if len(rows) != int(item.get("rows", -1)):
            raise ValueError(f"conteo del manifiesto no coincide para {table}")
        if table not in TABLE_COLUMNS:
            if rows:
                unknown_nonempty.append(table)
            continue
        result[table] = rows
    if unknown_nonempty:
        raise ValueError("tablas no soportadas con datos: " + ", ".join(sorted(unknown_nonempty)))
    return {table: result.get(table, []) for table in RESTORE_ORDER}


def build_restore_sql(root: Path) -> str:
    tables = _load_backup(root.resolve())
    statements = [
        "\\set ON_ERROR_STOP on",
        "BEGIN;",
        "SET LOCAL lock_timeout = '10s';",
        "SET LOCAL statement_timeout = '5min';",
        "LOCK TABLE " + ", ".join(f"public.{table}" for table in RESTORE_ORDER)
        + " IN EXCLUSIVE MODE;",
    ]
    nonempty = " OR ".join(f"EXISTS (SELECT 1 FROM public.{table})" for table in RESTORE_ORDER)
    statements.append(
        "DO $$ BEGIN IF " + nonempty
        + " THEN RAISE EXCEPTION 'emergency restore requires empty legacy tables' "
          "USING ERRCODE='55000'; END IF; END $$;"
    )

    for table in RESTORE_ORDER:
        rows = tables[table]
        if not rows:
            continue
        payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        tag = "restore_" + hashlib.sha256(payload.encode()).hexdigest()[:16]
        delimiter = f"${tag}$"
        if delimiter in payload:
            raise ValueError("el payload contiene un delimitador SQL reservado")
        columns = TABLE_COLUMNS[table]
        column_list = ", ".join(columns)
        statements.append(
            f"INSERT INTO public.{table} ({column_list})\n"
            f"SELECT {column_list} FROM jsonb_populate_recordset("
            f"NULL::public.{table}, {delimiter}{payload}{delimiter}::jsonb);"
        )

    checks = " ".join(
        f"IF (SELECT count(*) FROM public.{table}) <> {len(tables[table])} "
        f"THEN RAISE EXCEPTION 'restored count mismatch for {table}'; END IF;"
        for table in RESTORE_ORDER
    )
    statements.extend([f"DO $$ BEGIN {checks} END $$;", "COMMIT;", "\\echo emergency_restore_ok"])
    return "\n".join(statements) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path, help="directorio data/ del backup")
    args = parser.parse_args()
    try:
        sys.stdout.write(build_restore_sql(args.input))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 65
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
