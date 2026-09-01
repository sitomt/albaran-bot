"""
Tests del flujo de entrada manual de albaranes (/manual) con un `db` simulado en memoria.
Cubre: parsers, camino feliz con proveedor existente, alta de proveedor nuevo,
/corregir, /cancelar, foto opcional y detección de duplicados.
"""
from __future__ import annotations

import sys
import io
import uuid
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import manual_albaran as m  # noqa: E402


# ── Fake DB en memoria ──────────────────────────────────────────────────────────

class FakeDB:
    def __init__(self):
        self.proveedores = [{"id": "prov-1", "nombre": "Lucas Caballero S.L."}]
        self.albaranes = []
        self.lineas = []
        self.precios = {}
        self.ingestions = {}
        self.artifacts = {}
        self.storage = {}
        self.resolved_reviews = []
        self.audit_events = []

    async def listar_todos_proveedores(self):
        return [{"id": p["id"], "nombre": p["nombre"]} for p in self.proveedores]

    async def buscar_albaran_duplicado_por_nombre_proveedor(self, nombre, fecha, total):
        return next((a for a in self.albaranes
                     if a["proveedor_nombre"].casefold() == nombre.casefold()
                     and a["fecha"] == fecha and abs((a["total"] or 0) - total) <= 0.50), None)

    async def buscar_albaran_duplicado_norm(self, numero_norm, proveedor_id):
        return next((a for a in self.albaranes
                     if a.get("proveedor_id") == proveedor_id
                     and m._normalizar_numero_albaran(a.get("numero_albaran") or "") == numero_norm), None)

    async def subir_original_privado(self, bucket, path, data, content_type):
        self.storage[(bucket, path)] = data

    async def borrar_original_privado(self, bucket, path):
        self.storage.pop((bucket, path), None)

    async def crear_ingestion_manual(self, **row):
        stored = {"status": "extracted", **row}
        self.ingestions[row["ingestion_id"]] = stored
        return stored

    async def actualizar_ingestion(self, ingestion_id, **fields):
        self.ingestions.setdefault(ingestion_id, {}).update(fields)
        return self.ingestions[ingestion_id]

    async def obtener_ingestion(self, ingestion_id):
        return self.ingestions.get(ingestion_id)

    async def siguiente_intento_extraccion(self, ingestion_id):
        return 1

    async def registrar_artefacto_extraccion(self, **row):
        artifact = {"id": f"art-{uuid.uuid4().hex[:8]}", **row}
        self.artifacts[artifact["id"]] = artifact
        return artifact

    async def confirmar_albaran_atomico(
        self, *, ingestion_id, idempotency_key, actor_type, actor_id,
        albaran, lineas, extraction_artifact_id,
    ):
        provider_id = albaran.get("proveedor_id")
        if not provider_id:
            existing = next(
                (p for p in self.proveedores if p["nombre"].casefold() == albaran["proveedor_nombre"].casefold()),
                None,
            )
            if existing is None:
                existing = {
                    "id": f"prov-{len(self.proveedores)+1}",
                    "nombre": albaran["proveedor_nombre"], "nif": albaran.get("proveedor_nif"),
                }
                self.proveedores.append(existing)
            provider_id = existing["id"]
        row = {
            "id": f"alb-{len(self.albaranes)+1}", "proveedor_id": provider_id,
            "proveedor_nombre": albaran["proveedor_nombre"],
            "numero_albaran": albaran.get("numero_albaran"), "fecha": albaran["fecha"],
            "forma_pago": albaran.get("forma_pago"), "base_imponible": albaran.get("base_imponible"),
            "total_iva": albaran.get("total_iva"), "total": albaran["total"],
            "origen": albaran.get("origen"),
            "imagen_url": self.ingestions.get(ingestion_id, {}).get("storage_path"),
        }
        self.albaranes.append(row)
        self.lineas.extend({"albaran_id": row["id"], **line} for line in lineas)
        self.ingestions[ingestion_id]["status"] = "confirmed"
        return {"albaran_id": row["id"], "status": "confirmed"}

    async def registrar_evento_auditoria(self, *args, **kwargs):
        self.audit_events.append((args, kwargs))
        return None

    async def resolver_revisiones_abiertas(self, *args, **kwargs):
        self.resolved_reviews.append((args, kwargs))
        return None


@pytest.fixture
def fake_db(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(m, "db", db)
    m._manual_flows.clear()
    return db


CHAT = 12345


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(output, format="PNG")
    return output.getvalue()


# ── Parsers ─────────────────────────────────────────────────────────────────────

def test_parsear_cabecera_variantes():
    assert m._parsear_cabecera("3950 / 04-05-2026") == ("3950", "2026-05-04")
    assert m._parsear_cabecera("3950, 4 mayo 2026") == ("3950", "2026-05-04")
    assert m._parsear_cabecera("04/05/2026") == (None, "2026-05-04")
    assert m._parsear_cabecera("3950") == ("3950", None)


def test_parsear_producto_decimal_coma():
    assert m._parsear_producto("Tomate entero, 12, 1.81") == ("Tomate entero", 12.0, 1.81)
    assert m._parsear_producto("Aceite Oliva, 2, 46,75") == ("Aceite Oliva", 2.0, 46.75)
    assert m._parsear_producto("solo nombre") is None


def test_parsear_producto_detallado_valida_tarifa_descuento_neto_importe():
    line = m._parsear_producto_detallado("Tomate | 10 | 2,00 | 10% | 1,80 | 18,00")
    assert line["precio_tarifa"] == 2
    assert line["descuento_pct"] == 10
    assert line["precio"] == 1.8
    assert line["importe"] == 18
    assert m._parsear_producto_detallado("Tomate | 10 | 2 | 10 | 1,80 | 99") is None


def test_parsear_totales_variantes_y_rechazar_descuadres():
    assert m._parsear_totales("OK", 94.65) == (94.65, 0.0, 94.65)
    assert m._parsear_totales("IVA 3,78", 94.65) == (94.65, 3.78, 98.43)
    assert m._parsear_totales("98,43", 94.65) == (94.65, 3.78, 98.43)
    assert m._parsear_totales("94,65 / 3,78 / 98,43", 94.65) == (94.65, 3.78, 98.43)
    assert m._parsear_totales("94,65 / 3,78 / 99,00", 94.65) is None
    assert m._parsear_totales("90 / 8,43 / 98,43", 94.65) is None


# ── Camino feliz: proveedor existente ───────────────────────────────────────────

async def test_flujo_completo_proveedor_existente(fake_db):
    await m.iniciar(CHAT)
    assert m.flujo_activo(CHAT)

    await m.manejar_texto(CHAT, "1")                       # proveedor por número
    await m.manejar_texto(CHAT, "3950 / 04-05-2026")        # cabecera
    await m.manejar_texto(CHAT, "Tomate entero, 12, 1.81")  # producto 1
    await m.manejar_texto(CHAT, "Anchoa, 3, 22,73")         # producto 2
    await m.manejar_texto(CHAT, "FIN")
    await m.manejar_texto(CHAT, "OK")                       # total OK (acepta calculado)
    await m.manejar_texto(CHAT, "15 días")                  # forma de pago
    await m.manejar_texto(CHAT, "NO")                       # sin foto
    final = await m.manejar_texto(CHAT, "OK")               # confirmar

    assert "guardado" in final.lower()
    assert len(fake_db.albaranes) == 1
    alb = fake_db.albaranes[0]
    assert alb["origen"] == "manual"
    assert alb["proveedor_id"] == "prov-1"
    assert alb["numero_albaran"] == "3950"
    assert alb["fecha"] == "2026-05-04"
    assert alb["forma_pago"] == "15 días"
    # total = 12*1.81 + 3*22.73 = 21.72 + 68.19 = 89.91
    assert alb["total"] == pytest.approx(89.91, abs=0.01)
    assert len(fake_db.lineas) == 2
    assert not m.flujo_activo(CHAT)  # flujo cerrado


async def test_total_manual_sobrescribe(fake_db):
    await m.iniciar(CHAT)
    await m.manejar_texto(CHAT, "1")
    await m.manejar_texto(CHAT, "04/05/2026")
    await m.manejar_texto(CHAT, "Tomate, 10, 1.00")
    await m.manejar_texto(CHAT, "FIN")
    await m.manejar_texto(CHAT, "12,50")   # total real distinto del calculado (10.00)
    await m.manejar_texto(CHAT, "NO")
    await m.manejar_texto(CHAT, "NO")
    await m.manejar_texto(CHAT, "OK")
    assert fake_db.albaranes[0]["total"] == pytest.approx(12.50, abs=0.01)
    assert fake_db.albaranes[0]["base_imponible"] == pytest.approx(10.00, abs=0.01)
    assert fake_db.albaranes[0]["total_iva"] == pytest.approx(2.50, abs=0.01)


async def test_total_explicito_base_iva_total_y_resumen(fake_db):
    await m.iniciar(CHAT)
    await m.manejar_texto(CHAT, "1")
    await m.manejar_texto(CHAT, "04/05/2026")
    await m.manejar_texto(CHAT, "Producto, 3, 31,55")
    await m.manejar_texto(CHAT, "FIN")
    respuesta = await m.manejar_texto(CHAT, "94,65 / 3,78 / 98,43")
    assert "94,65€ + IVA 3,78€ = 98,43€" in respuesta
    await m.manejar_texto(CHAT, "NO")
    resumen = await m.manejar_texto(CHAT, "NO")
    assert "Base: 94,65€ + IVA: 3,78€ = Total: 98,43€" in resumen
    await m.manejar_texto(CHAT, "OK")
    assert fake_db.albaranes[0]["base_imponible"] == pytest.approx(94.65)
    assert fake_db.albaranes[0]["total_iva"] == pytest.approx(3.78)
    assert fake_db.albaranes[0]["total"] == pytest.approx(98.43)


async def test_producto_manual_detallado_conserva_columnas_observadas(fake_db):
    await m.iniciar(CHAT)
    await m.manejar_texto(CHAT, "1")
    await m.manejar_texto(CHAT, "04/05/2026")
    respuesta = await m.manejar_texto(CHAT, "Tomate | 10 | 2,00 | 10 | 1,80 | 18,00")
    assert "Tomate" in respuesta
    await m.manejar_texto(CHAT, "FIN")
    await m.manejar_texto(CHAT, "IVA 1,80")
    await m.manejar_texto(CHAT, "NO")
    resumen = await m.manejar_texto(CHAT, "NO")
    assert "tarifa 2,00€ − 10% → neto 1,80€ = 18,00€" in resumen
    await m.manejar_texto(CHAT, "OK")
    line = fake_db.lineas[0]
    assert line["descuento_pct"] == 10
    assert line["valores_observados"]["precio_tarifa"] == 2
    assert line["importe_neto"] == 18


async def test_total_inconsistente_no_avanza_y_permite_volver_a_productos(fake_db):
    await m.iniciar(CHAT)
    await m.manejar_texto(CHAT, "1")
    await m.manejar_texto(CHAT, "04/05/2026")
    await m.manejar_texto(CHAT, "Producto, 1, 10")
    await m.manejar_texto(CHAT, "FIN")
    respuesta = await m.manejar_texto(CHAT, "10 / 2 / 20")
    assert "no cuadran" in respuesta.lower()
    assert m._manual_flows[CHAT]["step"] == "total"
    respuesta = await m.manejar_texto(CHAT, "ATRÁS")
    assert "cargo como una línea" in respuesta
    assert m._manual_flows[CHAT]["step"] == "productos"


# ── /corregir elimina la última línea ───────────────────────────────────────────

async def test_corregir_elimina_ultima(fake_db):
    await m.iniciar(CHAT)
    await m.manejar_texto(CHAT, "1")
    await m.manejar_texto(CHAT, "04/05/2026")
    await m.manejar_texto(CHAT, "Tomate, 12, 1.81")
    await m.manejar_texto(CHAT, "Error producto, 5, 9.99")
    r = m.corregir_ultimo(CHAT)
    assert "Error producto" in r
    flow = m._manual_flows[CHAT]
    assert len(flow["lineas"]) == 1
    assert flow["lineas"][0]["nombre"] == "Tomate"


# ── /cancelar aborta sin insertar ───────────────────────────────────────────────

async def test_cancelar_aborta(fake_db):
    await m.iniciar(CHAT)
    await m.manejar_texto(CHAT, "1")
    await m.manejar_texto(CHAT, "04/05/2026")
    await m.manejar_texto(CHAT, "Tomate, 12, 1.81")
    msg = m.cancelar(CHAT)
    assert "cancelad" in msg.lower()
    assert not m.flujo_activo(CHAT)
    assert len(fake_db.albaranes) == 0


# ── Alta de proveedor nuevo ─────────────────────────────────────────────────────

async def test_proveedor_nuevo(fake_db):
    await m.iniciar(CHAT)
    await m.manejar_texto(CHAT, "Pescados Nuevos S.L.")  # nombre no existente
    await m.manejar_texto(CHAT, "B73623910")             # NIF válido
    await m.manejar_texto(CHAT, "30 días")               # forma de pago
    await m.manejar_texto(CHAT, "100 / 01/06/2026")
    await m.manejar_texto(CHAT, "Merluza, 5, 10.00")
    await m.manejar_texto(CHAT, "FIN")
    await m.manejar_texto(CHAT, "OK")
    await m.manejar_texto(CHAT, "NO")
    await m.manejar_texto(CHAT, "NO")
    await m.manejar_texto(CHAT, "OK")
    assert any(p["nombre"] == "Pescados Nuevos S.L." for p in fake_db.proveedores)
    assert fake_db.albaranes[0]["origen"] == "manual"


# ── Foto opcional ───────────────────────────────────────────────────────────────

async def test_foto_opcional(fake_db):
    await m.iniciar(CHAT)
    await m.manejar_texto(CHAT, "1")
    await m.manejar_texto(CHAT, "04/05/2026")
    await m.manejar_texto(CHAT, "Tomate, 12, 1.81")
    await m.manejar_texto(CHAT, "FIN")
    await m.manejar_texto(CHAT, "OK")
    await m.manejar_texto(CHAT, "NO")
    r = await m.manejar_foto(CHAT, _png())
    assert "resumen" in r.lower()
    await m.manejar_texto(CHAT, "OK")
    assert fake_db.albaranes[0]["imagen_url"].startswith("manual/")


async def test_manual_sustituye_revision_ocr_y_reutiliza_original(fake_db):
    ingestion_id = "11111111-2222-3333-4444-555555555555"
    fake_db.ingestions[ingestion_id] = {
        "id": ingestion_id, "status": "needs_review", "storage_bucket": "albaranes",
        "storage_path": "intake/original.jpg", "metadata": {},
    }
    await m.iniciar_desde_ingestion(CHAT, CHAT, ingestion_id)
    await m.manejar_texto(CHAT, "1")
    await m.manejar_texto(CHAT, "04/05/2026")
    await m.manejar_texto(CHAT, "Producto, 2, 5")
    await m.manejar_texto(CHAT, "FIN")
    await m.manejar_texto(CHAT, "OK")
    resumen = await m.manejar_texto(CHAT, "NO")
    assert "Con foto adjunta" in resumen
    final = await m.manejar_texto(CHAT, "OK")

    assert "guardado" in final.lower()
    assert fake_db.resolved_reviews[0][1]["status"] == "rejected"
    assert any(args[0] == "candidate.replaced_by_manual" for args, _ in fake_db.audit_events)
    assert fake_db.ingestions[ingestion_id]["status"] == "confirmed"


# ── Duplicado detectado en el segundo intento ───────────────────────────────────

async def test_duplicado_detectado(fake_db):
    async def registrar():
        await m.iniciar(CHAT)
        await m.manejar_texto(CHAT, "1")
        await m.manejar_texto(CHAT, "07/06/2026")
        await m.manejar_texto(CHAT, "Tomate, 10, 2.00")
        await m.manejar_texto(CHAT, "FIN")
        await m.manejar_texto(CHAT, "OK")
        await m.manejar_texto(CHAT, "NO")
        await m.manejar_texto(CHAT, "NO")
        return await m.manejar_texto(CHAT, "OK")

    r1 = await registrar()
    assert "guardado" in r1.lower()
    r2 = await registrar()
    assert "posible duplicado" in r2.lower()
    assert "otra entrega legítima" in r2.lower()
    m.cancelar(CHAT)
    assert len(fake_db.albaranes) == 1  # no se duplicó
