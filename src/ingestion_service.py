"""Orquestación production v1: evidencia -> candidato -> revisión -> confirmación.

Nada extraído por IA llega a las tablas contables hasta que `confirm_albaran_v1`
lo publica de forma transaccional e idempotente.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from mistralai.client.sdk import Mistral

from . import supabase_client as db
from .accounting_validation import ValidationIssue, ValidationReport, validate_candidate
from .albaran_processor import (
    AlbaranLLM,
    _EXTRACTION_SYSTEM_PROMPT,
    _MODELO_LLM,
    _MODELO_OCR,
    _parse_json_robusto,
    _resolver_precio_neto,
    _verificar_blacklist,
)
from .config import settings
from .spanish_tax_id import is_valid_spanish_tax_id, normalize_tax_id

logger = logging.getLogger(__name__)

PROMPT_VERSION = "delivery-note-v2.1.0"
CLASSIFIER_PROMPT_VERSION = "document-quality-v1.1.0"


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    pages: int | None = None
    request_id: str | None = None


@dataclass(frozen=True)
class OCRResult:
    text: str
    raw: dict[str, Any]
    confidence: float | None
    usage: Usage
    duration_ms: int


@dataclass(frozen=True)
class Classification:
    document_type: str
    handwritten: bool | None
    quality: int | None
    confidence: int | None
    reason: str
    raw: dict[str, Any]
    usage: Usage
    duration_ms: int


@dataclass(frozen=True)
class CandidateResult:
    ingestion_id: str
    candidate_artifact_id: str
    candidate: dict[str, Any]
    validation: ValidationReport
    classification: Classification
    probable_duplicate: dict[str, Any] | None
    open_review_count: int

    @property
    def needs_review(self) -> bool:
        # La lista durable de revisiones es la fuente de verdad. En producción un
        # candidato limpio también recibe `human_confirmation_required`; mirar
        # solo la validación contable haría que el worker intentase autoconfirmarlo.
        return self.open_review_count > 0


class BillableExtractionError(ValueError):
    """La API respondió con uso medible, pero el payload no pudo interpretarse."""

    def __init__(self, message: str, *, usage: Usage, duration_ms: int, raw_text: str):
        super().__init__(message)
        self.usage = usage
        self.duration_ms = duration_ms
        self.raw_text = raw_text


class TerminalDocumentRejected(ValueError):
    """El documento se procesó correctamente, pero no es un albarán admisible."""

    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        self.reason = reason


def _dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return value
    return {}


def _usage(response: Any) -> Usage:
    raw = getattr(response, "usage", None) or getattr(response, "usage_info", None)
    return Usage(
        input_tokens=getattr(raw, "prompt_tokens", None) or getattr(raw, "input_tokens", None),
        output_tokens=getattr(raw, "completion_tokens", None) or getattr(raw, "output_tokens", None),
        pages=getattr(raw, "pages_processed", None) or getattr(raw, "pages", None),
        request_id=(
            str(getattr(response, "id", None) or getattr(response, "request_id", None) or "")
            or None
        ),
    )


def _confidence_values(value: Any) -> list[float]:
    """Extrae scores de respuesta OCR sin acoplarse a una versión concreta del SDK."""
    result: list[float] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"confidence", "confidence_score", "score"} and isinstance(child, (int, float)):
                score = float(child)
                if 0 <= score <= 1:
                    result.append(score)
            else:
                result.extend(_confidence_values(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(_confidence_values(child))
    return result


def _page_text(page: Any, raw_page: dict[str, Any] | None = None) -> str:
    """Devuelve el markdown con cualquier tabla separada insertada en su lugar.

    OCR 4 reemplaza las tablas por enlaces ``tbl-N.html`` cuando se solicita un
    formato separado. Enviar esos enlaces al LLM elimina precisamente cantidades,
    precios e IVA. Aunque producción usa tablas inline, se mantiene este soporte
    para respuestas/versiones del proveedor que las devuelvan por separado.
    """
    markdown = str(getattr(page, "markdown", "") or "")
    page_data = raw_page if isinstance(raw_page, dict) else {}
    for table in page_data.get("tables") or []:
        if not isinstance(table, dict):
            continue
        table_id = str(table.get("id") or "").strip()
        content = str(table.get("content") or "").strip()
        if table_id and content:
            markdown = markdown.replace(f"[{table_id}]({table_id})", content)
    return markdown


async def _ocr(image_bytes: bytes, client: Mistral, content_type: str = "image/jpeg") -> OCRResult:
    started = time.perf_counter()
    response = await asyncio.wait_for(
        client.ocr.process_async(
            model=_MODELO_OCR,
            document={
                "type": "image_url",
                "image_url": f"data:{content_type};base64," + base64.b64encode(image_bytes).decode(),
            },
            confidence_scores_granularity="word",
            include_blocks=True,
            # None conserva las tablas inline. Con "html"/"markdown", OCR 4
            # devuelve placeholders y el contenido en page.tables.
            table_format=None,
        ),
        timeout=90,
    )
    raw = _dump(response)
    pages = getattr(response, "pages", None) or []
    raw_pages = raw.get("pages") if isinstance(raw.get("pages"), list) else []
    text = "\n\n".join(
        _page_text(page, raw_pages[index] if index < len(raw_pages) else None)
        for index, page in enumerate(pages)
    ).strip()
    scores = _confidence_values(raw)
    confidence = sum(scores) / len(scores) if scores else None
    return OCRResult(
        text=text,
        raw=raw,
        confidence=confidence,
        usage=Usage(
            input_tokens=_usage(response).input_tokens,
            output_tokens=_usage(response).output_tokens,
            pages=_usage(response).pages or max(1, len(pages)),
            request_id=_usage(response).request_id,
        ),
        duration_ms=int((time.perf_counter() - started) * 1000),
    )


def _ocr_desde_artefacto(artifact: dict[str, Any]) -> OCRResult:
    """Reconstruye el resultado de OCR guardado sin volver a llamar al proveedor."""
    payload = artifact.get("payload") or {}
    confidence = payload.get("confidence")
    return OCRResult(
        text=str(payload.get("text") or ""),
        raw=payload.get("response") if isinstance(payload.get("response"), dict) else {},
        confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
        # Sin llamada no hay tokens ni páginas que facturar: dejar el uso vacío es
        # lo que impide que el ledger registre un coste que nadie ha pagado.
        usage=Usage(),
        duration_ms=0,
    )


async def _classify(
    image_bytes: bytes, client: Mistral, content_type: str = "image/jpeg"
) -> Classification:
    prompt = (
        "Clasifica visualmente este documento. Responde solo JSON con: "
        '{"document_type":"delivery_note|invoice|receipt|payroll|utility|other",'
        '"handwritten":true|false,"quality":0-100,"confidence":0-100,"reason":"breve"}. '
        "handwritten=true solo si cantidades, descripciones o importes relevantes están escritos a mano, "
        "aunque la plantilla sea impresa. Una firma, una marca aislada, el papel torcido, texto de impresora "
        "matricial o una anotación fuera de los campos económicos NO convierten el documento en manuscrito."
    )
    started = time.perf_counter()
    try:
        response = await asyncio.wait_for(
            client.chat.complete_async(
                model=_MODELO_LLM,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {
                            "url": f"data:{content_type};base64," + base64.b64encode(image_bytes).decode()
                        }},
                        {"type": "text", "text": prompt},
                    ],
                }],
                response_format={"type": "json_object"},
                temperature=0,
                max_tokens=180,
            ),
            timeout=60,
        )
    except Exception as exc:
        logger.warning("No se pudo clasificar visualmente el documento: %s", exc)
        return Classification(
            "unknown", None, None, None, "clasificación no disponible",
            {"outcome": "provider_error"}, Usage(),
            int((time.perf_counter() - started) * 1000),
        )

    usage = _usage(response)
    duration_ms = int((time.perf_counter() - started) * 1000)
    try:
        content = response.choices[0].message.content
        raw = json.loads(content if isinstance(content, str) else str(content))
        doc_type = str(raw.get("document_type", "other"))
        handwritten = raw.get("handwritten") if isinstance(raw.get("handwritten"), bool) else None
        quality = int(raw["quality"]) if isinstance(raw.get("quality"), (int, float)) else None
        confidence = int(raw["confidence"]) if isinstance(raw.get("confidence"), (int, float)) else None
        return Classification(
            doc_type, handwritten, quality, confidence, str(raw.get("reason", "")), raw,
            usage, duration_ms,
        )
    except Exception as exc:
        logger.warning("La clasificación facturable devolvió un payload inválido: %s", exc)
        return Classification(
            "unknown", None, None, None, "clasificación no interpretable",
            {"outcome": "parse_error"}, usage, duration_ms,
        )


def _llm_cost(usage: Usage) -> float:
    return round(
        (usage.input_tokens or 0) * settings.LLM_INPUT_USD_PER_MILLION_TOKENS / 1_000_000
        + (usage.output_tokens or 0) * settings.LLM_OUTPUT_USD_PER_MILLION_TOKENS / 1_000_000,
        8,
    )


async def _record_usage_safely(
    *, ingestion_id: str | None, user_id: int | None, operation: str, model: str,
    usage: Usage, duration_ms: int, metadata: dict[str, Any] | None = None,
    retries: int = 0,
) -> float:
    if operation == "ocr":
        cost = round((usage.pages or 1) * settings.OCR_USD_PER_1000_PAGES / 1000, 8)
        page_price = settings.OCR_USD_PER_1000_PAGES / 1000
    else:
        cost = _llm_cost(usage)
        page_price = None
    if not (usage.input_tokens or usage.output_tokens or usage.pages or cost):
        return 0.0
    # La contabilidad de uso es parte del resultado de la llamada, no telemetría
    # opcional: si no puede persistirse, el job falla de forma visible.
    try:
        await db.registrar_uso_ai(
            operation=operation, model=model, cost_usd=cost,
            ingestion_id=ingestion_id, user_id=user_id,
            request_id=usage.request_id,
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            pages=usage.pages, retries=retries,
            input_unit_price=settings.LLM_INPUT_USD_PER_MILLION_TOKENS / 1_000_000,
            output_unit_price=settings.LLM_OUTPUT_USD_PER_MILLION_TOKENS / 1_000_000,
            page_unit_price=page_price,
            metadata={"duration_ms": duration_ms, **(metadata or {})},
        )
    except Exception:
        logger.exception(
            "Coste AI no persistido operation=%s model=%s request_id=%s "
            "input_tokens=%s output_tokens=%s pages=%s cost_usd=%.8f",
            operation, model, usage.request_id, usage.input_tokens,
            usage.output_tokens, usage.pages, cost,
        )
        raise
    return cost


async def _extract(ocr_text: str, client: Mistral) -> tuple[dict[str, Any], Usage, int]:
    started = time.perf_counter()
    response = await asyncio.wait_for(
        client.chat.complete_async(
            model=_MODELO_LLM,
            messages=[
                {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": f"Texto OCR del albarán:\n\n{ocr_text}"},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=8192,
        ),
        timeout=90,
    )
    usage = _usage(response)
    duration_ms = int((time.perf_counter() - started) * 1000)
    content = response.choices[0].message.content
    raw_text = content if isinstance(content, str) else str(content)
    try:
        parsed = _parse_json_robusto(raw_text)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise BillableExtractionError(
            "La extracción devolvió JSON no interpretable", usage=usage,
            duration_ms=duration_ms, raw_text=raw_text,
        ) from exc
    return parsed, usage, duration_ms


def _candidate_payload(
    model: AlbaranLLM, observed: dict[str, Any], ocr_text: str = "",
) -> dict[str, Any]:
    extracted_nif = model.proveedor_nif
    normalized_nif = normalize_tax_id(extracted_nif)
    is_customer_nif = normalized_nif in settings.customer_nifs_set
    valid_nif = is_valid_spanish_tax_id(extracted_nif)
    accepted_nif = extracted_nif if valid_nif and not is_customer_nif else None
    header = {
        "proveedor_nombre": model.proveedor_nombre,
        "proveedor_nif": accepted_nif,
        "proveedor_direccion": model.proveedor_direccion,
        "proveedor_telefono": model.proveedor_telefono,
        "proveedor_email": model.proveedor_email,
        "numero_albaran": model.numero_albaran,
        "fecha": model.fecha,
        "forma_pago": model.forma_pago,
        "base_imponible": model.base_imponible,
        "total_iva": model.total_iva,
        "total": model.total,
        "detalle_iva": [item.model_dump(mode="json") for item in model.detalle_iva or []],
        "origen": "ocr",
        "decisiones": {
            "proveedor_nif": {
                "observed": extracted_nif,
                "accepted": accepted_nif,
                "rule": (
                    "customer-nif-exclusion" if is_customer_nif
                    else "invalid-check-digit" if extracted_nif and not valid_nif
                    else "validated-observation" if accepted_nif else "not-observed"
                ),
            }
        },
    }
    observed_lines = observed.get("lineas") if isinstance(observed.get("lineas"), list) else []
    # Las filas de catálogo descartadas dejan huecos: hay que volver a la posición
    # que la línea ocupaba en la respuesta original, no a la que ocupa ahora.
    posiciones = getattr(model, "indices_conservados", None) or list(range(len(model.lineas)))
    lines: list[dict[str, Any]] = []
    for index, line in enumerate(model.lineas):
        origen = posiciones[index] if index < len(posiciones) else index
        raw_line = observed_lines[origen] if origen < len(observed_lines) else {}
        accepted = line.model_dump(mode="json", exclude={"precio_tarifa", "precio_neto"})
        accepted["descripcion_original"] = line.descripcion_original or line.nombre_producto
        accepted["descripcion_limpia"] = line.nombre_producto
        accepted["valores_observados"] = raw_line
        accepted["valores_calculados"] = {
            "precio_neto_resuelto": line.precio_unitario,
            "importe_resuelto": line.importe_neto,
        }
        accepted["decisiones"] = {"rule": "net-price-v2", "requires_human_acceptance": True}
        lines.append(accepted)
    _derivar_totales_ausentes(header, lines, ocr_text)
    # Qué cifras del pie están REALMENTE escritas en la foto. La revisión no
    # tiene el OCR delante, y sin este apunte no puede distinguir un total
    # impreso —un hecho del papel, y el mejor árbitro que hay para cuadrar un
    # albarán— de uno que hemos calculado nosotros sumando líneas.
    header.setdefault("decisiones", {})["impresos"] = {
        campo: _amount_is_visible(header.get(campo), ocr_text)
        for campo in ("base_imponible", "total_iva", "total")
    }
    if getattr(model, "lineas_descartadas", 0):
        header.setdefault("decisiones", {})["lineas"] = {
            "rule": "filas-de-catalogo-descartadas",
            "descartadas": model.lineas_descartadas,
            "motivo": "filas impresas del formulario sin cantidad, precio ni importe",
        }
    return {"header": header, "lines": lines, "observed": observed}


_MESES_ES = {
    1: "enero", 2: "febrero", 3: "marzo", 4: "abril", 5: "mayo", 6: "junio",
    7: "julio", 8: "agosto", 9: "septiembre", 10: "octubre", 11: "noviembre",
    12: "diciembre",
}


def _fecha_observada(fecha_iso: str | None, ocr_text: str) -> bool:
    """¿Aparece esa fecha escrita en el documento, en algún formato habitual?"""
    if not fecha_iso:
        return False
    try:
        anio, mes, dia = (int(parte) for parte in str(fecha_iso).split("-"))
        mes_nombre = _MESES_ES[mes]
    except (ValueError, KeyError):
        return False
    variantes: set[str] = set()
    for sep in ("/", "-", ".", " "):
        for dd, mm in ((f"{dia:02d}", f"{mes:02d}"), (str(dia), str(mes))):
            variantes.add(f"{dd}{sep}{mm}{sep}{anio}")
            variantes.add(f"{dd}{sep}{mm}{sep}{anio % 100:02d}")
            variantes.add(f"{anio}{sep}{mm}{sep}{dd}")
    variantes.update({
        f"{dia} de {mes_nombre} de {anio}", f"{dia} de {mes_nombre}", f"{dia} {mes_nombre} {anio}",
    })
    plano = re.sub(r"\s+", "", ocr_text).lower()
    return any(re.sub(r"\s+", "", v).lower() in plano for v in variantes)


def _descartar_fecha_no_observada(model: AlbaranLLM, ocr_text: str) -> str | None:
    """Vacía la fecha cuando no está escrita en el documento, y devuelve la descartada.

    En un albarán manuscrito cuyo pie decía "de 6 de 202" (con el año cortado) el
    modelo devolvió 06/01/2024: se inventó mes y año. Nadie lo detectaba, porque
    el control de "esto no aparece en la foto" solo cubría base, IVA y total.

    Una fecha equivocada no salta a la vista y sin embargo descuadra los informes
    de gasto por meses, que es justo lo que se consulta por Telegram. Es mejor no
    tener fecha y preguntarla —se responde mirando el papel, o es la de hoy— que
    arrastrar un dato falso con apariencia de leído.
    """
    if not model.fecha or _fecha_observada(model.fecha, ocr_text):
        return None
    descartada = model.fecha
    model.fecha = None
    return descartada


def _derivar_totales_en_modelo(model: AlbaranLLM, ocr_text: str = "") -> dict[str, Any] | None:
    """Aplica la derivación de totales sobre el modelo antes de validarlo.

    Devuelve la anotación de `decisiones` si hubo cálculo, para que el candidato
    la conserve y la revisión pueda avisar de que el total lo pusimos nosotros.
    """
    header: dict[str, Any] = {
        "base_imponible": model.base_imponible,
        "total_iva": model.total_iva,
        "total": model.total,
    }
    _derivar_totales_ausentes(
        header, [{"importe_neto": linea.importe_neto} for linea in model.lineas], ocr_text
    )
    decision = (header.get("decisiones") or {}).get("totales")
    if decision and decision.get("base_descartada") is not None:
        # Si la base era inventada, el IVA que la acompaña lo es igual: en un
        # albarán sin tabla de impuestos el modelo llegó a fabricar un tramo
        # entero (21% sobre 1.138,46 = 216,31 €) sin que ninguna de esas cifras
        # estuviera en el documento. Quedarse solo con la base calculada dejaba
        # un híbrido peor que cualquiera de las dos versiones.
        iva_inventado = model.total_iva is not None and not _amount_is_visible(
            model.total_iva, ocr_text
        )
        tramos_inventados = all(
            not _amount_is_visible(tramo.base, ocr_text)
            and not _amount_is_visible(tramo.cuota, ocr_text)
            for tramo in (model.detalle_iva or [])
        )
        if iva_inventado and tramos_inventados:
            decision["iva_descartado"] = model.total_iva
            model.total_iva = None
            model.detalle_iva = None
            header["total_iva"] = None
            header["total"] = header.get("base_imponible")
    model.base_imponible = header.get("base_imponible")
    model.total = header.get("total")
    return decision


def _derivar_totales_ausentes(
    header: dict[str, Any], lines: list[dict[str, Any]], ocr_text: str = "",
) -> None:
    """Calcula base y total cuando el albarán no los imprime, mutando la cabecera.

    Hay albaranes que solo traen la tabla de productos y terminan sin totales.
    Pedirle la suma al modelo daba un número distinto en cada pasada (un mismo
    documento devolvió 1.183, 1.194, 1.197 y 1.034 € siendo 1.000,14 €), porque
    sumar decenas de importes de cabeza no es algo que un LLM haga de forma
    fiable. Aquí la suma es aritmética exacta y siempre da lo mismo.

    Cubre dos casos:
      - el modelo dejó los totales vacíos (lo que se le pide cuando no están
        impresos): se rellenan con la suma;
      - el modelo devolvió un total que NO aparece en el documento y que además
        no coincide con la suma de las líneas: es una suma inventada, así que se
        sustituye. El prompt se lo prohíbe, pero reincide de vez en cuando y un
        total inventado corrompe la contabilidad en silencio.

    Nunca toca un importe que sí esté impreso en el albarán: eso es un hecho, y
    si no cuadra con las líneas debe verlo una persona, no taparlo el sistema.
    """
    importes = [line.get("importe_neto") for line in lines]
    if not importes or any(importe is None for importe in importes):
        return  # con una sola línea incompleta, la suma engañaría; mejor sin dato
    base_declarada = header.get("base_imponible")
    total_declarado = header.get("total")
    base = round(sum(float(importe) for importe in importes), 2)

    coincide_con_las_lineas = (
        base_declarada is not None and abs(float(base_declarada) - base) <= 0.02
    )
    ninguna_visible = (
        not _amount_is_visible(base_declarada, ocr_text)
        and (total_declarado is None or not _amount_is_visible(total_declarado, ocr_text))
    )
    if base_declarada is None and total_declarado is None:
        motivo = "el albarán no imprime base ni total"
    elif base_declarada is not None and ninguna_visible and not coincide_con_las_lineas:
        motivo = "el total no aparece en el documento y no cuadraba con las líneas"
    elif base_declarada is not None and ninguna_visible and coincide_con_las_lineas:
        # El número es correcto —es exactamente la suma de las líneas— pero NO
        # está escrito en el papel: la casilla de totales viene en blanco y lo
        # calculó el modelo. Antes esto se colaba como cifra impresa, porque solo
        # se dejaba constancia cuando había que CAMBIAR el valor.
        #
        # Confundir "calculado por nosotros" con "impreso en el albarán" tiene
        # consecuencias: al corregir después una línea, el total no se recalcula
        # (creemos que es un hecho del papel) y el albarán se bloquea acusando al
        # documento de declarar una cifra que nunca declaró.
        header.setdefault("decisiones", {})["totales"] = {
            "rule": "sumado-de-lineas",
            "motivo": "el albarán no imprime los totales; la cifra coincide con la suma",
            "base_calculada": base,
            "total_calculado": header.get("total"),
            "lineas_sumadas": len(importes),
        }
        return
    else:
        return

    iva = header.get("total_iva")
    total = round(base + float(iva), 2) if iva is not None else base
    header["base_imponible"] = base
    header["total"] = total
    header.setdefault("decisiones", {})["totales"] = {
        "rule": "sumado-de-lineas",
        "motivo": motivo,
        "base_descartada": base_declarada,
        "base_calculada": base,
        "total_calculado": total,
        "lineas_sumadas": len(importes),
    }


def _provenance_issues(candidate: dict[str, Any]) -> list[ValidationIssue]:
    """Hace visibles los valores derivados; nunca deben parecer transcritos."""
    issues: list[ValidationIssue] = []
    for index, line in enumerate(candidate["lines"], start=1):
        raw = line.get("valores_observados") or {}
        if raw.get("importe_neto") in (None, "") and line.get("importe_neto") is not None:
            issues.append(ValidationIssue(
                "line_amount_derived", "El importe no estaba observado y fue calculado.",
                severity="warning", field="importe_neto", line_index=index,
                observed=None, expected=line.get("importe_neto"),
            ))
        elif raw.get("importe_neto") not in (None, "") and line.get("importe_neto") is not None:
            try:
                amount_changed = abs(float(raw["importe_neto"]) - float(line["importe_neto"])) > 0.01
            except (TypeError, ValueError):
                amount_changed = True
            if amount_changed:
                issues.append(ValidationIssue(
                    "line_amount_adjusted", "El importe aceptado difiere del valor extraído.",
                    severity="warning", field="importe_neto", line_index=index,
                    observed=raw.get("importe_neto"), expected=line.get("importe_neto"),
                ))
        raw_quantity = raw.get("cantidad")
        if raw_quantity not in (None, "") and line.get("cantidad") is not None:
            try:
                changed = abs(float(raw_quantity) - float(line["cantidad"])) > 0.001
            except (TypeError, ValueError):
                changed = True
            if changed:
                issues.append(ValidationIssue(
                    "line_quantity_adjusted", "La cantidad aceptada fue ajustada por una regla de peso.",
                    severity="warning", field="cantidad", line_index=index,
                    observed=raw_quantity, expected=line.get("cantidad"),
                ))
        if (
            raw.get("precio_neto") in (None, "")
            and raw.get("precio_tarifa") not in (None, "")
            and raw.get("descuento_pct") not in (None, "", 0, 0.0)
            and line.get("precio_unitario") is not None
        ):
            issues.append(ValidationIssue(
                "line_price_derived", "El precio neto fue calculado desde tarifa y descuento.",
                severity="warning", field="precio_unitario", line_index=index,
                observed={"tarifa": raw.get("precio_tarifa"), "descuento": raw.get("descuento_pct")},
                expected=line.get("precio_unitario"),
            ))
        elif raw.get("precio_unitario") not in (None, "") and line.get("precio_unitario") is not None:
            try:
                price_changed = abs(
                    float(raw["precio_unitario"]) - float(line["precio_unitario"])
                ) > 0.0001
            except (TypeError, ValueError):
                price_changed = True
            if price_changed:
                issues.append(ValidationIssue(
                    "line_price_adjusted", "El precio neto aceptado difiere del valor extraído.",
                    severity="warning", field="precio_unitario", line_index=index,
                    observed=raw.get("precio_unitario"), expected=line.get("precio_unitario"),
                ))
    return issues


def _amount_is_visible(value: Any, ocr_text: str) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    variants = {
        f"{number:.2f}", f"{number:.2f}".replace(".", ","),
        f"{number:g}", f"{number:g}".replace(".", ","),
        # Con separador de miles: "1000.14" se imprime "1.000,14" o "1,000.14".
        # Sin estas variantes, un total impreso a partir de 1.000 € se marcaba
        # como "no observado" y ensuciaba la revisión con un aviso falso.
        f"{number:,.2f}", f"{number:,.2f}".replace(",", "."),
        f"{number:,.2f}".translate(str.maketrans({",": ".", ".": ","})),
    }
    compact = ocr_text.replace(" ", "")
    return any(variant in compact for variant in variants)


def _header_provenance_issues(candidate: dict[str, Any], ocr_text: str) -> list[ValidationIssue]:
    """Marca cifras de cabecera no observadas; pueden ser cálculos, nunca hechos."""
    issues: list[ValidationIssue] = []
    header = candidate.get("header") or {}
    nif_decision = (header.get("decisiones") or {}).get("proveedor_nif") or {}
    if nif_decision.get("rule") == "invalid-check-digit":
        issues.append(ValidationIssue(
            "supplier_tax_id_invalid",
            "El NIF/CIF leído no supera el dígito de control y no se guardará.",
            severity="warning", field="proveedor_nif",
            observed=nif_decision.get("observed"), expected=None,
        ))
    totales = (header.get("decisiones") or {}).get("totales") or {}
    if totales.get("rule") == "sumado-de-lineas":
        # Sabemos con certeza que lo calculamos nosotros: se dice así, en vez de
        # dejar el aviso genérico de "no aparece en el OCR", que suena a sospecha.
        issues.append(ValidationIssue(
            "totales_sumados_de_lineas",
            "El albarán no trae totales impresos; se han sumado las líneas.",
            severity="warning", field="total",
            observed=None, expected=totales.get("total_calculado"),
        ))
    for field in ("base_imponible", "total_iva", "total"):
        if totales.get("rule") == "sumado-de-lineas" and field in ("base_imponible", "total"):
            continue  # ya explicado arriba; no repetir el aviso genérico
        value = header.get(field)
        if value is not None and not _amount_is_visible(value, ocr_text):
            issues.append(ValidationIssue(
                "header_value_not_observed",
                f"{field} no aparece literalmente en el OCR; puede ser un cálculo o una inferencia.",
                severity="warning", field=field, observed=None, expected=value,
            ))
    return issues


async def _find_probable_duplicate(
    candidate: dict[str, Any], *, perceptual_hash: str | None = None,
    exclude_ingestion_id: str | None = None,
) -> dict[str, Any] | None:
    header = candidate["header"]
    target_date = header.get("fecha")
    target_total = header.get("total")
    target_supplier = str(header.get("proveedor_nombre") or "").strip().casefold()
    if target_date and target_total is not None and target_supplier:
        client = await db.get_client()
        res = await (
            client.table("albaranes")
            .select("id,numero_albaran,fecha,total,proveedores(nombre)")
            .eq("fecha", target_date)
            .gte("total", float(target_total) - 0.50)
            .lte("total", float(target_total) + 0.50)
            .eq("status", "confirmed")
            .limit(20)
            .execute()
        )
        for row in db._safe_data(res, many=True):
            supplier = str((row.get("proveedores") or {}).get("nombre") or "").strip().casefold()
            if supplier == target_supplier:
                return row
    if perceptual_hash and exclude_ingestion_id:
        visual = await db.buscar_ingestion_similar_perceptual(
            perceptual_hash, exclude_ingestion_id=exclude_ingestion_id
        )
        if visual:
            metadata = visual.get("metadata") or {}
            return {
                "id": visual.get("id"),
                "numero_albaran": metadata.get("number"),
                "fecha": metadata.get("date"),
                "total": metadata.get("total"),
                "proveedores": {"nombre": metadata.get("provider")},
                "match_type": "perceptual_hash",
                "perceptual_distance": visual.get("perceptual_distance"),
            }
    return None


# Avisos de "trazabilidad" (no bloquean confirmar): se replica aquí, no en
# review_service, porque _review_items necesita esta lista para deduplicar
# antes de insertar. review_service importa esta misma constante para no
# mantener dos copias que puedan desincronizarse.
WARNING_REASONS = {
    "human_confirmation_required",
    "handwritten_document",
    "ocr_confidence_missing",
    "ocr_confidence_low",
    "line_confidence_low",
    "line_amount_derived",
    "line_amount_adjusted",
    "line_quantity_adjusted",
    "line_price_derived",
    "line_price_adjusted",
    "header_value_not_observed",
    "totales_sumados_de_lineas",
    "supplier_tax_id_invalid",
}


def _review_items(
    report: ValidationReport, candidate_artifact_id: str,
    probable_duplicate: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    # Dos reglas de validación distintas pueden apuntar al mismo campo — p.ej.
    # "el total no aparece literal en el OCR" (aviso informativo) y "base + IVA
    # no cuadra con el total" (bloqueante) son ambas sobre field=total. La tabla
    # solo admite UN aviso por (entity_type, entity_key, field_name); sin
    # deduplicar aquí, el INSERT del lote choca contra su propia restricción
    # única y el job entero falla. Si dos avisos coinciden en el mismo campo,
    # se queda el que bloquea — nunca se pierde silenciosamente un error real.
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for issue in report.issues:
        entity_type = "line" if issue.line_index is not None else "document"
        entity_key = str(issue.line_index or "header")
        field_name = issue.field or issue.code
        key = (entity_type, entity_key, field_name)
        item = {
            "extraction_artifact_id": candidate_artifact_id,
            "entity_type": entity_type,
            "entity_key": entity_key,
            "field_name": field_name,
            "observed_value": issue.observed,
            "calculated_value": issue.expected,
            "proposed_value": issue.expected,
            "reason_code": issue.code,
            "confidence": None,
            "status": "open",
        }
        existing = by_key.get(key)
        if existing is None or (
            existing["reason_code"] in WARNING_REASONS and issue.code not in WARNING_REASONS
        ):
            by_key[key] = item
    items = list(by_key.values())
    if probable_duplicate:
        items.append({
            "extraction_artifact_id": candidate_artifact_id,
            "entity_type": "document",
            "entity_key": "header",
            "field_name": "duplicate_candidate",
            "observed_value": probable_duplicate,
            "calculated_value": None,
            "proposed_value": None,
            "reason_code": "probable_duplicate",
            "confidence": None,
            "status": "open",
        })
    return items


async def process_ingestion(ingestion_id: str, *, attempt: int = 1) -> CandidateResult:
    ingestion = await db.obtener_ingestion(ingestion_id)
    if not ingestion:
        raise RuntimeError("ingesta no encontrada")
    month_cost = await db.coste_ai_mes_actual()
    if month_cost >= settings.MONTHLY_AI_BUDGET_USD:
        raise RuntimeError(
            f"Presupuesto mensual de IA alcanzado ({month_cost:.2f} USD)"
        )
    image_bytes = await db.descargar_original_privado(
        ingestion["storage_bucket"], ingestion["storage_path"]
    )
    user_id = int(ingestion["telegram_user_id"])
    client = Mistral(api_key=settings.MISTRAL_API_KEY)
    content_type = str(ingestion.get("content_type") or "image/jpeg")
    classification_task = asyncio.create_task(_classify(image_bytes, client, content_type))
    # La foto original no cambia entre intentos, así que su OCR tampoco: repetir
    # la llamada en un reintento paga otra vez por el mismo texto.
    reused_ocr = await db.buscar_artefacto_ocr_reutilizable(ingestion_id)
    try:
        if reused_ocr is not None:
            ocr = _ocr_desde_artefacto(reused_ocr)
            logger.info(
                "OCR reutilizado del intento %s para la ingesta %s",
                reused_ocr.get("attempt"), ingestion_id,
            )
        else:
            ocr = await _ocr(image_bytes, client, content_type)
    except Exception:
        if classification_task.done() and not classification_task.cancelled():
            completed = await asyncio.gather(classification_task, return_exceptions=True)
            classification_result = completed[0]
            if isinstance(classification_result, Classification):
                await _record_usage_safely(
                    ingestion_id=ingestion_id, user_id=user_id, operation="classification",
                    model=_MODELO_LLM, usage=classification_result.usage,
                    duration_ms=classification_result.duration_ms,
                    metadata={
                        "outcome": classification_result.raw.get("outcome", "success"),
                        "attempt": attempt, "ocr_failed": True,
                    },
                    retries=int(attempt > 1),
                )
        else:
            classification_task.cancel()
            await asyncio.gather(classification_task, return_exceptions=True)
        raise
    classification = await classification_task
    ocr_cost = 0.0 if reused_ocr is not None else await _record_usage_safely(
        ingestion_id=ingestion_id, user_id=user_id, operation="ocr",
        model=_MODELO_OCR, usage=ocr.usage, duration_ms=ocr.duration_ms,
        metadata={"attempt": attempt}, retries=int(attempt > 1),
    )
    await _record_usage_safely(
        ingestion_id=ingestion_id, user_id=user_id, operation="classification",
        model=_MODELO_LLM, usage=classification.usage, duration_ms=classification.duration_ms,
        metadata={"outcome": classification.raw.get("outcome", "success"), "attempt": attempt},
        retries=int(attempt > 1),
    )
    if not ocr.text:
        raise ValueError("El OCR no extrajo texto")
    blocked = _verificar_blacklist(ocr.text)
    if blocked or classification.document_type in {"payroll", "utility", "receipt"}:
        rejection_reason = f"document_type:{blocked or classification.document_type}"
        raise TerminalDocumentRejected(
            "El documento no es un albarán de proveedor", reason=rejection_reason
        )

    if reused_ocr is None:
        await db.registrar_artefacto_extraccion(
            ingestion_id=ingestion_id, attempt=attempt, artifact_type="ocr_raw",
            payload={"text": ocr.text, "response": ocr.raw, "confidence": ocr.confidence},
            model_name=_MODELO_OCR, model_version=_MODELO_OCR, pages=ocr.usage.pages,
            duration_ms=ocr.duration_ms, cost_usd=ocr_cost, complete=True,
        )
    try:
        raw, extraction_usage, extraction_ms = await _extract(ocr.text, client)
    except BillableExtractionError as exc:
        extraction_cost = await _record_usage_safely(
            ingestion_id=ingestion_id, user_id=user_id, operation="extraction",
            model=_MODELO_LLM, usage=exc.usage, duration_ms=exc.duration_ms,
            metadata={"outcome": "parse_error", "attempt": attempt},
            retries=int(attempt > 1),
        )
        await db.registrar_artefacto_extraccion(
            ingestion_id=ingestion_id, attempt=attempt, artifact_type="llm_raw",
            payload={"raw_text": exc.raw_text, "parse_error": True},
            model_name=_MODELO_LLM, model_version=_MODELO_LLM,
            prompt_version=PROMPT_VERSION, input_tokens=exc.usage.input_tokens,
            output_tokens=exc.usage.output_tokens, duration_ms=exc.duration_ms,
            cost_usd=extraction_cost, complete=False,
        )
        raise
    extraction_cost = await _record_usage_safely(
        ingestion_id=ingestion_id, user_id=user_id, operation="extraction",
        model=_MODELO_LLM, usage=extraction_usage, duration_ms=extraction_ms,
        metadata={"attempt": attempt}, retries=int(attempt > 1),
    )
    extraction_complete = bool(raw.pop("_extraction_complete", True))
    observed = copy.deepcopy(raw)
    await db.registrar_artefacto_extraccion(
        ingestion_id=ingestion_id, attempt=attempt, artifact_type="llm_raw",
        payload=observed, model_name=_MODELO_LLM, model_version=_MODELO_LLM,
        prompt_version=PROMPT_VERSION, input_tokens=extraction_usage.input_tokens,
        output_tokens=extraction_usage.output_tokens, duration_ms=extraction_ms,
        cost_usd=extraction_cost,
        complete=extraction_complete,
    )

    model = AlbaranLLM.model_validate(raw)
    for line in model.lineas:
        _resolver_precio_neto(line)
    # Antes de validar, no después: la validación mira el modelo, así que unos
    # totales derivados solo en el candidato dejaban saltar "falta la base" y
    # "falta el total" con los dos valores ya calculados delante.
    _derivar_totales_en_modelo(model, ocr.text)
    fecha_descartada = _descartar_fecha_no_observada(model, ocr.text)
    candidate = _candidate_payload(model, observed, ocr.text)
    if fecha_descartada:
        candidate["header"].setdefault("decisiones", {})["fecha"] = {
            "rule": "no-observada-en-el-documento", "descartada": fecha_descartada,
        }
    report = validate_candidate(
        model,
        extraction_complete=extraction_complete,
        document_is_handwritten=classification.handwritten is not False,
        ocr_confidence=ocr.confidence,
    )
    provenance_issues = _provenance_issues(candidate) + _header_provenance_issues(candidate, ocr.text)
    if provenance_issues:
        report = ValidationReport(
            issues=report.issues + tuple(provenance_issues),
            line_sum=report.line_sum,
            auto_confirmable=False,
        )
    candidate_artifact = await db.registrar_artefacto_extraccion(
        ingestion_id=ingestion_id, attempt=attempt, artifact_type="candidate",
        payload=candidate, model_name=_MODELO_LLM, model_version=_MODELO_LLM,
        prompt_version=PROMPT_VERSION, complete=extraction_complete,
    )
    await db.registrar_artefacto_extraccion(
        ingestion_id=ingestion_id, attempt=attempt, artifact_type="validation",
        payload=report.to_dict(), prompt_version=PROMPT_VERSION, complete=True,
    )
    probable_duplicate = await _find_probable_duplicate(
        candidate, perceptual_hash=ingestion.get("perceptual_hash"),
        exclude_ingestion_id=ingestion_id,
    )
    reviews = _review_items(report, candidate_artifact["id"], probable_duplicate)
    if not reviews and not bool(getattr(settings, "AUTO_CONFIRM_CLEAN", False)):
        reviews.append({
            "extraction_artifact_id": candidate_artifact["id"],
            "entity_type": "document",
            "entity_key": "header",
            "field_name": "human_confirmation",
            "observed_value": {"total": model.total, "line_count": len(model.lineas)},
            "calculated_value": None,
            "proposed_value": True,
            "reason_code": "human_confirmation_required",
            "confidence": None,
            "status": "open",
        })
    if reviews:
        await db.reemplazar_revisiones_abiertas(ingestion_id, reviews)
    status = "needs_review" if reviews else "extracted"
    await db.actualizar_ingestion(
        ingestion_id,
        status=status,
        metadata={
            **(ingestion.get("metadata") or {}),
            "candidate_artifact_id": candidate_artifact["id"],
            "provider": model.proveedor_nombre,
            "number": model.numero_albaran,
            "date": model.fecha,
            "total": model.total,
            "review_count": len(reviews),
            "classification": classification.raw,
        },
    )
    await db.registrar_evento_auditoria(
        "ingestion.extracted", ingestion_id=ingestion_id,
        data={"status": status, "review_count": len(reviews), "attempt": attempt},
    )
    return CandidateResult(
        ingestion_id, candidate_artifact["id"], candidate, report, classification,
        probable_duplicate, len(reviews),
    )


async def load_candidate(ingestion_id: str) -> tuple[dict[str, Any], str]:
    ingestion = await db.obtener_ingestion(ingestion_id)
    if not ingestion:
        raise RuntimeError("ingesta no encontrada")
    artifact_id = (ingestion.get("metadata") or {}).get("candidate_artifact_id")
    if not artifact_id:
        raise RuntimeError("candidato no encontrado")
    client = await db.get_client()
    res = await client.table("extraction_artifacts").select("id,payload,complete").eq(
        "id", artifact_id
    ).eq("ingestion_id", ingestion_id).limit(1).execute()
    rows = db._safe_data(res, many=True)
    if not rows or not rows[0].get("complete"):
        raise RuntimeError("candidato incompleto")
    return rows[0]["payload"], rows[0]["id"]


async def confirm_candidate(
    ingestion_id: str, *, actor_id: str, actor_type: str = "telegram_user"
) -> dict[str, Any]:
    ingestion = await db.obtener_ingestion(ingestion_id)
    if not ingestion:
        raise RuntimeError("ingesta no encontrada")
    open_reviews = await db.listar_revisiones_ingestion(ingestion_id, only_open=True)
    if open_reviews:
        raise ValueError("Quedan campos pendientes de revisión")
    candidate, artifact_id = await load_candidate(ingestion_id)
    result = await db.confirmar_albaran_atomico(
        ingestion_id=ingestion_id,
        idempotency_key=ingestion["idempotency_key"],
        actor_type=actor_type,
        actor_id=actor_id,
        albaran=candidate["header"],
        lineas=candidate["lines"],
        extraction_artifact_id=artifact_id,
    )
    return result


def format_candidate_summary(result: CandidateResult) -> str:
    header = result.candidate["header"]
    lines = result.candidate["lines"]
    total = header.get("total")
    total_text = f"{float(total):.2f}€".replace(".", ",") if total is not None else "sin total"
    reasons = sorted({issue.code for issue in result.validation.issues})
    hard_issues = [issue for issue in result.validation.issues if issue.severity != "warning"]
    text = [
        f"Albarán extraído — {header.get('proveedor_nombre') or 'proveedor desconocido'}",
        f"{len(lines)} líneas | {total_text}",
    ]
    if result.probable_duplicate:
        text.append("Posible duplicado: necesito que compares ambos documentos.")
    if hard_issues:
        text.append(f"Revisión necesaria: {len(hard_issues)} comprobaciones que debes corregir.")
        text.append("Usa /revisar para abrir la lista y confirmar o corregir cada campo.")
    elif reasons:
        text.append("Todo cuadra, no hace falta que corrijas nada.")
        text.append("Usa /revisar para comprobar la foto y confirmar con un toque.")
    elif result.needs_review:
        text.append("Todas las validaciones automáticas han pasado; falta la confirmación de un propietario.")
    else:
        text.append("Todas las validaciones automáticas han pasado.")
    text.append(f"Referencia: {result.ingestion_id[:8]}")
    return "\n".join(text)
