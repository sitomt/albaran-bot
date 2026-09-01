"""Normalización y dígito de control de identificadores fiscales españoles."""
from __future__ import annotations

import re

_DNI_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"
_CIF_LETTERS = "JABCDEFGHI"


def normalize_tax_id(value: str | None) -> str:
    normalized = re.sub(r"[^A-Z0-9]", "", (value or "").upper())
    if normalized.startswith("ES") and len(normalized) > 9:
        normalized = normalized[2:]
    return normalized


def is_valid_spanish_tax_id(value: str | None) -> bool:
    value = normalize_tax_id(value)
    if re.fullmatch(r"\d{8}[A-Z]", value):
        return value[-1] == _DNI_LETTERS[int(value[:8]) % 23]
    if re.fullmatch(r"[XYZ]\d{7}[A-Z]", value):
        number = str("XYZ".index(value[0])) + value[1:8]
        return value[-1] == _DNI_LETTERS[int(number) % 23]
    if not re.fullmatch(r"[ABCDEFGHJNPQRSUVW]\d{7}[0-9A-J]", value):
        return False
    digits = [int(character) for character in value[1:8]]
    odd_sum = sum(sum(divmod(digit * 2, 10)) for digit in digits[0::2])
    even_sum = sum(digits[1::2])
    control_digit = (10 - (odd_sum + even_sum) % 10) % 10
    expected_digit = str(control_digit)
    expected_letter = _CIF_LETTERS[control_digit]
    if value[0] in "ABEH":
        return value[-1] == expected_digit
    if value[0] in "KPQS":
        return value[-1] == expected_letter
    return value[-1] in {expected_digit, expected_letter}
