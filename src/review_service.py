"""Revisión durable y corrección de candidatos desde Telegram."""
from __future__ import annotations

import copy
import dataclasses
import json
from dataclasses import dataclass, field
from typing import Any

from . import supabase_client as db
from .accounting_validation import validate_candidate
from .albaran_processor import _normalizar_fecha, _parsear_numero
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
    "totales_sumados_de_lineas": (
        "Este albarán no trae el total escrito, así que lo hemos sumado nosotros "
        "a partir de las líneas. Comprueba que te cuadra."
    ),
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


_CIFRAS_DE_LINEA = {"cantidad", "precio_unitario", "importe_neto"}


def _lineas_corregidas_a_mano(candidate: dict[str, Any]) -> list[str]:
    """Nombres de los productos cuyas cifras ha tocado una persona."""
    nombres = []
    for linea in candidate.get("lines") or []:
        decisiones = linea.get("decisiones") or {}
        if any(
            campo in _CIFRAS_DE_LINEA and isinstance(detalle, dict)
            and detalle.get("source") == "human_correction"
            for campo, detalle in decisiones.items()
        ):
            nombres.append(str(linea.get("descripcion_limpia") or "una línea"))
    return nombres


def _explicacion_descuadre(item: dict, corregidas: list[str] | None = None) -> str | None:
    """Explica un descuadre de totales por la DIFERENCIA, no por el valor a copiar.

    "observado 316,22 → propuesto 314,97" empuja a sustituir la base por la suma
    de líneas, y eso borra cargos legítimos que el albarán cobra aparte (portes,
    envases, un P.V.). Lo que la persona necesita saber es cuánto falta y de qué
    puede ser, para decidir con la foto delante.
    """
    reason = str(item.get("reason_code") or "")
    if reason not in {"base_lines_mismatch", "document_total_mismatch"}:
        return None
    try:
        observado = float(item.get("observed_value"))
        calculado = float(item.get("calculated_value"))
    except (TypeError, ValueError):
        return None
    diferencia = round(observado - calculado, 2)
    if reason == "base_lines_mismatch" and corregidas:
        # El descuadre lo ha abierto la corrección que acaban de hacer, no el
        # OCR. Hablar aquí de "portes o envases" mandaría a inventar un cargo
        # que no existe: lo que hay que mirar es el otro número de esa línea.
        return (
            f"Al corregir {', '.join(f'«{nombre}»' for nombre in corregidas[:2])} "
            f"los productos pasan a sumar {_number(calculado)}€, y el albarán "
            f"declara {_number(observado)}€ de base: bailan {_number(abs(diferencia))}€. "
            "Si la cantidad nueva es la buena, entonces el precio o el total "
            "también están mal leídos: compruébalos en la foto."
        )
    if reason == "base_lines_mismatch":
        if diferencia > 0:
            return (
                f"La base del albarán ({_number(observado)}€) es {_number(abs(diferencia))}€ "
                f"mayor que la suma de los productos ({_number(calculado)}€). "
                "Suele ser un cargo aparte (portes, envases, retornos). "
                "Mira la foto: si ese cargo está, el albarán es correcto."
            )
        return (
            f"Los productos suman {_number(calculado)}€, pero la base del albarán es "
            f"{_number(observado)}€, o sea {_number(abs(diferencia))}€ menos. "
            "Puede que sobre una línea o que alguna esté repetida."
        )
    return (
        f"El total del albarán ({_number(observado)}€) no cuadra con base + IVA "
        f"({_number(calculado)}€); bailan {_number(abs(diferencia))}€. "
        "Comprueba en la foto el total y los tramos de IVA."
    )


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
    "totales_sumados_de_lineas": "El albarán no trae totales impresos; se sumaron las líneas",
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
    open_reviews: list[dict[str, Any]] = field(default_factory=list, repr=False, compare=False)
    # Qué se acaba de cambiar, en una frase. Decir solo "corregido" obliga a
    # releer todo el albarán para comprobar que se tocó lo que se quería tocar,
    # y esconde lo que el cambio arrastró (el importe recalculado, por ejemplo).
    ultimo_cambio: str = ""


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
        _resumen_importes(header),
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
            explicacion = _explicacion_descuadre(item, _lineas_corregidas_a_mano(candidate))
            if explicacion:
                text.append(f"• {explicacion}")
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
        # El encabezado de arriba ya dice que no se puede confirmar; repetirlo aquí
        # solo añadía alarma. Y si hay un botón que lo arregla de un toque, el
        # comando con su sintaxis sobra: se ofrece solo como alternativa escrita.
        atajos = _acciones_para(reviews, candidate)
        hint = _correction_hint(ingestion_id[:8], reviews, hard_reasons)
        if atajos:
            botones = " o ".join(f"«{texto}»" for texto, _ in atajos)
            text.extend(["", f"Puedes resolverlo con el botón {botones} de abajo."])
            if hint:
                text.append(f"O a mano: {hint}")
        elif hint:
            # El comando exacto queda como alternativa para quien lo prefiera,
            # pero lo primero que se lee es el botón: nadie debería tener que
            # aprenderse un nombre de campo para arreglar una cifra mal leída.
            text.extend([
                "",
                "Pulsa «✏️ Corregir un dato» y elige qué está mal; el bot te "
                "pregunta y tú respondes solo con el dato.",
                f"O a mano: {hint}",
            ])
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
        open_reviews=reviews,
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


_ALTA_LINEA = {"añadir", "anadir", "añade", "agregar", "add"}
_BAJA_LINEA = {"borrar", "eliminar", "quitar", "borra"}
_CARGO = {"cargo", "portes", "envases"}
CARGO_SIN_IDENTIFICAR = "Cargo sin identificar"


def _nueva_linea(nombre: str, cantidad: float, precio: float, user_id: int) -> dict[str, Any]:
    """Construye una línea con la misma forma que las del OCR, marcada como humana."""
    return {
        "descripcion_limpia": nombre,
        "descripcion_original": nombre,
        "nombre_producto": nombre,
        "cantidad": cantidad,
        "unidad": "ud",
        "precio_unitario": precio,
        "importe_neto": round(cantidad * precio, 2),
        "descuento_pct": None,
        "confianza": 100,
        "valores_observados": {},
        "valores_calculados": {"importe_resuelto": round(cantidad * precio, 2)},
        "decisiones": {
            "rule": "linea-anadida-a-mano",
            "source": "human_correction",
            "actor": str(user_id),
        },
    }


def _hueco_de_la_base(candidate: dict[str, Any]) -> float:
    """Diferencia entre la base declarada y lo que suman las líneas."""
    header = candidate.get("header") or {}
    base = header.get("base_imponible")
    if base is None:
        return 0.0
    importes = [linea.get("importe_neto") for linea in candidate.get("lines") or []]
    if any(importe is None for importe in importes):
        return 0.0
    return round(float(base) - sum(float(importe) for importe in importes), 2)


def _añadir_linea(candidate: dict[str, Any], args: list[str], user_id: int) -> str:
    """`añadir Tomate, 12, 1,81` — mete un producto que el OCR no vio."""
    from .manual_albaran import _parsear_producto

    texto = " ".join(args).strip()
    parsed = _parsear_producto(texto)
    if parsed is None:
        raise ValueError(
            "Escribe el producto así: añadir NOMBRE, CANTIDAD, PRECIO\n"
            "Por ejemplo: añadir Tomate frito, 3, 8,70"
        )
    nombre, cantidad, precio = parsed
    candidate["lines"].append(_nueva_linea(nombre, cantidad, precio, user_id))
    return f"línea añadida ({nombre})"


def _borrar_linea(candidate: dict[str, Any], args: list[str]) -> str:
    """`borrar linea 3` — quita una línea que el OCR duplicó o inventó."""
    numeros = [a for a in args if a.isdigit()]
    if not numeros:
        raise ValueError("Indica cuál: borrar linea 3")
    index = int(numeros[0]) - 1
    lineas = candidate["lines"]
    if index < 0 or index >= len(lineas):
        raise ValueError(f"Ese albarán tiene {len(lineas)} líneas; no existe la {index + 1}")
    if len(lineas) == 1:
        raise ValueError("No puedo dejar el albarán sin ninguna línea")
    eliminada = lineas.pop(index)
    return f"línea {index + 1} borrada ({eliminada.get('descripcion_limpia') or 'sin nombre'})"


def _añadir_cargo(candidate: dict[str, Any], args: list[str], user_id: int) -> str:
    """Materializa como línea el dinero que falta para cuadrar con la base.

    Cuando la base del albarán supera a la suma de los productos y nadie sabe de
    qué es ese dinero (un porte, unos envases, un cargo manuscrito ilegible), la
    alternativa era bloquear el albarán o falsear la base. Ninguna sirve: la
    primera deja fuera gasto real y la segunda corrompe el total.

    Dándole cuerpo de línea, la contabilidad vuelve a cuadrar sin tocar ni un
    número leído, el importe desconocido queda a la vista con nombre propio y se
    puede identificar más adelante. Es lo que haría un contable: no se tapa un
    descuadre, se le pone una etiqueta.
    """
    hueco = _hueco_de_la_base(candidate)
    importe = _parsear_numero(" ".join(args[1:])) if len(args) > 1 else hueco
    if importe is None or importe <= 0:
        if len(args) <= 1:
            raise ValueError(
                "Las cuentas ya cuadran: los productos suman exactamente la base "
                "del albarán, así que no hace falta ningún cargo."
            )
        raise ValueError("Necesito el importe del cargo, por ejemplo: cargo 1,25")
    nombre = CARGO_SIN_IDENTIFICAR
    candidate["lines"].append({
        **_nueva_linea(nombre, 1.0, round(float(importe), 2), user_id),
        "decisiones": {
            "rule": "cargo-sin-identificar",
            "source": "human_correction",
            "actor": str(user_id),
            "motivo": "la base del albarán no la cubren los productos leídos",
        },
    })
    return f"cargo de {_euros(importe)} añadido"


def _decimal(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _recalcular_linea(linea: dict[str, Any], campo: str) -> str | None:
    """Mantiene cantidad × precio = importe después de corregir uno de los tres.

    Corregir "15,9 kg" por "15,4 kg" y dejar el importe en 43,73 € no arregla
    nada: deja la línea contradiciéndose consigo misma y el albarán bloqueado por
    un descuadre nuevo, provocado por la propia corrección. Quien mira el papel
    corrige el dato que ve mal, no espera tener que recalcular a mano lo que
    depende de él.
    """
    cantidad = _decimal(linea.get("cantidad"))
    precio = _decimal(linea.get("precio_unitario"))
    importe = _decimal(linea.get("importe_neto"))
    if campo in {"cantidad", "precio_unitario"} and cantidad is not None and precio is not None:
        nuevo = round(cantidad * precio, 2)
        if importe is None or abs(nuevo - importe) > 0.005:
            linea["importe_neto"] = nuevo
            linea.setdefault("valores_calculados", {})["importe_resuelto"] = nuevo
            return f"el importe pasa de {_euros(importe)} a {_euros(nuevo)}"
    if campo == "importe_neto" and cantidad and importe is not None:
        nuevo = round(importe / cantidad, 4)
        if precio is None or abs(nuevo - precio) > 0.0005:
            linea["precio_unitario"] = nuevo
            linea.setdefault("valores_calculados", {})["precio_neto_resuelto"] = nuevo
            return f"el precio pasa a {_number(nuevo, 4)}€/u"
    return None


def _recalcular_totales_derivados(candidate: dict[str, Any]) -> str | None:
    """Rehace base y total SOLO si los habíamos calculado nosotros sumando líneas.

    Un importe impreso en el papel es un hecho y no se toca: si tras la
    corrección deja de cuadrar con las líneas, eso es justo lo que una persona
    tiene que ver, no algo que el sistema deba tapar recalculando.
    """
    header = candidate.get("header") or {}
    decision = (header.get("decisiones") or {}).get("totales") or {}
    if decision.get("rule") != "sumado-de-lineas":
        return None
    importes = [_decimal(linea.get("importe_neto")) for linea in candidate.get("lines") or []]
    if not importes or any(importe is None for importe in importes):
        return None
    base = round(sum(importes), 2)
    iva = _decimal(header.get("total_iva"))
    total = round(base + iva, 2) if iva is not None else base
    if base == _decimal(header.get("base_imponible")) and total == _decimal(header.get("total")):
        return None
    header["base_imponible"] = base
    header["total"] = total
    decision.update({"base_calculada": base, "total_calculado": total})
    return f"la base y el total pasan a {_euros(base)} y {_euros(total)}"


def _igualar_base_y_total_sin_iva(
    header: dict[str, Any], campo: str, anterior: Any,
) -> str | None:
    """En un albarán sin IVA, base y total son por definición el mismo número.

    Corregir uno y dejar el otro con el valor viejo transforma un descuadre en
    dos y obliga a escribir dos veces la misma cifra.
    """
    if campo not in {"base_imponible", "total"} or header.get("total_iva"):
        return None
    otro = "total" if campo == "base_imponible" else "base_imponible"
    # Solo si ya iban de la mano: si el papel traía dos cifras distintas, esa
    # diferencia es un dato del documento y no nos toca a nosotros borrarla.
    if header.get(otro) != anterior:
        return None
    header[otro] = header[campo]
    return f"base y total quedan igualados en {_euros(header[campo])}"


def _euros(valor: Any) -> str:
    """Importe con dos decimales para mensajes de dinero ("6,00€", no "6€")."""
    try:
        return f"{float(valor):.2f}".replace(".", ",") + "€"
    except (TypeError, ValueError):
        return "—"


def _resumen_importes(header: dict[str, Any]) -> str:
    """Línea de totales; omite el IVA cuando el albarán no repercute ninguno."""
    base = header.get("base_imponible")
    iva = header.get("total_iva")
    total = header.get("total")
    if not iva:
        return f"Base {_number(base)}€ = TOTAL {_number(total)}€ (sin IVA)"
    return f"Base {_number(base)}€ + IVA {_number(iva)}€ = TOTAL {_number(total)}€"


# ── Corrección guiada de un dato suelto ──────────────────────────────────────
# Corregir una cifra mal leída era, con diferencia, lo que más se hace y lo más
# incómodo de hacer: había que localizar la referencia del documento, saber el
# nombre interno del campo y escribir /corregir con la sintaxis exacta. Todo eso
# es trabajo de ordenador, no de la persona que tiene el albarán en la mano.
#
# Lo que sigue prepara el material para preguntarlo a botones: qué se puede
# corregir, cuánto vale ahora cada cosa y qué frase usar para pedir el valor
# nuevo. El bot solo tiene que pintar botones y esperar un número suelto.

_ETIQUETA_CAMPO = {
    "cantidad": "cantidad",
    "precio_unitario": "precio",
    "importe_neto": "importe",
    "descripcion_limpia": "nombre",
    "unidad": "unidad",
    "descuento_pct": "descuento",
    "fecha": "fecha",
    "numero_albaran": "nº de albarán",
    "proveedor_nombre": "proveedor",
    "proveedor_nif": "NIF/CIF",
    "base_imponible": "base imponible",
    "total_iva": "IVA",
    "total": "total",
}

# Orden de aparición en el teclado: primero lo que casi siempre falla al leer
# números escritos a mano, y el nombre al final porque casi nunca se toca.
_CAMPOS_DE_LINEA = ("cantidad", "precio", "importe", "nombre")
_CAMPOS_DE_CABECERA = ("fecha", "numero", "proveedor", "total", "base", "iva")


def _formatear_valor(valor: Any) -> str:
    if valor in (None, ""):
        return "vacío"
    if isinstance(valor, (int, float)):
        return _number(valor, 4)
    return str(valor)


def _linea_numero(candidate: dict[str, Any], numero: int) -> dict[str, Any] | None:
    lineas = candidate.get("lines") or []
    return lineas[numero - 1] if 1 <= numero <= len(lineas) else None


def valor_actual(candidate: dict[str, Any], destino: list[str]) -> str:
    """Cómo se ve hoy el dato al que apunta `destino`, con su unidad o su €."""
    if destino and destino[0] == "linea":
        linea = _linea_numero(candidate, int(destino[1]))
        if linea is None:
            return "—"
        campo = destino[2]
        if campo == "cantidad":
            return f"{_number(linea.get('cantidad'), 3)} {linea.get('unidad') or 'ud'}"
        if campo == "precio":
            return f"{_number(linea.get('precio_unitario'), 4)}€"
        if campo == "importe":
            return f"{_number(linea.get('importe_neto'))}€"
        if campo == "nombre":
            return str(linea.get("descripcion_limpia") or "—")
        return "—"
    header = candidate.get("header") or {}
    campo = destino[0] if destino else ""
    if campo == "fecha":
        return str(header.get("fecha") or "—")
    if campo == "numero":
        return str(header.get("numero_albaran") or "—")
    if campo == "proveedor":
        return str(header.get("proveedor_nombre") or "—")
    valor = header.get(HEADER_FIELDS.get(campo, ""))
    return f"{_number(valor)}€" if valor is not None else "—"


def _acortar(texto: str, limite: int = 22) -> str:
    """Telegram recorta los botones largos por su cuenta y sin avisar; mejor
    recortar nosotros con "…" para que se vea que el texto sigue."""
    texto = str(texto)
    return texto if len(texto) <= limite else texto[:limite - 1].rstrip() + "…"


def lineas_corregibles(view: "ReviewView") -> list[tuple[int, str]]:
    """(número de línea, etiqueta corta para el botón)."""
    return [
        (numero, f"{numero}. {_acortar(linea.get('descripcion_limpia') or 'Sin nombre')}")
        for numero, linea in enumerate(view.candidate.get("lines") or [], start=1)
    ]


def campos_de_linea(view: "ReviewView", numero: int) -> list[tuple[list[str], str]]:
    """(destino para /corregir, etiqueta con el valor que tiene ahora)."""
    linea = _linea_numero(view.candidate, numero)
    if linea is None:
        raise ValueError("Esa línea ya no existe")
    opciones = []
    for campo in _CAMPOS_DE_LINEA:
        destino = ["linea", str(numero), campo]
        opciones.append((destino, f"{campo.capitalize()}: {valor_actual(view.candidate, destino)}"))
    return opciones


def campos_de_cabecera(view: "ReviewView") -> list[tuple[list[str], str]]:
    etiquetas = {
        "fecha": "Fecha", "numero": "Nº albarán", "proveedor": "Proveedor",
        "total": "Total", "base": "Base", "iva": "IVA",
    }
    return [
        ([campo], f"{etiquetas[campo]}: {_acortar(valor_actual(view.candidate, [campo]), 28)}")
        for campo in _CAMPOS_DE_CABECERA
    ]


def titulo_de_linea(view: "ReviewView", numero: int) -> str:
    linea = _linea_numero(view.candidate, numero)
    if linea is None:
        raise ValueError("Esa línea ya no existe")
    return _line_summary(numero, linea)


def pregunta_de_correccion(view: "ReviewView", destino: list[str]) -> str:
    """La frase que se manda al pedir el valor nuevo.

    Dice siempre qué hay ahora, para que se pueda comparar con el papel sin
    volver atrás, y pone un ejemplo del formato para que nadie dude de si los
    decimales van con coma o con punto.
    """
    actual = valor_actual(view.candidate, destino)
    if destino[0] == "linea":
        linea = _linea_numero(view.candidate, int(destino[1]))
        nombre = str((linea or {}).get("descripcion_limpia") or f"línea {destino[1]}")
        campo = destino[2]
        preguntas = {
            "cantidad": f"¿Qué cantidad pone en el albarán para «{nombre}»?",
            "precio": f"¿Qué precio por unidad pone para «{nombre}»?",
            "importe": f"¿Qué importe pone en la línea de «{nombre}»?",
            "nombre": "¿Cómo se llama ese producto?",
        }
        ejemplo = "Sardina" if campo == "nombre" else "15,4"
        return (
            f"{preguntas[campo]}\n"
            f"Ahora tengo: {actual}\n\n"
            f"Responde solo con el dato, por ejemplo: {ejemplo}"
        )
    preguntas = {
        "fecha": "¿Qué fecha pone el albarán?",
        "numero": "¿Qué número de albarán pone?",
        "proveedor": "¿Cómo se llama el proveedor?",
        "total": "¿Qué total pone el albarán?",
        "base": "¿Qué base imponible pone?",
        "iva": "¿Qué IVA pone?",
    }
    ejemplos = {
        "fecha": "02/09/2026", "numero": "0734079", "proveedor": "Matadero INTESA",
        "total": "133,67", "base": "133,67", "iva": "13,37",
    }
    return (
        f"{preguntas[destino[0]]}\n"
        f"Ahora tengo: {actual}\n\n"
        f"Responde solo con el dato, por ejemplo: {ejemplos[destino[0]]}"
    )


# Descuadres que delatan que las cifras del pie no son de fiar. Con cualquiera de
# ellos presente no sabemos qué número está mal, así que no se ofrece el cargo:
# convertir el hueco en una línea fosilizaría una mala lectura en vez de arreglarla.
_CIFRAS_DE_CABECERA_DUDOSAS = {
    "vat_quota_mismatch", "vat_bases_mismatch", "vat_total_mismatch",
    "vat_detail_invalid", "document_total_mismatch",
}


def _acciones_para(reviews: list[dict], candidate: dict[str, Any]) -> list[tuple[str, str]]:
    if not candidate:
        return []
    acciones: list[tuple[str, str]] = []
    motivos = {str(item.get("reason_code")) for item in reviews}
    if "date_invalid" in motivos:
        acciones.append(("📅 Es de hoy", "hoy"))
    if (
        "base_lines_mismatch" in motivos
        and not (motivos & _CIFRAS_DE_CABECERA_DUDOSAS)
        # Si el hueco lo abrió una corrección a mano, no es un cargo que falte:
        # es que otra cifra de esa misma línea sigue mal leída. Convertirlo en
        # "Cargo sin identificar" fosilizaría el error en la contabilidad.
        and not _lineas_corregidas_a_mano(candidate)
    ):
        # Un albarán manuscrito declaraba "TOTAL BRUTO 427,0" con sus dos líneas
        # sumando 421,00: parecía faltar un cargo de 6 €, pero el pie tampoco
        # cuadraba consigo mismo (427 + 47,10 ≠ 463,10). Era un 421 mal leído.
        hueco = _hueco_de_la_base(candidate)
        if hueco > 0:
            acciones.append((f"➕ Cargo de {_euros(hueco)}", "cargo"))
    return acciones


def atajos_de_correccion(view: "ReviewView") -> list[tuple[str, list[str]]]:
    """Accesos directos al dato que casi seguro sigue mal, sin pasar por el menú.

    Cuando alguien corrige la cantidad de un producto y con eso el albarán deja
    de cuadrar, solo hay dos culpables posibles: el precio de esa misma línea o
    el total del papel. Hacerle recorrer otra vez menú → producto → campo para
    llegar a uno de los dos es trabajo inútil, y encima esconde cuáles son las
    dos opciones reales.
    """
    motivos = {str(item.get("reason_code")) for item in view.open_reviews}
    if "base_lines_mismatch" not in motivos:
        return []
    atajos: list[tuple[str, list[str]]] = []
    for numero, linea in enumerate(view.candidate.get("lines") or [], start=1):
        decisiones = linea.get("decisiones") or {}
        tocados = {
            campo for campo, detalle in decisiones.items()
            if campo in _CIFRAS_DE_LINEA and isinstance(detalle, dict)
            and detalle.get("source") == "human_correction"
        }
        if not tocados:
            continue
        nombre = _acortar(linea.get("descripcion_limpia") or f"línea {numero}", 16)
        # Se ofrece la otra cifra de la línea, no la que se acaba de tocar.
        campo = "importe" if "precio_unitario" in tocados else "precio"
        etiqueta = "Importe" if campo == "importe" else "Precio"
        atajos.append((f"✏️ {etiqueta} de {nombre}", ["linea", str(numero), campo]))
    if atajos:
        # Sin IVA da igual cuál se toque (se igualan solos) y "total" es lo que
        # la gente busca en el papel; con IVA hay que apuntar a la base, que es
        # la cifra concreta que no cuadra con las líneas.
        sin_iva = not (view.candidate.get("header") or {}).get("total_iva")
        atajos.append(
            ("✏️ Total del albarán", ["total"]) if sin_iva
            else ("✏️ Base del albarán", ["base"])
        )
    return atajos[:3]


def acciones_sugeridas(view: "ReviewView") -> list[tuple[str, str]]:
    """Arreglos de un toque para lo que bloquea este albarán.

    Devuelve pares (texto del botón, acción). La idea es que la persona no tenga
    que deducir qué campo tocar ni redactar un comando: si lo que falta tiene una
    respuesta evidente, se ofrece hecha y basta con confirmarla.
    """
    return _acciones_para(view.open_reviews, view.candidate)


def _set_correction(candidate: dict[str, Any], args: list[str], user_id: int) -> str:
    if not args:
        raise ValueError("Falta campo o valor")
    accion = args[0].lower()
    if accion in _ALTA_LINEA:
        return _añadir_linea(candidate, args[1:], user_id)
    if accion in _BAJA_LINEA:
        return _borrar_linea(candidate, args[1:])
    if accion in _CARGO:
        return _añadir_cargo(candidate, args, user_id)
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
        nombre = target.get("descripcion_limpia") or f"línea {index + 1}"
        label = f"«{nombre}» · {_ETIQUETA_CAMPO.get(field, field)}"
    else:
        field = HEADER_FIELDS.get(args[0].lower())
        if not field:
            raise ValueError("Campo de cabecera no permitido")
        raw_value = " ".join(args[1:]).strip()
        target = candidate["header"]
        label = _ETIQUETA_CAMPO.get(field, field)
    if field in NUMERIC_FIELDS:
        value = _parsear_numero(raw_value)
        if value is None or value < 0:
            raise ValueError(
                "Eso no me parece un número. Escribe solo la cifra, "
                "con coma para los decimales: 15,4"
            )
    elif field == "fecha":
        # La validación contable exige ISO (date.fromisoformat). Guardar aquí el
        # texto tal cual hacía que corregir la fecha no sirviera de nada: se
        # aceptaba "02/09/2026" y el albarán seguía marcado como fecha inválida.
        value = _normalizar_fecha(raw_value)
        if value is None:
            raise ValueError("No entiendo esa fecha. Escríbela así: 02/09/2026")
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
    consecuencias: list[str] = []
    if target is candidate["header"]:
        nota_sin_iva = _igualar_base_y_total_sin_iva(target, field, previous)
        if nota_sin_iva:
            consecuencias.append(nota_sin_iva)
    if target is not candidate["header"]:
        decisions = target.setdefault("decisiones", {})
        decisions[field] = {
            "source": "human_correction", "actor": str(user_id),
            "previous": previous, "accepted": value,
        }
        nota = _recalcular_linea(target, field)
        if nota:
            consecuencias.append(nota)
    nota_totales = _recalcular_totales_derivados(candidate)
    if nota_totales:
        consecuencias.append(nota_totales)
    resumen = f"{label}: {_formatear_valor(previous)} → {_formatear_valor(value)}"
    resumen = resumen[0].upper() + resumen[1:]
    if consecuencias:
        resumen += f" ({'; '.join(consecuencias)})"
    return resumen


# Estados desde los que corregir el candidato ya no tiene sentido: el albarán
# está cerrado y sus líneas viven en `albaranes`/`lineas_albaran`, no en el
# candidato. Editar aquí dejaría la ingesta abierta otra vez con una versión
# nueva que jamás podrá confirmarse (la clave de idempotencia ya está usada con
# otro contenido), y mientras tanto la contabilidad seguiría con los datos
# viejos. Es preferible decirlo que corromperlo en silencio.
_ESTADOS_CERRADOS = {"confirmed", "rejected", "archived"}


async def correct_candidate(reference: str, user_id: int, correction_args: list[str]) -> ReviewView:
    ingestion = await db.buscar_ingestion_por_referencia(reference, user_id)
    if not ingestion:
        raise ValueError("Referencia no encontrada o ambigua")
    if str(ingestion.get("status")) in _ESTADOS_CERRADOS:
        raise ValueError(
            "Ese albarán ya está cerrado y no se puede corregir desde aquí. "
            f"Archívalo con «/anular {reference} motivo» y vuelve a subir la foto."
        )
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
    return dataclasses.replace(
        await build_review_view(ingestion_id, user_id), ultimo_cambio=changed_label
    )
