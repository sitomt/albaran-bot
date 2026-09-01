from src.albaran_processor import AlbaranLLM
from src.ingestion_service import _candidate_payload, _header_provenance_issues
from src.review_service import _tax_id_summary
from src.spanish_tax_id import is_valid_spanish_tax_id, normalize_tax_id


def test_valida_nif_cif_y_prefijo_es_con_digito_de_control():
    assert is_valid_spanish_tax_id("B30402812")
    assert is_valid_spanish_tax_id("A-28/647451")
    assert is_valid_spanish_tax_id("ESB30402812")
    assert normalize_tax_id(" ES B-30402812 ") == "B30402812"
    assert not is_valid_spanish_tax_id("B30402813")
    assert not is_valid_spanish_tax_id("9-30058911")


def test_nif_ocr_invalido_se_conserva_como_observado_pero_no_contamina_maestro():
    model = AlbaranLLM(proveedor_nombre="Proveedor", proveedor_nif="B30402813", lineas=[])
    candidate = _candidate_payload(model, {"lineas": []})

    assert candidate["header"]["proveedor_nif"] is None
    decision = candidate["header"]["decisiones"]["proveedor_nif"]
    assert decision["observed"] == "B30402813"
    assert decision["rule"] == "invalid-check-digit"
    issues = _header_provenance_issues(candidate, "B30402813")
    assert any(issue.code == "supplier_tax_id_invalid" for issue in issues)
    assert "no se guardará" in _tax_id_summary(candidate["header"])


def test_nif_valido_se_acepta_y_se_muestra_en_revision():
    model = AlbaranLLM(proveedor_nombre="Proveedor", proveedor_nif="B73623910", lineas=[])
    candidate = _candidate_payload(model, {"lineas": []})

    assert candidate["header"]["proveedor_nif"] == "B73623910"
    assert "B73623910" in _tax_id_summary(candidate["header"])
