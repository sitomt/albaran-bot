"""
Normaliza nombres de productos usando Mistral Small.
Decide si un nombre es producto nuevo o variante de uno existente en el catálogo.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta

from mistralai.client.sdk import Mistral
from pydantic import BaseModel

from .config import settings
from . import supabase_client as db

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
Eres un asistente especializado en normalizar nombres de productos de hostelería.

Tu tarea: dado un nombre de producto extraído de un albarán y la lista de productos
ya registrados para ese proveedor, decide si es un producto nuevo o una variante
(nombre alternativo) de uno ya existente.

Criterios:
- Considera variante si es claramente el mismo producto escrito de forma diferente.
  Ejemplos: "LONGANIZA BLANCA L/S" = "Longaniza Bca" = "longaniza blanca" → variante.
- Si hay duda, crea producto nuevo.
- El nombre normalizado debe ser en Title Case, sin abreviaciones, sin unidades de
  medida ni cantidades (esas van en otros campos).
  Ejemplo: "Bocata gran reserva 150g (50 unid)" → "Bocata Gran Reserva"
  Ejemplo: "Queso Cremette cubo 3.5kg" → "Queso Cremette Cubo"
- Si es el mismo producto de otro proveedor, crea nuevo (proveedor_id diferente).

Responde SOLO con JSON con esta estructura exacta:
{
  "is_new_product": true/false,
  "normalized_name": "Nombre Normalizado",
  "existing_product_id": "uuid-si-es-variante-o-null",
  "is_variant": true/false,
  "variant_name": "nombre original a guardar en variantes o null"
}
"""

_SYSTEM_PROMPT_BATCH = """\
Eres un asistente especializado en normalizar nombres de productos de hostelería.

Tu tarea: dada una lista numerada de nombres de productos extraídos de un albarán y
la lista de productos ya registrados para ese proveedor, decide para CADA producto si
es nuevo o una variante de uno ya existente.

Criterios (igual para cada producto):
- Considera variante si es claramente el mismo producto escrito de forma diferente.
- Si hay duda, crea producto nuevo.
- El nombre normalizado debe ser en Title Case, sin abreviaciones, sin unidades de
  medida ni cantidades.
  Ejemplo: "Bocata gran reserva 150g (50 unid)" → "Bocata Gran Reserva"
- Si es el mismo producto de otro proveedor, crea nuevo.

Responde SOLO con un array JSON con exactamente tantos elementos como productos en la
lista de entrada, en el mismo orden:
[
  {
    "is_new_product": true/false,
    "normalized_name": "Nombre Normalizado",
    "existing_product_id": "uuid-o-null",
    "is_variant": true/false,
    "variant_name": "nombre original o null"
  },
  ...
]
"""

# Caché en memoria: proveedor_id → (lista_productos, timestamp)
_catalogo_cache: dict[str, tuple[list, datetime]] = {}
_CACHE_TTL = timedelta(hours=1)


def invalidar_cache_proveedor(proveedor_id: str) -> None:
    """Llama tras insertar un producto nuevo para que el siguiente albarán vea el catálogo actualizado."""
    _catalogo_cache.pop(proveedor_id, None)


async def _get_catalogo(proveedor_id: str) -> list[dict]:
    entrada = _catalogo_cache.get(proveedor_id)
    if entrada and datetime.now() - entrada[1] < _CACHE_TTL:
        return entrada[0]
    productos = await db.buscar_productos_por_proveedor(proveedor_id)
    _catalogo_cache[proveedor_id] = (productos, datetime.now())
    return productos


class NormalizationResult(BaseModel):
    is_new_product: bool
    normalized_name: str
    existing_product_id: str | None = None
    is_variant: bool = False
    variant_name: str | None = None


async def normalizar_producto(
    proveedor_id: str,
    nombre_original: str,
    productos_existentes: list[dict] | None = None,
) -> NormalizationResult:
    """
    Normaliza un nombre de producto para un proveedor dado.
    Si productos_existentes se pasa, se usa directamente (evita consulta BD).
    Si no se pasa, consulta BD con caché de 1 hora.
    """
    if productos_existentes is None:
        productos_existentes = await _get_catalogo(proveedor_id)

    if not productos_existentes:
        normalized = _normalizar_simple(nombre_original)
        return NormalizationResult(
            is_new_product=True,
            normalized_name=normalized,
            is_variant=False,
        )

    catalogo_texto = "\n".join(
        f"- ID: {p['id']} | Nombre: {p['nombre_normalizado']} | Variantes: {p.get('variantes', [])}"
        for p in productos_existentes
    )

    user_content = f"""Nombre del producto a normalizar: "{nombre_original}"

Productos ya registrados para este proveedor:
{catalogo_texto}

¿Es un producto nuevo o variante de alguno existente?"""

    client = Mistral(api_key=settings.MISTRAL_API_KEY)
    try:
        response = await asyncio.wait_for(
            client.chat.complete_async(
                model="mistral-small-2506",
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
                max_tokens=300,
            ),
            timeout=20,
        )
        raw = response.choices[0].message.content
        data = json.loads(raw)
        result = NormalizationResult.model_validate(data)
    except Exception as e:
        logger.warning("Error normalizando producto '%s': %s — usando normalización simple", nombre_original, e)
        result = NormalizationResult(
            is_new_product=True,
            normalized_name=_normalizar_simple(nombre_original),
            is_variant=False,
        )

    if result.is_variant and result.existing_product_id and result.variant_name:
        try:
            await db.actualizar_variantes_producto(result.existing_product_id, result.variant_name)
        except Exception as e:
            logger.warning("No se pudo actualizar variantes para %s: %s", result.existing_product_id, e)

    return result


async def normalizar_productos_batch(
    proveedor_id: str,
    nombres: list[str],
    productos_existentes: list[dict] | None = None,
) -> list[NormalizationResult]:
    """
    Normaliza todos los nombres de una sola vez con una única llamada LLM.
    Fallback automático a llamadas individuales en paralelo si la respuesta es inválida.
    """
    if not nombres:
        return []

    if productos_existentes is None:
        productos_existentes = await _get_catalogo(proveedor_id)

    # Fast-path: catálogo vacío → normalización sin LLM para todos
    if not productos_existentes:
        return [
            NormalizationResult(
                is_new_product=True,
                normalized_name=_normalizar_simple(n),
                is_variant=False,
            )
            for n in nombres
        ]

    catalogo_texto = "\n".join(
        f"- ID: {p['id']} | Nombre: {p['nombre_normalizado']} | Variantes: {p.get('variantes', [])}"
        for p in productos_existentes
    )
    lista_productos = "\n".join(f"{i + 1}. \"{n}\"" for i, n in enumerate(nombres))
    user_content = (
        f"Productos a normalizar:\n{lista_productos}\n\n"
        f"Catálogo registrado para este proveedor:\n{catalogo_texto}"
    )

    client = Mistral(api_key=settings.MISTRAL_API_KEY)
    results: list[NormalizationResult] | None = None
    try:
        response = await asyncio.wait_for(
            client.chat.complete_async(
                model="mistral-small-2506",
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT_BATCH},
                    {"role": "user", "content": user_content},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
                max_tokens=min(300 * len(nombres), 4096),
            ),
            timeout=30,
        )
        raw = response.choices[0].message.content
        # El modelo puede devolver {"results": [...]} o directamente [...]
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            # Buscar la primera clave cuyo valor sea una lista
            arr = next((v for v in parsed.values() if isinstance(v, list)), None)
        else:
            arr = parsed
        if not isinstance(arr, list) or len(arr) != len(nombres):
            raise ValueError(f"Respuesta batch inválida: esperados {len(nombres)} items, obtenidos {len(arr) if isinstance(arr, list) else '?'}")
        results = [NormalizationResult.model_validate(item) for item in arr]
    except Exception as e:
        logger.warning("Batch normalización fallida (%s) — usando llamadas individuales", e)

    if results is None:
        # Fallback a N llamadas en paralelo
        results = list(await asyncio.gather(*[
            normalizar_producto(proveedor_id, n, productos_existentes)
            for n in nombres
        ]))
        return results

    # Actualizar variantes en paralelo para todos los resultados que lo requieran
    variante_tasks = [
        db.actualizar_variantes_producto(r.existing_product_id, r.variant_name)
        for r in results
        if r.is_variant and r.existing_product_id and r.variant_name
    ]
    if variante_tasks:
        try:
            await asyncio.gather(*variante_tasks)
        except Exception as e:
            logger.warning("Error actualizando variantes en batch: %s", e)

    return results


def _normalizar_simple(nombre: str) -> str:
    """Normalización básica sin LLM: title case, elimina unidades y cantidades al final."""
    import re
    nombre = re.sub(r"\s+\d+[\.,]?\d*\s*(kg|g|gr|l|ml|cl|unid|uds?|pcs?)\.?\s*(\(.*?\))?$", "", nombre, flags=re.IGNORECASE)
    nombre = re.sub(r"\s+\(.*?\)\s*$", "", nombre)
    return nombre.strip().title()
