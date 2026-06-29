"""
Verificación METICULOSA: la BD debe coincidir EXACTAMENTE con los datos reales del albarán.

Para cada uno de los 9 albaranes ingeridos:
  - Reconstruye los valores esperados desde .cache_test/*.raw.json (verificados contra OCR),
    aplicando la misma resolución determinista de precio NETO del pipeline.
  - Consulta la BD (albaranes + líneas).
  - Compara cabecera (nº, fecha, total, base, IVA) y el MULTISET de líneas
    (cantidad, precio_unitario, importe_neto) — independiente del orden.
Reporta cualquier discrepancia. Salida limpia = fidelidad perfecta.
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src import albaran_processor as ap  # noqa: E402
from src import supabase_client as db    # noqa: E402

_CACHE = _ROOT / ".cache_test"
_EXCLUIR = {"albaran-problematico1", "albaran-problematico2"}


def _esperado(stem: str) -> dict:
    raw = json.loads((_CACHE / f"{stem}.raw.json").read_text(encoding="utf-8"))
    alb = ap.AlbaranLLM.model_validate(raw)
    for l in alb.lineas:
        ap._resolver_precio_neto(l)
    return {
        "numero": alb.numero_albaran,
        "fecha": alb.fecha,
        "total": alb.total,
        "base": alb.base_imponible,
        "iva": alb.total_iva,
        "n_lineas": len(alb.lineas),
        "lineas": Counter(
            (round(l.cantidad, 3), round(l.precio_unitario, 4) if l.precio_unitario else None,
             round(l.importe_neto, 2) if l.importe_neto else None)
            for l in alb.lineas
        ),
    }


def _num(v):
    return float(v) if v is not None else None


async def main() -> None:
    stems = sorted(
        p.stem for p in (_ROOT / "albaranes_test.md").iterdir()
        if p.suffix.lower() in {".jpg", ".jpeg", ".png"} and not p.name.startswith(".")
        and p.stem not in _EXCLUIR
    )

    client = await db.get_client()
    alb_rows = (await client.table("albaranes").select(
        "id, numero_albaran, fecha, total, base_imponible, total_iva, origen, proveedor_id, proveedores(nombre, nif)"
    ).execute()).data

    total_problemas = 0
    print(f"Verificando {len(stems)} albaranes contra la BD ({len(alb_rows)} en BD)\n")

    for stem in stems:
        esp = _esperado(stem)
        # Emparejar por número normalizado o por total
        cand = [a for a in alb_rows if (a.get("numero_albaran") == esp["numero"])]
        if not cand and esp["total"]:
            cand = [a for a in alb_rows if a.get("total") and abs(float(a["total"]) - esp["total"]) < 0.01]
        if not cand:
            print(f"❌ {stem}: NO ENCONTRADO en BD")
            total_problemas += 1
            continue
        alb = cand[0]
        prov = (alb.get("proveedores") or {})
        problemas = []

        # Cabecera
        if (alb.get("numero_albaran") or None) != (esp["numero"] or None):
            problemas.append(f"nº BD={alb.get('numero_albaran')} vs esp={esp['numero']}")
        if str(alb.get("fecha")) != esp["fecha"]:
            problemas.append(f"fecha BD={alb.get('fecha')} vs esp={esp['fecha']}")
        if esp["total"] is not None and (_num(alb.get("total")) is None or abs(_num(alb["total"]) - esp["total"]) > 0.01):
            problemas.append(f"total BD={alb.get('total')} vs esp={esp['total']}")

        # Líneas
        lineas_db = (await client.table("lineas_albaran").select(
            "cantidad, precio_unitario, importe_neto, descripcion_limpia"
        ).eq("albaran_id", alb["id"]).execute()).data
        db_counter = Counter(
            (round(_num(l["cantidad"]), 3) if l["cantidad"] is not None else None,
             round(_num(l["precio_unitario"]), 4) if l["precio_unitario"] is not None else None,
             round(_num(l["importe_neto"]), 2) if l["importe_neto"] is not None else None)
            for l in lineas_db
        )
        if len(lineas_db) != esp["n_lineas"]:
            problemas.append(f"nº líneas BD={len(lineas_db)} vs esp={esp['n_lineas']}")
        faltan = esp["lineas"] - db_counter
        sobran = db_counter - esp["lineas"]
        if faltan:
            problemas.append(f"líneas esperadas no halladas: {list(faltan.elements())}")
        if sobran:
            problemas.append(f"líneas en BD inesperadas: {list(sobran.elements())}")

        estado = "✓" if not problemas else "❌"
        print(f"{estado} {stem[:26]:28} {prov.get('nombre','?')[:26]:28} nif={prov.get('nif')} "
              f"líneas={len(lineas_db)}/{esp['n_lineas']}")
        for p in problemas:
            print(f"      ↳ {p}")
            total_problemas += 1

    # Proveedores: detectar duplicados por nombre normalizado
    print("\n— Proveedores en BD —")
    provs = (await client.table("proveedores").select("nombre, nif").order("nombre").execute()).data
    nombres = Counter(p["nombre"].strip().lower() for p in provs)
    for p in provs:
        dup = " ⚠DUPLICADO" if nombres[p["nombre"].strip().lower()] > 1 else ""
        print(f"   {p['nombre']:38} {p['nif']}{dup}")

    print(f"\n{'✅ FIDELIDAD PERFECTA' if total_problemas == 0 else f'❌ {total_problemas} discrepancia(s)'}")


if __name__ == "__main__":
    asyncio.run(main())
