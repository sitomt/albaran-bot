"""
Tests del flujo de entrada manual de albaranes (/manual) con un `db` simulado en memoria.
Cubre: parsers, camino feliz con proveedor existente, alta de proveedor nuevo,
/corregir, /cancelar, foto opcional y detección de duplicados.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import manual_albaran as m  # noqa: E402


# ── Fake DB en memoria ──────────────────────────────────────────────────────────

class FakeDB:
    def __init__(self):
        self.proveedores = [{"id": "prov-1", "nombre": "Lucas Caballero S.L."}]
        self.albaranes = []
        self.lineas = []
        self.precios = {}

    async def listar_todos_proveedores(self):
        return [{"id": p["id"], "nombre": p["nombre"]} for p in self.proveedores]

    async def buscar_o_crear_proveedor(self, nombre, nif=None, direccion=None,
                                       telefono=None, email=None, forma_pago_habitual=None):
        existente = next((p for p in self.proveedores if p["nombre"].lower() == nombre.lower()), None)
        if existente:
            return existente, False
        nuevo = {"id": f"prov-{len(self.proveedores)+1}", "nombre": nombre,
                 "nif": nif, "forma_pago_habitual": forma_pago_habitual}
        self.proveedores.append(nuevo)
        return nuevo, True

    async def buscar_o_crear_producto_catalogo(self, proveedor_id, nombre_normalizado,
                                               unidad_base=None, formato_habitual=None):
        return {"id": f"cat-{uuid.uuid4().hex[:6]}", "nombre_normalizado": nombre_normalizado}

    async def insertar_albaran(self, **kw):
        row = {"id": f"alb-{len(self.albaranes)+1}", **kw}
        self.albaranes.append(row)
        return row

    async def insertar_lineas(self, lineas):
        self.lineas.extend(lineas)
        return lineas

    async def actualizar_precio_catalogo(self, producto_id, precio):
        self.precios[producto_id] = precio
        return None, False

    async def subir_imagen(self, bucket, path, data, content_type="image/jpeg"):
        return f"https://fake/{path}"

    async def buscar_albaran_duplicado_combinacion(self, proveedor_id, fecha, total):
        return next((a for a in self.albaranes
                     if a["proveedor_id"] == proveedor_id and a["fecha"] == fecha
                     and abs((a["total"] or 0) - total) <= 0.50), None)

    async def buscar_albaran_duplicado_por_nombre_proveedor(self, nombre, fecha, total):
        return None

    async def buscar_albaran_duplicado_norm(self, numero_norm, proveedor_id):
        return None


@pytest.fixture
def fake_db(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(m, "db", db)
    m._manual_flows.clear()
    return db


CHAT = 12345


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


# ── Camino feliz: proveedor existente ───────────────────────────────────────────

async def test_flujo_completo_proveedor_existente(fake_db):
    await m.iniciar(CHAT)
    assert m.flujo_activo(CHAT)

    await m.manejar_texto(CHAT, "1")                       # proveedor por número
    await m.manejar_texto(CHAT, "3950 / 04-05-2026")        # cabecera
    await m.manejar_texto(CHAT, "Tomate entero, 12, 1.81")  # producto 1
    await m.manejar_texto(CHAT, "Anchoa, 3, 22,73")         # producto 2
    await m.manejar_texto(CHAT, "FIN")
    r = await m.manejar_texto(CHAT, "OK")                   # total OK (acepta calculado)
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
    await m.manejar_texto(CHAT, "B12345678")             # NIF
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
    r = await m.manejar_foto(CHAT, b"fake-bytes")
    assert "resumen" in r.lower()
    await m.manejar_texto(CHAT, "OK")
    assert fake_db.albaranes[0]["imagen_url"].startswith("https://fake/")


# ── Foto reaprovechada tras OCR fallido (botón "Introducir a mano") ──────────────

async def test_foto_reaprovechada_salta_paso_foto(fake_db):
    # Simula: el OCR falló y guardamos la foto; el usuario pulsa "Introducir a mano".
    m.recordar_foto_fallida(CHAT, b"foto-del-albaran")
    intro = await m.iniciar(CHAT)
    assert "foto que enviaste" in intro.lower()
    await m.manejar_texto(CHAT, "1")
    await m.manejar_texto(CHAT, "100 / 01/06/2026")
    await m.manejar_texto(CHAT, "Merluza, 5, 10.00")
    await m.manejar_texto(CHAT, "FIN")
    # Tras la forma de pago NO debe preguntar por foto (ya la tenemos) → resumen directo
    await m.manejar_texto(CHAT, "OK")          # total
    resumen = await m.manejar_texto(CHAT, "30 días")  # forma de pago → resumen
    assert "resumen" in resumen.lower()
    assert "foto adjunta" in resumen.lower()
    final = await m.manejar_texto(CHAT, "OK")
    assert "guardado" in final.lower()
    assert fake_db.albaranes[0]["imagen_url"].startswith("https://fake/")
    assert fake_db.albaranes[0]["origen"] == "manual"


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
    assert "ya estaba registrado" in r2.lower()
    assert len(fake_db.albaranes) == 1  # no se duplicó
