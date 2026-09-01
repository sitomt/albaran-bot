"""Revisión durable y corrección de candidatos desde Telegram."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any

from . import supabase_client as db
from .accounting_validation import validate_candidate
from .albaran_processor import _parsear_numero
from .spanish_tax_id import is_valid_spanish_tax_id
from .ingestion_service import (
    PROMPT_VERSION,
    WARNING_REASONS,
    _find_probable_duplicate,
    _review_items,
    load_candidate,
)

HEADER_FIELDS = {
    "total": "total",
    "base": "base_imponible",
    "base_imponible": "base_imponible",
    "iva": "total_iva",
    "total_iva": "total_iva",
    "fecha": "fecha",
    "numero": "numero_albaran",
    "número": "numero_albaran",
    "proveedor": "proveedor_nombre",
    "nif": "proveedor_nif",
}
LINE_FIELDS = {
    "cantidad": "cantidad",
    "precio": "precio_unitario",
    "importe": "importe_neto",
    "descuento": "descuento_pct",
    "nombre": "descripcion_limpia",
    "unidad": "unidad",
}
NUMERIC_FIELDS = {"total", "base_imponible", "total_iva", "cantidad", "precio_unitario", "importe_neto", "descuento_pct"}
_HEADER_ALIASES = {value: key for key, value in HEADER_FIELDS.items() if key == value or key in {"base", "iva", "numero"}}
_LINE_ALIASES = {value: key for key, value in LINE_FIELDS.items()}
_NON_EDITABLE_REASONS = {"extraction_incomplete", "lines_missing"}

_HEADER_FIELD_FRIENDLY = {
    "base_imponible": "la base imponible",
    "total": "el total",
    "total_iva": "el IVA total",
    "fecha": "la fecha",
    "numero_albaran": "el número de albarán",
    "proveedor_nombre": "el nombre del proveedor",
    "proveedor_nif": "el NIF/CIF del proveedor",
}

# Cada aviso "de trazabilidad" (no bloquea confirmar) se traduce a una frase
# humana en vez de mostrar el reason_code y el volcado observado→propuesto:
# quien usa el bot no es programador y no debe tener que interpretar JSON.
_WARNING_LINE_TEMPLATES = {
    "line_confidence_low": "En la línea {n} ({nombre}) hemos leído con menos nitidez de lo normal.",
    "line_amount_derived": (
        "El importe de la línea {n} ({nombre}) no se leía con claridad, "
        "así que lo hemos calculado con la cantidad y el precio."
    ),
    "line_amount_adjusted": (
        "Hemos ajustado el importe de la línea {n} ({nombre}) porque no coincidía "
        "exactamente con lo impreso."
    ),
    "line_quantity_adjusted": (
        "Hemos corregido la cantidad de la línea {n} ({nombre}) porque el peso impreso "
        "no encajaba con el resto de datos."
    ),
    "line_price_derived": (
        "El precio de la línea {n} ({nombre}) no aparecía escrito, así que lo hemos "
        "calculado con la tarifa y el descuento."
    ),
    "line_price_adjusted": (
        "Hemos ajustado el precio de la línea {n} ({nombre}) porque no coincidía "
        "exactamente con lo impreso."
    ),
}
_WARNING_DOCUMENT_FRIENDLY = {
    "human_confirmation_required": "Solo falta tu confirmación como responsable; no hemos detectado ningún error.",
    "handwritten_document": (
        "El documento tiene texto escrito a mano; lo hemos leído, pero conviene que "
        "lo compares bien con la foto."
    ),
    "ocr_confidence_missing": (
        "No hemos podido calcular con seguridad la fiabilidad de la lectura; échale "
        "un vistazo a la foto."
    ),
    "ocr_confidence_low": (
        "La lectura general del documento tiene menos nitidez de lo normal; échale "
        "un vistazo a la foto."
    ),
    "supplier_tax_id_invalid": (
        "El NIF/CIF del proveedor que hemos leído no es válido, así que no lo hemos "
        "guardado. Puedes añadirlo a mano si lo necesitas."
    ),
}


def _friendly_warning_sentences(reviews: list[dict], lines: list[dict]) -> list[str]:
    """Traduce avisos de trazabilidad a frases humanas; nunca expone reason_code
    ni el detalle observado→propuesto — eso es ruido de depuración, no algo que
    quien usa el bot en el restaurante deba interpretar."""
    sentences: list[str] = []
    header_fields: list[str] = []
    for item in reviews:
        reason = str(item.get("reason_code") or "")
        if reason == "header_value_not_observed":
            field = _HEADER_FIELD_FRIENDLY.get(str(item.get("field_name") or ""))
            if field and field not in header_fields:
                header_fields.append(field)
            continue
        if reason in _WARNING_LINE_TEMPLATES and item.get("entity_type") == "line":
            line_no = str(item.get("entity_key") or "")
            nombre = "producto sin nombre"
            if line_no.isdigit():
                index = int(line_no) - 1
                if 0 <= index < len(lines):
                    nombre = lines[index].get("descripcion_limpia") or nombre
            sentences.append(_WARNING_LINE_TEMPLATES[reason].format(n=line_no, nombre=nombre))
            continue
        if reason in _WARNING_DOCUMENT_FRIENDLY:
            sentences.append(_WARNING_DOCUMENT_FRIENDLY[reason])
    if header_fields:
        joined = header_fields[0] if len(header_fields) == 1 else (
            ", ".join(header_fields[:-1]) + " y " + header_fields[-1]
        )
        venia = "venía" if len(header_fields) == 1 else "venían"
        pronombre = "lo hemos" if len(header_fields) == 1 else "los hemos"
        sentences.insert(0, (
            f"{joined[0].upper()}{joined[1:]} no {venia} como una cifra suelta en el "
            f"albarán, así que {pronombre} calculado sumando los productos."
        ))
    return sentences


REASON_LABELS = {
    "supplier_missing": "Falta el proveedor",
    "date_future": "La fecha es futura",
    "date_invalid": "La fecha falta o no es válida",
    "extraction_incomplete": "La extracción llegó incompleta",
    "handwritten_document": "Hay datos manuscritos",
    "ocr_confidence_missing": "El OCR no ofrece una confianza verificable",
    "ocr_confidence_low": "La lectura OCR tiene confianza baja",
    "lines_missing": "No se detectaron productos",
    "line_description_missing": "Falta el nombre del producto",
    "line_quantity_invalid": "La cantidad falta o no es válida",
    "line_price_invalid": "El precio neto falta o no es válido",
    "line_amount_invalid": "El importe falta o no es válido",
    "line_discount_invalid": "El descuento no es válido",
    "line_confidence_low": "La línea tiene confianza baja",
    "line_amount_mismatch": "Cantidad × precio neto no coincide con el importe",
    "base_missing": "Falta la base imponible",
    "total_missing": "Falta el total a pagar",
    "base_lines_mismatch": "La suma de líneas no coincide con la base",
    "vat_detail_invalid": "Un tramo de IVA no es válido",
    "vat_quota_mismatch": "La cuota de IVA no coincide con base × tipo",
    "vat_bases_mismatch": "Los tramos de IVA no suman la base",
    "vat_total_mismatch": "Los tramos no suman el IVA total",
    "document_total_mismatch": "Base + IVA no coincide con el total",
    "line_amount_derived": "El importe fue calculado porque no era legible",
    "line_amount_adjusted": "El importe aceptado difiere del leído",
    "line_quantity_adjusted": "La cantidad fue ajustada por una regla de peso",
    "line_price_derived": "El neto se calculó desde tarifa y descuento",
    "line_price_adjusted": "El precio neto aceptado difiere del leído",
    "header_value_not_observed": "La cifra no aparece literal y puede ser una suma calculada",
    "supplier_tax_id_invalid": "El NIF/CIF leído no supera el dígito de control",
    "human_confirmation_required": "Falta la confirmación de un propietario",
    "probable_duplicate": "Hay otro albarán parecido que debes comparar",
}


def _number(value: Any, decimals: int = 2) -> str:
    if value in (None, ""):
        return "—"
    try:
        rendered = f"{float(value):.{decimals}f}"
        # Solo recortar ceros sobrantes de la parte decimal (si existe un ".");
        # si no, "50" o "100" con decimals=0 perderían ceros del número entero.
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        return rendered.replace(".", ",")
    except (TypeError, ValueError):
        return str(value)


def _pack_breakdown(line: dict[str, Any]) -> str | None:
    """Traduce bultos/peso-por-unidad a una frase clara. La "cantidad" numérica
    de la línea es la base de precio (cajas, tarrinas...), no necesariamente el
    número de piezas o los kilos reales recibidos — sin esta frase esa
    información queda oculta y parece que faltan datos aunque sí se capturaron."""
    try:
        quantity = float(line.get("cantidad"))
    except (TypeError, ValueError):
        return None
    unidades_por_envase = line.get("unidades_por_envase")
    peso_unitario_g = line.get("peso_unitario_g")
    if unidades_por_envase:
        try:
            total_uds = quantity * float(unidades_por_envase)
        except (TypeError, ValueError):
            return None
        pieza = f" de {_number(peso_unitario_g, 0)}g" if peso_unitario_g else ""
        envase = "caja" if quantity == 1 else "cajas"
        return (
            f"↳ {_number(quantity, 0)} {envase} × {_number(unidades_por_envase, 0)} "
            f"uds{pieza} = {_number(total_uds, 0)} uds en total"
        )
    if peso_unitario_g and line.get("unidad") != "kg":
        try:
            peso_total_kg = quantity * float(peso_unitario_g) / 1000.0
        except (TypeError, ValueError):
            return None
        envase = "envase" if quantity == 1 else "envases"
        return (
            f"↳ {_number(quantity, 0)} {envase} de {_number(float(peso_unitario_g) / 1000, 3)} kg "
            f"= {_number(peso_total_kg, 3)} kg en total"
        )
    return None


def _line_summary(index: int, line: dict[str, Any]) -> str:
    observed = line.get("valores_observados") or {}
    tariff = observed.get("precio_tarifa")
    discount = line.get("descuento_pct")
    net = line.get("precio_unitario")
    quantity = line.get("cantidad")
    unit = line.get("unidad") or "ud"
    parts = [f"{index}. {line.get('descripcion_limpia') or 'Producto sin nombre'}"]
    if tariff not in (None, ""):
        pricing = f"tarifa {_number(tariff, 4)}€"
        if discount not in (None, "", 0, 0.0):
            pricing += f" − {_number(discount)}%"
        pricing += f" → neto {_number(net, 4)}€/u"
    else:
        pricing = f"neto {_number(net, 4)}€/u"
        if discount not in (None, "", 0, 0.0):
            pricing += f" (dto. {_number(discount)}%)"
    parts.append(
        f"   {_number(quantity, 3)} {unit} × {pricing} = {_number(line.get('importe_neto'))}€"
    )
    breakdown = _pack_breakdown(line)
    if breakdown:
        parts.append(f"   {breakdown}")
    return "\n".join(parts)


def _tax_id_summary(header: dict[str, Any]) -> str:
    decision = (header.get("decisiones") or {}).get("proveedor_nif") or {}
    observed = decision.get("observed")
    accepted = header.get("proveedor_nif")
    if accepted:
        return f"NIF/CIF proveedor: {accepted}"
    if observed:
        return f"NIF/CIF leído: {observed} (no se guardará hasta corregirlo)"
    return "NIF/CIF proveedor: —"


@dataclass(frozen=True)
class ReviewView:
    ingestion_id: str
    text: str
    can_approve: bool
    probable_duplicate: bool
    candidate_artifact_id: str = ""
    candidate: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)


def _correction_hint(reference: str, reviews: list[dict], hard_reasons: set[str]) -> str | None:
    for item in reviews:
        if str(item.get("reason_code")) not in hard_reasons:
            continue
        field = str(item.get("field_name") or "")
        if item.get("entity_type") == "line":
            alias = _LINE_ALIASES.get(field)
            line_no = str(item.get("entity_key") or "")
            if alias and line_no.isdigit():
                return f"/corregir {reference} linea {line_no} {alias} VALOR_CORRECTO"
        alias = _HEADER_ALIASES.get(field)
        if alias:
            return f"/corregir {reference} {alias} VALOR_CORRECTO"
    return None


async def _owned_ingestion(ingestion_id: str, user_id: int) -> dict:
    from .config import settings
    ingestion = await db.obtener_ingestion(ingestion_id)
    if not ingestion or user_id not in settings.allowed_users:
        raise PermissionError("No puedes revisar este documento")
    return ingestion


async def build_review_view(ingestion_id: str, user_id: int) -> ReviewView:
    ingestion = await _owned_ingestion(ingestion_id, user_id)
    candidate, artifact_id = await load_candidate(ingestion_id)
    reviews = await db.listar_revisiones_ingestion(ingestion_id, only_open=True)
    header = candidate["header"]
    lines = candidate["lines"]
    reasons = {str(item.get("reason_code")) for item in reviews}
    hard_reasons = reasons - WARNING_REASONS - {"probable_duplicate"}
    can_approve = not hard_reasons and "probable_duplicate" not in reasons
    text = [
        f"Revisión {ingestion_id[:8]}",
        f"{header.get('proveedor_nombre') or 'Proveedor desconocido'} | {header.get('fecha') or 'sin fecha'}",
        f"Nº {header.get('numero_albaran') or '—'}",
        _tax_id_summary(header),
        f"Base {_number(header.get('base_imponible'))}€ + IVA {_number(header.get('total_iva'))}€ "
        f"= TOTAL {_number(header.get('total'))}€",
    ]
    if can_approve and reviews:
        text.extend(["", "✅ Todo cuadra, no tienes que corregir nada."])
    text.extend(["", "Líneas:"])
    # Con avisos de por medio, detallamos solo las líneas señaladas y resumimos el resto
    # para no obligar a releer todo el albarán buscando cuál es la que falla.
    line_issue_indices = {
        int(item["entity_key"])
        for item in reviews
        if item.get("entity_type") == "line" and item.get("field_name") != "detalle_iva"
        and str(item.get("entity_key") or "").isdigit()
    }
    compact_lines = len(lines) > 3 and bool(line_issue_indices)
    for index, line in enumerate(lines, start=1):
        if compact_lines and index not in line_issue_indices:
            text.append(
                f"{index}. {line.get('descripcion_limpia') or 'Producto sin nombre'} "
                f"— {_number(line.get('importe_neto'))}€ ✓"
            )
        else:
            text.append(_line_summary(index, line))
    vat_details = header.get("detalle_iva") or []
    if vat_details:
        text.extend(["", "IVA:"])
        for detail in vat_details:
            text.append(
                f"• {_number(detail.get('tipo'))}% sobre {_number(detail.get('base'))}€ "
                f"= {_number(detail.get('cuota'))}€"
            )
    if hard_reasons:
        # Aquí sí hace falta que la persona corrija algo: mantenemos el detalle
        # técnico (reason_code, observado/propuesto) porque sirve para localizar
        # el campo exacto a corregir con /corregir.
        text.extend(["", "⚠️ Hay diferencias que impiden confirmar:"])
        for item in reviews[:12]:
            if str(item.get("reason_code")) not in hard_reasons:
                continue
            detail = ""
            observed = item.get("observed_value")
            calculated = item.get("calculated_value")
            if observed is not None or calculated is not None:
                observed_text = json.dumps(observed, ensure_ascii=False, default=str)[:80]
                calculated_text = json.dumps(calculated, ensure_ascii=False, default=str)[:80]
                detail = f": observado {observed_text} → propuesto {calculated_text}"
            reason = str(item.get("reason_code") or "")
            location = ""
            if item.get("entity_type") == "line" and item.get("entity_key"):
                # Los avisos de IVA reutilizan "entity_type=line" con el índice del
                # tramo, no de un producto: etiquetarlo como "línea N" confundiría
                # con la línea de producto N.
                if item.get("field_name") == "detalle_iva":
                    location = f" [tramo IVA {item['entity_key']}]"
                else:
                    location = f" [línea {item['entity_key']}]"
            text.append(f"• {REASON_LABELS.get(reason, reason)}{location}{detail}")
        if len(reviews) > 12:
            text.append(f"• y {len(reviews) - 12} comprobaciones más")
        text.extend(["", "Hay diferencias que impiden confirmar."])
        hint = _correction_hint(ingestion_id[:8], reviews, hard_reasons)
        if hint:
            text.extend(["Corrige el campo indicado sustituyendo VALOR_CORRECTO:", hint])
        if hard_reasons & _NON_EDITABLE_REASONS or not hint:
            text.append(
                "Si faltan líneas o la foto no se entiende, reintenta el OCR o usa "
                "«Introducir a mano»; se conservará esta misma foto."
            )
    elif reviews:
        # Sin nada que corregir: nada de reason_code ni JSON crudo — solo frases
        # humanas de lo que el bot calculó por su cuenta y por qué no es un problema.
        friendly = _friendly_warning_sentences(reviews, lines)
        if friendly:
            text.extend([
                "",
                "Antes de guardarlo, esto es lo que hemos calculado nosotros mismos "
                "en vez de leerlo directo en la foto (no afecta al resultado):",
            ])
            text.extend(f"• {sentence}" for sentence in friendly)
        text.extend([
            "",
            "Comprueba que el total coincide con la foto y pulsa «✅ Confirmar definitivamente».",
        ])
    return ReviewView(
        ingestion_id, "\n".join(text), can_approve,
        probable_duplicate="probable_duplicate" in reasons,
        candidate_artifact_id=artifact_id, candidate=candidate,
    )


async def approve_all(
    ingestion_id: str, user_id: int, *, expected_artifact_prefix: str | None = None,
) -> dict[str, Any]:
    ingestion = await _owned_ingestion(ingestion_id, user_id)
    view = await build_review_view(ingestion_id, user_id)
    if expected_artifact_prefix and not view.candidate_artifact_id.startswith(
        expected_artifact_prefix.lower()
    ):
        raise ValueError("La revisión ha cambiado; vuelve a abrir el documento")
    if not view.can_approve:
        raise ValueError("Este documento tiene diferencias que debes corregir antes de confirmarlo")
    try:
        return await db.aceptar_y_confirmar_candidato_atomico(
            ingestion_id=ingestion_id,
            candidate_artifact_id=view.candidate_artifact_id,
            idempotency_key=ingestion["idempotency_key"], actor_id=str(user_id),
            albaran=view.candidate["header"], lineas=view.candidate["lines"],
        )
    except Exception:
        # Las decisiones quedan auditadas; el operador puede ver el fallo del commit.
        await db.registrar_evento_auditoria(
            "review.confirmation_failed", ingestion_id=ingestion_id,
            actor_type="telegram_user", actor_id=str(user_id),
        )
        raise


async def reject_ingestion(
    ingestion_id: str, user_id: int, *, as_duplicate: bool = False,
    expected_artifact_prefix: str | None = None,
) -> None:
    ingestion = await _owned_ingestion(ingestion_id, user_id)
    artifact_id = str((ingestion.get("metadata") or {}).get("candidate_artifact_id") or "")
    if not artifact_id:
        raise ValueError("Este documento no tiene un candidato rechazable")
    if expected_artifact_prefix and not artifact_id.startswith(expected_artifact_prefix.lower()):
        raise ValueError("La revisión ha cambiado; vuelve a abrir el documento")
    await db.rechazar_ingestion_atomico(
        ingestion_id=ingestion_id, candidate_artifact_id=artifact_id,
        actor_id=str(user_id), as_duplicate=as_duplicate,
    )


def _set_correction(candidate: dict[str, Any], args: list[str], user_id: int) -> str:
    if len(args) < 2:
        raise ValueError("Falta campo o valor")
    if args[0].lower() == "linea":
        if len(args) < 4 or not args[1].isdigit():
            raise ValueError("Usa: linea N campo valor")
        index = int(args[1]) - 1
        if index < 0 or index >= len(candidate["lines"]):
            raise ValueError("Número de línea fuera de rango")
        field = LINE_FIELDS.get(args[2].lower())
        if not field:
            raise ValueError("Campo de línea no permitido")
        raw_value = " ".join(args[3:]).strip()
        target = candidate["lines"][index]
        label = f"línea {index + 1} {field}"
    else:
        field = HEADER_FIELDS.get(args[0].lower())
        if not field:
            raise ValueError("Campo de cabecera no permitido")
        raw_value = " ".join(args[1:]).strip()
        target = candidate["header"]
        label = field
    if field in NUMERIC_FIELDS:
        value = _parsear_numero(raw_value)
        if value is None or value < 0:
            raise ValueError("El valor numérico no es válido")
    else:
        value = raw_value[:200]
        if not value:
            raise ValueError("El valor está vacío")
        if field == "proveedor_nif" and not is_valid_spanish_tax_id(value):
            raise ValueError("El NIF/CIF no supera el dígito de control")
    previous = target.get(field)
    target[field] = value
    if target is candidate["header"] and field == "proveedor_nif":
        target.setdefault("decisiones", {})["proveedor_nif"] = {
            "source": "human_correction", "actor": str(user_id),
            "previous": previous, "observed": previous, "accepted": value,
            "rule": "human-validated",
        }
    if target is not candidate["header"]:
        decisions = target.setdefault("decisiones", {})
        decisions[field] = {
            "source": "human_correction", "actor": str(user_id),
            "previous": previous, "accepted": value,
        }
    return label


async def correct_candidate(reference: str, user_id: int, correction_args: list[str]) -> ReviewView:
    ingestion = await db.buscar_ingestion_por_referencia(reference, user_id)
    if not ingestion:
        raise ValueError("Referencia no encontrada o ambigua")
    ingestion_id = ingestion["id"]
    candidate, _ = await load_candidate(ingestion_id)
    revised = copy.deepcopy(candidate)
    changed_label = _set_correction(revised, correction_args, user_id)

    combined = {**revised["header"], "lineas": revised["lines"]}
    classification = (ingestion.get("metadata") or {}).get("classification") or {}
    report = validate_candidate(
        combined,
        extraction_complete=True,
        document_is_handwritten=classification.get("handwritten") is not False,
        ocr_confidence=1.0 if classification.get("confidence", 0) >= 85 else None,
    )
    attempt = await db.siguiente_intento_extraccion(ingestion_id)
    artifact = await db.registrar_artefacto_extraccion(
        ingestion_id=ingestion_id, attempt=attempt, artifact_type="candidate",
        payload=revised, prompt_version=PROMPT_VERSION, complete=True,
    )
    await db.resolver_revisiones_abiertas(
        ingestion_id, status="rejected", resolved_by=f"telegram_user:{user_id}",
        note=f"Sustituida por nueva versión tras corregir {changed_label}",
    )
    probable = await _find_probable_duplicate(
        revised, perceptual_hash=ingestion.get("perceptual_hash"),
        exclude_ingestion_id=ingestion_id,
    )
    reviews = _review_items(report, artifact["id"], probable)
    if not reviews:
        reviews.append({
            "extraction_artifact_id": artifact["id"], "entity_type": "document",
            "entity_key": "header", "field_name": "human_confirmation",
            "observed_value": {"changed": changed_label}, "calculated_value": None,
            "proposed_value": True, "reason_code": "human_confirmation_required",
            "confidence": None, "status": "open",
        })
    await db.reemplazar_revisiones_abiertas(ingestion_id, reviews)
    metadata = {**(ingestion.get("metadata") or {}), "candidate_artifact_id": artifact["id"]}
    await db.actualizar_ingestion(ingestion_id, status="needs_review", metadata=metadata)
    await db.registrar_evento_auditoria(
        "candidate.corrected", ingestion_id=ingestion_id,
        actor_type="telegram_user", actor_id=str(user_id), data={"field": changed_label},
    )
    return await build_review_view(ingestion_id, user_id)
