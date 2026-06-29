"""
Ingesta REAL de los 9 albaranes no-problemáticos a Supabase, ejecutando el pipeline de
producción `procesar_albaran` tal cual, pero con el OCR y la extracción LLM servidos desde
.cache_test/ (ya verificados) para no re-gastar tokens ni introducir no-determinismo.

Los 2 manuscritos (albaran-problematico1/2) NO se ingieren aquí: los inserta el usuario
por Telegram para revisarlos juntos.

Uso: python scripts/ingest_real.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src import albaran_processor as ap  # noqa: E402
from src import supabase_client as db    # noqa: E402

_CACHE = _ROOT / ".cache_test"
_IMG_DIR = _ROOT / "albaranes_test.md"
_CHAT_ID = 672878000  # id de prueba para la ingesta

# Los 9 no-problemáticos (se excluyen los 2 manuscritos para inserción manual por Telegram)
_EXCLUIR = {"albaran-problematico1", "albaran-problematico2"}


def _imagenes() -> list[Path]:
    return sorted(
        p for p in _IMG_DIR.iterdir()
        if p.suffix.lower() in {".jpg", ".jpeg", ".png"}
        and not p.name.startswith(".")
        and p.stem not in _EXCLUIR
    )


def _fake_ocr(text: str):
    async def _f(_b64, _client):
        return text
    return _f


def _fake_llm(raw: dict):
    async def _f(_ocr, _client):
        return raw
    return _f


async def _progress(_msg: str) -> None:
    pass


async def main() -> None:
    imagenes = _imagenes()
    print(f"Ingiriendo {len(imagenes)} albaranes (pipeline real, OCR/LLM cacheados)\n")

    resultados = []
    for img in imagenes:
        stem = img.stem
        ocr_text = (_CACHE / f"{stem}.ocr.txt").read_text(encoding="utf-8")
        raw = json.loads((_CACHE / f"{stem}.raw.json").read_text(encoding="utf-8"))

        # Servir OCR + extracción desde caché, dejar el resto del pipeline en real
        ap._ocr_imagen = _fake_ocr(ocr_text)
        ap._extraer_datos_llm = _fake_llm(raw)

        imagen_bytes = img.read_bytes()
        imagen_hash = hashlib.sha256(imagen_bytes).hexdigest()
        job = await db.crear_job(telegram_user_id=_CHAT_ID)
        try:
            r = await ap.procesar_albaran(job["id"], imagen_bytes, _CHAT_ID, _progress, imagen_hash=imagen_hash)
            resultados.append((stem, r))
            dup = " [DUPLICADO]" if r.es_duplicado else ""
            rev = f" revisión={r.lineas_con_revision}" if r.lineas_con_revision else ""
            conf = f" confirmar={len(r.lineas_para_confirmacion)}" if r.lineas_para_confirmacion else ""
            print(f"✓ {stem[:28]:30} {r.proveedor_nombre[:30]:32} Nº {str(r.numero_albaran):>12} "
                  f"tot={r.total} líneas={r.num_lineas}{dup}{rev}{conf}")
        except Exception as e:
            print(f"✗ {stem[:28]:30} ERROR: {e}")
            resultados.append((stem, None))

    print(f"\nIngeridos OK: {sum(1 for _, r in resultados if r and not r.es_duplicado)}/{len(imagenes)}")


if __name__ == "__main__":
    asyncio.run(main())
