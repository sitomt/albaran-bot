"""Evalúa extracciones cacheadas contra el corpus real y las reglas actuales.

No llama a Mistral ni escribe en Supabase. El modo de producción completo se
prueba después de la migración mediante Telegram; este programa permite repetir
la evaluación determinista sin coste y hace visibles los errores de OCR antiguos.

Uso:
  python scripts/test_pipeline.py
  python scripts/test_pipeline.py --only Lucas --json-out runtime/golden-report.json
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.accounting_validation import validate_candidate  # noqa: E402
from src.albaran_processor import AlbaranLLM, _resolver_precio_neto  # noqa: E402
from src.config import settings  # noqa: E402
from src.ingestion_service import _header_provenance_issues  # noqa: E402

MANIFEST = ROOT / "tests" / "fixtures" / "golden_albaranes.json"
CACHE = ROOT / ".cache_test"


def _normal(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _money_equal(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is right
    return abs(Decimal(str(left)) - Decimal(str(right))) <= Decimal("0.03")


def _load_documents(only: str | None) -> list[dict[str, Any]]:
    documents = json.loads(MANIFEST.read_text(encoding="utf-8"))["documents"]
    if only:
        documents = [doc for doc in documents if only.casefold() in doc["file"].casefold()]
    return documents


def _header_mismatches(expected: dict[str, Any], model: AlbaranLLM) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    accepted_numbers = expected.get("accepted_numbers") or [expected.get("number")]
    accepted_numbers = [value for value in accepted_numbers if value]
    if accepted_numbers and _normal(model.numero_albaran) not in {_normal(value) for value in accepted_numbers}:
        mismatches.append({
            "field": "numero_albaran", "expected": accepted_numbers,
            "observed": model.numero_albaran,
        })
    pairs = [
        ("fecha", expected.get("date"), model.fecha, lambda a, b: a == b),
        ("base_imponible", expected.get("base"), model.base_imponible, _money_equal),
        ("total_iva", expected.get("vat"), model.total_iva, _money_equal),
        ("total", expected.get("total"), model.total, _money_equal),
    ]
    for field, wanted, observed, comparator in pairs:
        if field not in {"fecha"} and {"base_imponible": "base", "total_iva": "vat", "total": "total"}[field] not in expected:
            continue
        if field == "fecha" and "date" not in expected:
            continue
        if not comparator(wanted, observed):
            mismatches.append({"field": field, "expected": wanted, "observed": observed})
    return mismatches


def evaluate_raw(
    document: dict[str, Any], raw: dict[str, Any], *, source: str, ocr_text: str = ""
) -> dict[str, Any]:
    raw = copy.deepcopy(raw)
    raw.pop("_extraction_complete", None)
    try:
        model = AlbaranLLM.model_validate(copy.deepcopy(raw))
    except Exception as exc:
        return {
            "file": document["file"], "expected_outcome": document["expected_outcome"],
            "source": source, "cache": "invalid", "safe_route": True, "mismatches": [],
            "validation_codes": ["candidate_schema_invalid"], "error": str(exc)[:500],
        }

    for line in model.lineas:
        _resolver_precio_neto(line)
    report = validate_candidate(
        model,
        extraction_complete=True,
        document_is_handwritten=bool(document.get("handwritten")),
        # La caché antigua no conservó scores. Este 0,99 aísla la exactitud de
        # campos; en producción una confianza ausente/baja añade otra revisión.
        ocr_confidence=0.99,
    )
    mismatches = _header_mismatches(document.get("header", {}), model)
    if ocr_text:
        candidate = {"header": {
            "base_imponible": model.base_imponible,
            "total_iva": model.total_iva,
            "total": model.total,
        }}
        extra_codes = {issue.code for issue in _header_provenance_issues(candidate, ocr_text)}
    else:
        extra_codes = set()
    core_safe = not report.auto_confirmable or bool(mismatches)
    human_gate = not settings.AUTO_CONFIRM_CLEAN
    expected_caution = document["expected_outcome"] in {"review", "manual"}
    return {
        "file": document["file"],
        "source": source,
        "expected_outcome": document["expected_outcome"],
        "cache": "loaded",
        "provider": model.proveedor_nombre,
        "number": model.numero_albaran,
        "date": model.fecha,
        "total": model.total,
        "line_count": len(model.lineas),
        "validation_codes": sorted({issue.code for issue in report.issues} | extra_codes),
        "mismatches": mismatches,
        "accounting_auto_confirmable": report.auto_confirmable and not extra_codes,
        "human_confirmation_gate": human_gate,
        "safe_route": (not expected_caution) or core_safe or human_gate,
    }


def evaluate(document: dict[str, Any]) -> dict[str, Any]:
    raw_path = CACHE / f"{Path(document['file']).stem}.raw.json"
    if not raw_path.exists():
        return {
            "file": document["file"], "expected_outcome": document["expected_outcome"],
            "source": "legacy-cache", "cache": "missing",
            "safe_route": document["expected_outcome"] != "confirmable",
            "mismatches": [], "validation_codes": ["cached_extraction_missing"],
        }
    return evaluate_raw(
        document, json.loads(raw_path.read_text(encoding="utf-8")), source="legacy-cache"
    )


def print_report(rows: list[dict[str, Any]]) -> None:
    print("EVALUACIÓN DEL CORPUS REAL (extracciones cacheadas, reglas actuales)\n")
    for row in rows:
        flags = []
        if row.get("validation_codes"):
            flags.append("reglas=" + ",".join(row["validation_codes"]))
        if row.get("mismatches"):
            flags.append("campos_ref=" + ",".join(item["field"] for item in row["mismatches"]))
        detail = "; ".join(flags) or "sin discrepancias deterministas"
        print(
            f"{'OK' if row['safe_route'] else 'UNSAFE':6} {row['file']} -> "
            f"{row['expected_outcome']} ({detail})"
        )
    mismatches = sum(len(row.get("mismatches", [])) for row in rows)
    unsafe = sum(not row["safe_route"] for row in rows)
    print(f"\nDocumentos: {len(rows)} | discrepancias de referencia: {mismatches} | escapes inseguros: {unsafe}")
    print("AUTO_CONFIRM_CLEAN está " + ("ACTIVO" if settings.AUTO_CONFIRM_CLEAN else "desactivado"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="Subcadena del nombre de imagen")
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    rows = [evaluate(document) for document in _load_documents(args.only)]
    print_report(rows)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if any(not row["safe_route"] for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
