"""Validación contable determinista de candidatos de albarán.

Este módulo no corrige ni sobrescribe valores observados. Produce incidencias que
deciden si un candidato puede confirmarse automáticamente o necesita revisión.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    severity: str = "error"
    field: str | None = None
    line_index: int | None = None
    observed: Any = None
    expected: Any = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ValidationReport:
    issues: tuple[ValidationIssue, ...]
    line_sum: Decimal | None
    auto_confirmable: bool

    @property
    def needs_review(self) -> bool:
        return not self.auto_confirmable

    def to_dict(self) -> dict[str, Any]:
        return {
            "issues": [issue.to_dict() for issue in self.issues],
            "line_sum": float(self.line_sum) if self.line_sum is not None else None,
            "auto_confirmable": self.auto_confirmable,
        }


CENT = Decimal("0.01")


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _money_equal(left: Decimal, right: Decimal, *, line_count: int = 1) -> bool:
    # Un céntimo por línea más dos céntimos de margen para redondeos del pie.
    tolerance = CENT * max(3, line_count + 2)
    return abs(left - right) <= tolerance


def _line_amount_tolerance(amount: Decimal) -> Decimal:
    # En productos vendidos por peso (kg), el proveedor suele calcular tarifa,
    # descuento y neto con más decimales de los que imprime, y el importe final
    # arrastra ese redondeo. Un margen fijo de 3 céntimos genera falsos
    # descuadres en albaranes de carnicería/charcutería; toleramos además un
    # 0,5% del importe de la línea (nunca menos de los 3 céntimos de base).
    return max(CENT * 3, (amount * Decimal("0.005")).quantize(CENT, rounding=ROUND_HALF_UP))


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def validate_candidate(
    candidate: Any,
    *,
    extraction_complete: bool = True,
    document_is_handwritten: bool = False,
    ocr_confidence: float | None = None,
) -> ValidationReport:
    issues: list[ValidationIssue] = []
    lines: list[Any] = list(_get(candidate, "lineas", []) or [])

    supplier = str(_get(candidate, "proveedor_nombre", "") or "").strip()
    if not supplier:
        issues.append(ValidationIssue("supplier_missing", "Falta el proveedor.", field="proveedor_nombre"))

    raw_date = _get(candidate, "fecha")
    try:
        parsed_date = date.fromisoformat(str(raw_date))
        if parsed_date > date.today():
            issues.append(ValidationIssue("date_future", "La fecha es futura.", field="fecha", observed=raw_date))
    except (TypeError, ValueError):
        issues.append(ValidationIssue("date_invalid", "La fecha falta o no es válida.", field="fecha", observed=raw_date))

    if not extraction_complete:
        issues.append(ValidationIssue(
            "extraction_incomplete", "La respuesta de extracción estaba truncada o incompleta."
        ))
    if document_is_handwritten:
        issues.append(ValidationIssue(
            "handwritten_document", "El documento es manuscrito y requiere revisión humana.", severity="warning"
        ))
    if ocr_confidence is None:
        issues.append(ValidationIssue(
            "ocr_confidence_missing", "El OCR no proporcionó confianza verificable.", severity="warning"
        ))
    elif ocr_confidence < 0.85:
        issues.append(ValidationIssue(
            "ocr_confidence_low", "La confianza global del OCR es baja.", severity="warning",
            observed=round(ocr_confidence, 4), expected=0.85,
        ))

    if not lines:
        issues.append(ValidationIssue("lines_missing", "El albarán no contiene líneas.", field="lineas"))

    line_amounts: list[Decimal] = []
    for index, line in enumerate(lines, start=1):
        description = str(
            _get(line, "descripcion_limpia") or _get(line, "descripcion_original")
            or _get(line, "nombre_producto") or ""
        ).strip()
        quantity = _decimal(_get(line, "cantidad"))
        unit_price = _decimal(_get(line, "precio_unitario"))
        amount = _decimal(_get(line, "importe_neto"))
        discount = _decimal(_get(line, "descuento_pct"))
        confidence = _get(line, "confianza")

        if not description:
            issues.append(ValidationIssue(
                "line_description_missing", "Falta el nombre del producto.",
                field="descripcion_limpia", line_index=index,
            ))

        if quantity is None or quantity <= 0:
            issues.append(ValidationIssue(
                "line_quantity_invalid", "Cantidad ausente o no positiva.", field="cantidad",
                line_index=index, observed=_get(line, "cantidad"),
            ))
        if unit_price is None or unit_price < 0:
            issues.append(ValidationIssue(
                "line_price_invalid", "Precio neto ausente o negativo.", field="precio_unitario",
                line_index=index, observed=_get(line, "precio_unitario"),
            ))
        if amount is None or amount < 0:
            issues.append(ValidationIssue(
                "line_amount_invalid", "Importe de línea ausente o negativo.", field="importe_neto",
                line_index=index, observed=_get(line, "importe_neto"),
            ))
        else:
            line_amounts.append(amount)
        if discount is not None and not (Decimal("0") <= discount < Decimal("100")):
            issues.append(ValidationIssue(
                "line_discount_invalid", "El descuento debe estar entre 0 y 100.", field="descuento_pct",
                line_index=index, observed=_get(line, "descuento_pct"),
            ))
        if confidence is None or not isinstance(confidence, (int, float)) or confidence < 85:
            issues.append(ValidationIssue(
                "line_confidence_low", "La línea no tiene confianza suficiente.", severity="warning",
                field="confianza", line_index=index, observed=confidence, expected=85,
            ))

        # Solo comprobar el producto cuando ambos operandos son observados. No se usa
        # este cálculo para sustituir el importe que figura en el documento.
        if quantity is not None and unit_price is not None and amount is not None:
            expected_amount = (quantity * unit_price).quantize(CENT, rounding=ROUND_HALF_UP)
            if abs(amount - expected_amount) > _line_amount_tolerance(expected_amount):
                # Algunos albaranes imprimen NETO con 2 decimales, pero calculan
                # el importe usando TARIFA × (1-DTO) con más precisión. Aceptar
                # esa segunda igualdad evita falsos descuadres sin modificar el
                # importe observado ni volver a aplicar el descuento al canónico.
                tariff = _decimal(_get(line, "precio_tarifa"))
                discounted_amount = None
                if tariff is not None and discount is not None:
                    discounted_amount = (
                        quantity * tariff * (Decimal("100") - discount) / Decimal("100")
                    ).quantize(CENT, rounding=ROUND_HALF_UP)
                if discounted_amount is None or abs(amount - discounted_amount) > _line_amount_tolerance(discounted_amount):
                    issues.append(ValidationIssue(
                        "line_amount_mismatch", "Cantidad × precio no coincide con el importe observado.",
                        field="importe_neto", line_index=index, observed=float(amount), expected=float(expected_amount),
                    ))

    line_sum = sum(line_amounts, Decimal("0")).quantize(CENT) if len(line_amounts) == len(lines) and lines else None
    base = _decimal(_get(candidate, "base_imponible"))
    vat = _decimal(_get(candidate, "total_iva"))
    total = _decimal(_get(candidate, "total"))

    if base is None:
        issues.append(ValidationIssue("base_missing", "Falta la base imponible.", severity="warning", field="base_imponible"))
    if total is None or total <= 0:
        issues.append(ValidationIssue("total_missing", "Falta el total a pagar.", field="total"))
    if line_sum is not None and base is not None and not _money_equal(line_sum, base, line_count=len(lines)):
        issues.append(ValidationIssue(
            "base_lines_mismatch", "La suma de líneas no coincide con la base imponible.",
            field="base_imponible", observed=float(base), expected=float(line_sum),
        ))

    vat_details: Iterable[Any] = _get(candidate, "detalle_iva", []) or []
    detail_bases: list[Decimal] = []
    detail_quotas: list[Decimal] = []
    for index, detail in enumerate(vat_details, start=1):
        rate = _decimal(_get(detail, "tipo"))
        detail_base = _decimal(_get(detail, "base"))
        quota = _decimal(_get(detail, "cuota"))
        if rate is None or rate < 0 or rate > 100 or detail_base is None or detail_base < 0 or quota is None or quota < 0:
            issues.append(ValidationIssue(
                "vat_detail_invalid", "Un tramo de IVA contiene valores inválidos.",
                field="detalle_iva", line_index=index,
            ))
            continue
        expected_quota = (detail_base * rate / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)
        # Cuota impresa en 0 con un tipo distinto de 0 no es un error de lectura: es
        # habitual en albaranes de entrega (no factura) donde el IVA se indica como
        # referencia pero todavía no se repercute; se liquidará en la factura.
        # Comparar aquí base × tipo generaría un falso descuadre sistemático.
        if quota != Decimal("0") and not _money_equal(quota, expected_quota):
            issues.append(ValidationIssue(
                "vat_quota_mismatch", "La cuota del tramo de IVA no coincide con base × tipo.",
                field="detalle_iva", line_index=index, observed=float(quota), expected=float(expected_quota),
            ))
        detail_bases.append(detail_base)
        detail_quotas.append(quota)

    if detail_bases and base is not None:
        details_base_sum = sum(detail_bases, Decimal("0")).quantize(CENT)
        if not _money_equal(details_base_sum, base, line_count=len(detail_bases)):
            issues.append(ValidationIssue(
                "vat_bases_mismatch", "Las bases de los tramos no suman la base imponible.",
                field="detalle_iva", observed=float(details_base_sum), expected=float(base),
            ))
    if detail_quotas:
        details_vat_sum = sum(detail_quotas, Decimal("0")).quantize(CENT)
        if vat is None or not _money_equal(details_vat_sum, vat, line_count=len(detail_quotas)):
            issues.append(ValidationIssue(
                "vat_total_mismatch", "Las cuotas de los tramos no suman el IVA total.",
                field="total_iva", observed=float(vat) if vat is not None else None,
                expected=float(details_vat_sum),
            ))

    if base is not None and total is not None:
        effective_vat = vat or Decimal("0")
        expected_total = (base + effective_vat).quantize(CENT)
        if not _money_equal(total, expected_total, line_count=max(1, len(detail_quotas))):
            issues.append(ValidationIssue(
                "document_total_mismatch", "Base imponible + IVA no coincide con el total.",
                field="total", observed=float(total), expected=float(expected_total),
            ))

    return ValidationReport(tuple(issues), line_sum, auto_confirmable=not issues)
