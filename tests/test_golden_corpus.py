import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "tests" / "fixtures" / "golden_albaranes.json"
IMAGES = ROOT / "albaranes_test.md"


def _money(values):
    return sum((Decimal(str(value)) for value in values), Decimal("0")).quantize(Decimal("0.01"))


def _documents():
    return json.loads(MANIFEST.read_text())["documents"]


def test_corpus_real_tiene_hashes_estables_y_sin_duplicados():
    documents = _documents()
    assert len(documents) == 11
    if not IMAGES.is_dir():
        pytest.skip("las fotos reales son privadas y no se versionan en CI")
    hashes = []
    for document in documents:
        image = IMAGES / document["file"]
        assert image.is_file(), document["file"]
        digest = hashlib.sha256(image.read_bytes()).hexdigest()
        assert digest == document["sha256"]
        hashes.append(digest)
    assert len(hashes) == len(set(hashes))


def test_corpus_cubre_confirmacion_revision_y_entrada_manual():
    outcomes = {document["expected_outcome"] for document in _documents()}
    assert outcomes == {"confirmable", "review", "manual"}
    assert any(document["handwritten"] for document in _documents())
    assert any(document.get("extra_charges") for document in _documents())


def test_referencias_confirmables_reconcilian_sin_inventar_datos():
    for document in _documents():
        if document["expected_outcome"] != "confirmable":
            continue
        header = document["header"]
        amounts = document.get("line_amounts")
        if amounts:
            assert _money(amounts) == Decimal(str(header["base"])).quantize(Decimal("0.01"))
        vat = document.get("vat_breakdown", [])
        if vat:
            assert _money(item["base"] for item in vat) == Decimal(str(header["base"])).quantize(Decimal("0.01"))
            assert _money(item["quota"] for item in vat) == Decimal(str(header["vat"])).quantize(Decimal("0.01"))
        assert _money([header["base"], header["vat"]]) == Decimal(str(header["total"])).quantize(Decimal("0.01"))


def test_documentos_ambiguos_no_se_marcan_como_confirmables():
    by_name = {document["file"]: document for document in _documents()}
    assert by_name["albaran-problematico1.JPG"]["expected_outcome"] == "manual"
    assert by_name["albaran-problematico2.JPG"]["expected_outcome"] == "review"
    assert by_name["PHOTO-2026-05-16-12-26-45.jpg"]["expected_outcome"] == "review"
    assert by_name["c6394709-5024-4d40-8a91-239fd00a8db5.JPG"]["expected_outcome"] == "review"
