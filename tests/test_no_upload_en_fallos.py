"""
Verifica el invariante central: la imagen se sube a Storage (db.subir_imagen) SOLO cuando
el albarán se guarda con éxito. En duplicado, ilegible, blacklist o validación fallida NO
debe subirse nada (no se crean fotos huérfanas).

Reproduce las "varias interacciones" pedidas con mocks: válido, duplicado por contenido,
duplicado en BD (23505), ilegible, blacklist y validación mínima fallida.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import albaran_processor as ap  # noqa: E402

_ALBARAN_VALIDO = {
    "proveedor_nombre": "Proveedor Test",
    "proveedor_nif": "B12345678",
    "numero_albaran": "A-100",
    "fecha": "01/06/2026",
    "total": 100.0,
    "lineas": [
        {
            "nombre_producto": "Tomate",
            "cantidad": 2,
            "unidad": "kg",
            "precio_unitario": 50.0,
            "importe_neto": 100.0,
            "confianza": 100,
        }
    ],
}


@pytest.fixture
def mocks(monkeypatch):
    """Mockea OCR, LLM, normalización y todas las funciones de BD del pipeline.

    Por defecto monta el camino de ÉXITO; cada test ajusta lo que necesite.
    Devuelve un namespace con los mocks para poder afinar e inspeccionar.
    """
    # Cliente Mistral: no se usa porque OCR y LLM están mockeados
    monkeypatch.setattr(ap, "Mistral", MagicMock())

    ocr = AsyncMock(return_value="Texto OCR del albarán")
    extraer = AsyncMock(return_value=_ALBARAN_VALIDO)
    monkeypatch.setattr(ap, "_ocr_imagen", ocr)
    monkeypatch.setattr(ap, "_extraer_datos_llm", extraer)

    norm = SimpleNamespace(normalized_name="Tomate", is_new_product=False)
    monkeypatch.setattr(ap, "normalizar_productos_batch", AsyncMock(return_value=[norm]))
    monkeypatch.setattr(ap, "invalidar_cache_proveedor", MagicMock())

    db = ap.db
    monkeypatch.setattr(db, "actualizar_job", AsyncMock(return_value={}))
    monkeypatch.setattr(db, "registrar_auditoria", AsyncMock(return_value=None))
    monkeypatch.setattr(
        db, "buscar_o_crear_proveedor",
        AsyncMock(return_value=({"id": "prov1", "nombre": "Proveedor Test"}, False)),
    )
    # Sin duplicado por defecto
    monkeypatch.setattr(db, "buscar_albaran_duplicado_combinacion", AsyncMock(return_value=None))
    monkeypatch.setattr(db, "buscar_albaran_duplicado_por_nombre_proveedor", AsyncMock(return_value=None))
    monkeypatch.setattr(db, "buscar_albaran_duplicado_norm", AsyncMock(return_value=None))
    monkeypatch.setattr(db, "insertar_albaran", AsyncMock(return_value={"id": "alb1"}))
    monkeypatch.setattr(db, "actualizar_campo_albaran", AsyncMock(return_value={}))
    monkeypatch.setattr(db, "buscar_productos_por_proveedor", AsyncMock(return_value=[]))
    monkeypatch.setattr(db, "buscar_o_crear_producto_catalogo", AsyncMock(return_value={"id": "cat1"}))
    monkeypatch.setattr(db, "actualizar_precio_catalogo", AsyncMock(return_value=(None, False)))
    monkeypatch.setattr(db, "insertar_lineas", AsyncMock(return_value=[{"id": "lin1"}]))

    # El mock que vigilamos
    subir = AsyncMock(return_value="https://storage/fake.jpg")
    monkeypatch.setattr(db, "subir_imagen", subir)

    return SimpleNamespace(db=db, subir=subir, ocr=ocr, extraer=extraer)


async def _procesar(mocks):
    return await ap.procesar_albaran(
        job_id="job1",
        imagen_bytes=b"bytes-imagen",
        chat_id=123,
        progress_callback=AsyncMock(),
        imagen_hash="hash123",
    )


async def test_exito_sube_una_vez(mocks):
    resultado = await _procesar(mocks)
    assert resultado.es_duplicado is False
    assert mocks.subir.await_count == 1
    assert resultado.imagen_url == "https://storage/fake.jpg"


async def test_duplicado_por_contenido_no_sube(mocks):
    mocks.db.buscar_albaran_duplicado_combinacion = AsyncMock(
        return_value={"id": "alb-existente", "creado_en": "2026-06-01", "numero_albaran": "A-99"}
    )
    resultado = await _procesar(mocks)
    assert resultado.es_duplicado is True
    assert mocks.subir.await_count == 0


async def test_duplicado_bd_23505_no_sube(mocks):
    mocks.db.insertar_albaran = AsyncMock(side_effect=Exception("duplicate key value 23505"))
    resultado = await _procesar(mocks)
    assert resultado.es_duplicado is True
    assert mocks.subir.await_count == 0


async def test_ilegible_no_sube(mocks):
    mocks.ocr.return_value = "   "  # OCR no extrae texto
    with pytest.raises(Exception):
        await _procesar(mocks)
    assert mocks.subir.await_count == 0


async def test_blacklist_no_sube(mocks):
    mocks.ocr.return_value = "NÓMINA del mes de junio - salario bruto"
    with pytest.raises(Exception):
        await _procesar(mocks)
    assert mocks.subir.await_count == 0


async def test_validacion_minima_falla_no_sube(mocks):
    sin_lineas = dict(_ALBARAN_VALIDO, lineas=[])
    mocks.extraer.return_value = sin_lineas
    with pytest.raises(Exception):
        await _procesar(mocks)
    assert mocks.subir.await_count == 0
