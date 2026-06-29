"""
Harness de prueba del pipeline de albaranes en MODO TEST (sin tocar la BD).

Procesa todas las imágenes de albaranes_test.md/ ejecutando:
  OCR (Mistral) → extracción LLM → resolución de precio NETO → validación.

Cachea el resultado del OCR y de la extracción LLM en .cache_test/ para poder
iterar sobre la lógica Python sin volver a gastar llamadas a la API.

Uso:
  python scripts/test_pipeline.py                 # usa caché si existe
  python scripts/test_pipeline.py --refresh-ocr   # rehace OCR + LLM
  python scripts/test_pipeline.py --refresh-llm    # rehace solo extracción LLM
  python scripts/test_pipeline.py --only problematico1
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from mistralai.client.sdk import Mistral  # noqa: E402

from src.config import settings  # noqa: E402
from src import albaran_processor as ap  # noqa: E402

_IMG_DIR = _ROOT / "albaranes_test.md"
_CACHE_DIR = _ROOT / ".cache_test"
_CACHE_DIR.mkdir(exist_ok=True)

_IMG_EXTS = {".jpg", ".jpeg", ".png"}


def _listar_imagenes() -> list[Path]:
    return sorted(
        p for p in _IMG_DIR.iterdir()
        if p.suffix.lower() in _IMG_EXTS and not p.name.startswith(".")
    )


async def _obtener_ocr(img: Path, client: Mistral, refresh: bool) -> str:
    cache = _CACHE_DIR / f"{img.stem}.ocr.txt"
    if cache.exists() and not refresh:
        return cache.read_text(encoding="utf-8")
    b64 = base64.b64encode(img.read_bytes()).decode()
    texto = await ap._con_reintento(ap._ocr_imagen, b64, client)
    cache.write_text(texto or "", encoding="utf-8")
    return texto or ""


async def _obtener_llm(img: Path, ocr_text: str, client: Mistral, refresh: bool) -> dict:
    cache = _CACHE_DIR / f"{img.stem}.raw.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text(encoding="utf-8"))
    data = await ap._con_reintento(ap._extraer_datos_llm, ocr_text, client)
    cache.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def _analizar(img_name: str, ocr_text: str, raw: dict) -> dict:
    """Aplica la lógica determinista y validaciones; devuelve un informe estructurado."""
    informe: dict = {"imagen": img_name, "errores": [], "lineas": []}

    # OCR vacío / confianza
    informe["ocr_len"] = len(ocr_text or "")
    if not ocr_text or not ocr_text.strip():
        informe["errores"].append({"tipo": "ocr_vacio", "detalle": "OCR no extrajo texto"})

    # blacklist
    bl = ap._verificar_blacklist(ocr_text or "")
    if bl:
        informe["errores"].append({"tipo": "blacklist", "detalle": bl})

    # Validación Pydantic / extracción
    try:
        albaran = ap.AlbaranLLM.model_validate(raw)
    except Exception as e:
        informe["errores"].append({"tipo": "pydantic", "detalle": str(e)[:300]})
        return informe

    informe["proveedor"] = albaran.proveedor_nombre
    informe["nif"] = albaran.proveedor_nif
    informe["numero"] = albaran.numero_albaran
    informe["fecha"] = albaran.fecha
    informe["total"] = albaran.total
    informe["forma_pago"] = albaran.forma_pago

    # Resolver precio neto determinista
    for l in albaran.lineas:
        ap._resolver_precio_neto(l)

    # Validación mínima
    ok_min, motivo_min = ap._validar_datos_minimos(albaran)
    if not ok_min:
        informe["errores"].append({"tipo": "validacion_minima", "detalle": motivo_min})

    # Detección de líneas duplicadas (mismo nombre normalizado lower)
    nombres = [l.nombre_producto.strip().lower() for l in albaran.lineas]
    vistos: dict[str, int] = {}
    for n in nombres:
        vistos[n] = vistos.get(n, 0) + 1
    repetidos = {n: c for n, c in vistos.items() if c > 1}
    informe["nombres_repetidos"] = repetidos  # informativo: NO es error per se

    # Por línea
    suma_lineas = 0.0
    lineas_revision = 0
    for i, l in enumerate(albaran.lineas, 1):
        ok, motivo = ap._validar_linea(l)
        suma_lineas += l.importe_neto or 0
        if not ok or l.confianza < 70:
            lineas_revision += 1
        esperado = None
        if l.precio_unitario and l.cantidad:
            esperado = round(l.precio_unitario * l.cantidad, 2)
        informe["lineas"].append({
            "n": i,
            "nombre": l.nombre_producto,
            "cant": l.cantidad,
            "unidad": l.unidad,
            "tarifa": l.precio_tarifa,
            "dto": l.descuento_pct,
            "neto_col": l.precio_neto,
            "precio_unit": l.precio_unitario,
            "importe": l.importe_neto,
            "esperado": esperado,
            "conf": l.confianza,
            "ok": ok,
            "motivo": motivo,
        })

    informe["num_lineas"] = len(albaran.lineas)
    informe["suma_lineas"] = round(suma_lineas, 2)
    informe["lineas_revision"] = lineas_revision

    # Total cuadra — usa la MISMA reconciliación IVA-aware del pipeline real
    cuadra, suma = ap._reconciliar_lineas_total(albaran)
    if albaran.total:
        informe["total_dif_pct"] = round(abs(suma - albaran.total) / albaran.total * 100, 2)
    if not cuadra:
        informe["errores"].append({
            "tipo": "total_no_cuadra",
            "detalle": (
                f"suma={suma:.2f} no cuadra con base={albaran.base_imponible} / "
                f"total-iva={(albaran.total - albaran.total_iva) if albaran.total and albaran.total_iva else None} / "
                f"total={albaran.total}"
            ),
        })

    return informe


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh-ocr", action="store_true")
    parser.add_argument("--refresh-llm", action="store_true")
    parser.add_argument("--only", default=None, help="subcadena del nombre de imagen")
    parser.add_argument("--json-out", default=None, help="ruta para volcar informe JSON")
    args = parser.parse_args()

    client = Mistral(api_key=settings.MISTRAL_API_KEY)
    imagenes = _listar_imagenes()
    if args.only:
        imagenes = [p for p in imagenes if args.only.lower() in p.name.lower()]

    informes = []
    for img in imagenes:
        try:
            ocr = await _obtener_ocr(img, client, args.refresh_ocr)
            raw = await _obtener_llm(img, ocr, client, args.refresh_ocr or args.refresh_llm)
            informe = _analizar(img.name, ocr, raw)
        except Exception as e:
            informe = {"imagen": img.name, "errores": [{"tipo": "excepcion", "detalle": str(e)[:300]}], "lineas": []}
        informes.append(informe)
        _imprimir(informe)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(informes, ensure_ascii=False, indent=2), encoding="utf-8")

    _resumen(informes)


def _imprimir(inf: dict) -> None:
    print("\n" + "=" * 78)
    print(f"📄 {inf['imagen']}")
    if "proveedor" in inf:
        print(f"   Proveedor: {inf.get('proveedor')!r}  NIF: {inf.get('nif')}")
        print(f"   Nº: {inf.get('numero')}  Fecha: {inf.get('fecha')}  Total: {inf.get('total')}  Pago: {inf.get('forma_pago')}")
        print(f"   Líneas: {inf.get('num_lineas')}  Suma líneas: {inf.get('suma_lineas')}  "
              f"Dif total: {inf.get('total_dif_pct', '—')}%  Revisión: {inf.get('lineas_revision')}")
        if inf.get("nombres_repetidos"):
            print(f"   ⚠ Nombres repetidos (¿líneas físicas distintas?): {inf['nombres_repetidos']}")
        for l in inf["lineas"]:
            flag = "✓" if l["ok"] and l["conf"] >= 70 else "✗"
            print(f"     {flag} {l['n']:>2}. {l['nombre'][:34]:<34} "
                  f"cant={_f(l['cant'])} {l['unidad'] or '?':<4} "
                  f"tar={_f(l['tarifa'])} dto={_f(l['dto'])} netoCol={_f(l['neto_col'])} "
                  f"→ PU={_f(l['precio_unit'])} imp={_f(l['importe'])} (esp={_f(l['esperado'])}) c={l['conf']}")
            if not l["ok"]:
                print(f"          ↳ {l['motivo']}")
    if inf["errores"]:
        print("   ❌ ERRORES:")
        for e in inf["errores"]:
            print(f"      - [{e['tipo']}] {e['detalle']}")


def _f(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _resumen(informes: list[dict]) -> None:
    print("\n" + "#" * 78)
    print("RESUMEN GLOBAL")
    print("#" * 78)
    n = len(informes)
    con_error = [i for i in informes if i["errores"]]
    print(f"Albaranes: {n}  |  Con errores: {len(con_error)}  |  Limpios: {n - len(con_error)}")
    # Conteo por tipo
    tipos: dict[str, int] = {}
    for inf in informes:
        for e in inf["errores"]:
            tipos[e["tipo"]] = tipos.get(e["tipo"], 0) + 1
    if tipos:
        print("Errores por tipo:")
        for t, c in sorted(tipos.items(), key=lambda x: -x[1]):
            print(f"   {c:>3}  {t}")
    # líneas totales y con problema
    total_lineas = sum(i.get("num_lineas", 0) for i in informes)
    lineas_mal = sum(1 for i in informes for l in i.get("lineas", []) if not l["ok"])
    print(f"Líneas totales: {total_lineas}  |  Líneas que fallan validación: {lineas_mal}")


if __name__ == "__main__":
    asyncio.run(main())
