from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops.restore_data_api import build_restore_sql


def _backup(tmp_path: Path, tables: dict[str, list[dict]]) -> Path:
    entries = []
    for table, rows in tables.items():
        name = f"{table}.json"
        (tmp_path / name).write_text(json.dumps(rows), encoding="utf-8")
        entries.append({"table": table, "rows": len(rows), "file": name})
    (tmp_path / "data-manifest.json").write_text(json.dumps({
        "format": "supabase-postgrest-emergency-v1", "tables": entries,
    }), encoding="utf-8")
    return tmp_path


def test_genera_restore_transaccional_y_omite_columnas_generadas(tmp_path):
    root = _backup(tmp_path, {"proveedores": [{
        "id": "00000000-0000-0000-0000-000000000001", "nombre": "Proveedor",
        "nif": "B1", "nombre_normalizado": "proveedor", "nif_normalizado": "B1",
    }]})
    sql = build_restore_sql(root)
    assert "BEGIN;" in sql and "COMMIT;" in sql
    assert "emergency restore requires empty legacy tables" in sql
    insert_columns = sql.split("INSERT INTO public.proveedores", 1)[1].split("SELECT", 1)[0]
    assert "nombre_normalizado" not in insert_columns
    assert "restored count mismatch for proveedores" in sql


def test_rechaza_tabla_desconocida_con_datos(tmp_path):
    root = _backup(tmp_path, {"tabla_extra": [{"id": 1}]})
    with pytest.raises(ValueError, match="tablas no soportadas"):
        build_restore_sql(root)


def test_rechaza_conteo_de_manifiesto_incorrecto(tmp_path):
    root = _backup(tmp_path, {"proveedores": []})
    manifest = json.loads((root / "data-manifest.json").read_text())
    manifest["tables"][0]["rows"] = 2
    (root / "data-manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="conteo"):
        build_restore_sql(root)
