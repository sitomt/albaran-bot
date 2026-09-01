"""Ejecuta OCR, clasificación y extracción actuales sobre el corpus privado.

Las llamadas son facturables. Cada respuesta se registra mediante el mismo
ledger/spool que producción. No crea ingestas ni modifica datos contables.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mistralai.client.sdk import Mistral  # noqa: E402

from scripts.test_pipeline import MANIFEST, evaluate_raw  # noqa: E402
from src import cost_ledger  # noqa: E402
from src.albaran_processor import _MODELO_LLM, _MODELO_OCR  # noqa: E402
from src.config import settings  # noqa: E402
from src.ingestion_service import (  # noqa: E402
    BillableExtractionError, _classify, _extract, _ocr, _record_usage_safely,
)

IMAGES = ROOT / "albaranes_test.md"
OUTPUT = ROOT / "runtime" / "golden-live"


def _pending_cost() -> float:
    return sum(float(row.get("cost_usd") or 0) for row in cost_ledger.pending())


async def evaluate_document(document: dict, client: Mistral, *, reuse_ocr: bool = False) -> dict:
    if _pending_cost() >= settings.MONTHLY_AI_BUDGET_USD:
        raise RuntimeError("presupuesto mensual de IA alcanzado en el spool local")
    path = IMAGES / document["file"]
    artifact_path = OUTPUT / f"{path.stem}.json"
    if reuse_ocr and artifact_path.exists():
        previous = json.loads(artifact_path.read_text(encoding="utf-8"))
        ocr_text = str(previous.get("ocr_text") or "")
        ocr_confidence = previous.get("ocr_confidence")
        ocr_pages = (previous.get("evaluation") or {}).get("ocr_pages")
        classification_raw = previous.get("classification") or {}
    else:
        image = path.read_bytes()
        classification_task = asyncio.create_task(_classify(image, client, "image/jpeg"))
        ocr = await _ocr(image, client, "image/jpeg")
        classification = await classification_task
        await _record_usage_safely(
            ingestion_id=None, user_id=None, operation="ocr",
            model=_MODELO_OCR, usage=ocr.usage, duration_ms=ocr.duration_ms,
            metadata={"purpose": "golden-corpus-evaluation"},
        )
        await _record_usage_safely(
            ingestion_id=None, user_id=None, operation="classification",
            model=_MODELO_LLM, usage=classification.usage, duration_ms=classification.duration_ms,
            metadata={"purpose": "golden-corpus-evaluation"},
        )
        ocr_text = ocr.text
        ocr_confidence = ocr.confidence
        ocr_pages = ocr.usage.pages
        classification_raw = classification.raw
    try:
        raw, usage, duration_ms = await _extract(ocr_text, client)
    except BillableExtractionError as exc:
        await _record_usage_safely(
            ingestion_id=None, user_id=None, operation="extraction",
            model=_MODELO_LLM, usage=exc.usage, duration_ms=exc.duration_ms,
            metadata={"purpose": "golden-corpus-evaluation", "outcome": "parse_error"},
        )
        raise
    await _record_usage_safely(
        ingestion_id=None, user_id=None, operation="extraction",
        model=_MODELO_LLM, usage=usage, duration_ms=duration_ms,
        metadata={"purpose": "golden-corpus-evaluation"},
    )
    row = evaluate_raw(document, raw, source="live", ocr_text=ocr_text)
    row.update({
        "classification": classification_raw,
        "classification_handwritten_expected": document.get("handwritten"),
        "ocr_confidence": ocr_confidence,
        "ocr_pages": ocr_pages,
    })
    artifact = {
        "file": document["file"], "ocr_text": ocr_text,
        "ocr_confidence": ocr_confidence, "classification": classification_raw,
        "extraction": raw, "evaluation": row,
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / f"{path.stem}.json").write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return row


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="Subcadena del nombre")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--reuse-ocr", action="store_true", help="Reutiliza OCR ya guardado y solo repite extracción")
    args = parser.parse_args()
    documents = json.loads(MANIFEST.read_text(encoding="utf-8"))["documents"]
    if args.only:
        documents = [doc for doc in documents if args.only.casefold() in doc["file"].casefold()]
    if args.limit:
        documents = documents[:args.limit]
    client = Mistral(api_key=settings.MISTRAL_API_KEY)
    rows = []
    for index, document in enumerate(documents, start=1):
        print(f"[{index}/{len(documents)}] {document['file']}", flush=True)
        try:
            row = await evaluate_document(document, client, reuse_ocr=args.reuse_ocr)
        except Exception as exc:
            row = {
                "file": document["file"], "source": "live", "safe_route": True,
                "error": str(exc)[:500], "validation_codes": ["live_evaluation_failed"],
                "mismatches": [],
            }
        rows.append(row)
        print(
            f"  safe={row['safe_route']} mismatches={len(row.get('mismatches', []))} "
            f"coste_pendiente=${_pending_cost():.6f}", flush=True
        )
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "report.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    failures = [row for row in rows if row.get("error")]
    unsafe = [row for row in rows if not row["safe_route"]]
    print(
        f"Fin: {len(rows)} documentos, {len(failures)} fallos de llamada, "
        f"{len(unsafe)} escapes inseguros, coste spool=${_pending_cost():.6f}"
    )
    return 1 if failures or unsafe else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
