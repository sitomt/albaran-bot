"""Entrada manual rápida para el albarán que el OCR no puede leer.

El caso: un albarán manuscrito cuyo membrete, número y fecha salen impresos y se
leen sin problema, pero cuyas líneas van a mano y no hay quien las entienda.

Antes eran unas once idas y vueltas —y la primera pregunta era el proveedor, que
el bot tenía delante en pantalla—. Estos tests fijan las tres cosas que lo
acortan: aprovechar la cabecera ya leída, aceptar todos los productos en un solo
mensaje, y poder guardar solo el total cuando no hay nada legible.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import manual_albaran as m  # noqa: E402
from test_manual_flow import CHAT, FakeDB  # noqa: E402


CABECERA_LEIDA = {
    "header": {
        "proveedor_nombre": "Matadero INTESA", "proveedor_nif": "A-03117231",
        "numero_albaran": "0734079", "fecha": "2026-09-02",
    },
    "lines": [],
}
INGESTION = "2" * 36


@pytest.fixture
def db_falsa(monkeypatch):
    db = FakeDB()
    db.ingestions[INGESTION] = {
        "id": INGESTION, "status": "needs_review", "storage_bucket": "albaranes",
        "storage_path": "intake/original.jpg",
        "metadata": {"candidate_artifact_id": "a" * 36},
    }
    monkeypatch.setattr(m, "db", db)
    m._manual_flows.clear()
    return db


@pytest.fixture
def con_candidato(monkeypatch, db_falsa):
    import src.ingestion_service as ingestion_service
    monkeypatch.setattr(
        ingestion_service, "load_candidate",
        AsyncMock(return_value=(CABECERA_LEIDA, "a" * 36)),
    )
    return db_falsa


def _flow():
    return m._manual_flows[CHAT]


# ── A · No volver a preguntar lo que el OCR ya leyó ──────────────────────────

@pytest.mark.asyncio
async def test_la_cabecera_leida_se_da_por_puesta_y_se_salta_al_grano(con_candidato):
    texto = await m.iniciar_desde_ingestion(CHAT, CHAT, INGESTION)

    assert "Matadero INTESA" in texto and "0734079" in texto and "2026-09-02" in texto
    assert _flow()["step"] == "productos"          # ni proveedor, ni número, ni fecha
    assert _flow()["proveedor_nombre"] == "Matadero INTESA"
    assert _flow()["numero_albaran"] == "0734079"
    assert _flow()["fecha"] == "2026-09-02"


@pytest.mark.asyncio
async def test_un_proveedor_ya_conocido_se_reconoce_y_no_se_duplica(con_candidato):
    con_candidato.proveedores.append({"id": "prov-9", "nombre": "Matadero INTESA"})

    await m.iniciar_desde_ingestion(CHAT, CHAT, INGESTION)

    assert _flow()["proveedor_id"] == "prov-9"
    assert _flow()["_nuevo"] is None


@pytest.mark.asyncio
async def test_un_proveedor_nuevo_se_da_de_alta_con_su_nif_sin_preguntarlo(con_candidato):
    """El NIF ya viene validado por dígito de control desde la extracción, así
    que volver a pedirlo solo añade un mensaje y una ocasión de teclearlo mal."""
    await m.iniciar_desde_ingestion(CHAT, CHAT, INGESTION)

    assert _flow()["_nuevo"] == {
        "nombre": "Matadero INTESA", "nif": "A-03117231", "forma_pago": None,
    }


@pytest.mark.asyncio
async def test_si_falta_la_fecha_se_pregunta_solo_la_fecha(con_candidato, monkeypatch):
    import src.ingestion_service as ingestion_service
    sin_fecha = {"header": {**CABECERA_LEIDA["header"], "fecha": None}, "lines": []}
    monkeypatch.setattr(
        ingestion_service, "load_candidate", AsyncMock(return_value=(sin_fecha, "a" * 36))
    )

    texto = await m.iniciar_desde_ingestion(CHAT, CHAT, INGESTION)

    assert _flow()["step"] == "fecha"
    assert "fecha" in texto.lower()
    assert _flow()["numero_albaran"] == "0734079"       # lo demás no se vuelve a pedir


@pytest.mark.asyncio
async def test_hoy_vale_como_fecha(con_candidato, monkeypatch):
    import src.ingestion_service as ingestion_service
    from datetime import datetime
    sin_fecha = {"header": {**CABECERA_LEIDA["header"], "fecha": None}, "lines": []}
    monkeypatch.setattr(
        ingestion_service, "load_candidate", AsyncMock(return_value=(sin_fecha, "a" * 36))
    )
    await m.iniciar_desde_ingestion(CHAT, CHAT, INGESTION)

    await m.manejar_texto(CHAT, "hoy")

    assert _flow()["fecha"] == datetime.now().date().isoformat()
    assert _flow()["step"] == "productos"


@pytest.mark.asyncio
async def test_sin_candidato_se_cae_al_cuestionario_de_siempre(db_falsa):
    """Un OCR que se cayó del todo no deja cabecera que reutilizar. No puede
    romper: tiene que preguntar como antes."""
    db_falsa.ingestions[INGESTION]["metadata"] = {}

    await m.iniciar_desde_ingestion(CHAT, CHAT, INGESTION)

    assert _flow()["step"] == "proveedor"


# ── B · Todos los productos en un solo mensaje ───────────────────────────────

@pytest.mark.asyncio
async def test_los_productos_van_todos_en_un_mensaje(con_candidato):
    await m.iniciar_desde_ingestion(CHAT, CHAT, INGESTION)

    respuesta = await m.manejar_texto(CHAT, (
        "Pollos 15,4 2,75\n"
        "Pechugas 3,2 5,70\n"
        "Conejos 9,1 7\n"
        "Sangre cocida 4 2\n"
        "FIN"
    ))

    assert len(_flow()["lineas"]) == 4
    assert _flow()["step"] == "total"                 # el FIN del final ya cierra
    assert m._total_lineas(_flow()) == 132.29
    assert "132,29" in respuesta


@pytest.mark.asyncio
async def test_una_linea_ilegible_no_tira_las_demas(con_candidato):
    """Rechazar el mensaje entero por un renglón obligaría a reescribir los
    buenos, que es justo lo que veníamos a evitar."""
    await m.iniciar_desde_ingestion(CHAT, CHAT, INGESTION)

    respuesta = await m.manejar_texto(CHAT, (
        "Pollos 15,4 2,75\n"
        "esto no hay quien lo lea\n"
        "Pechugas 3,2 5,70"
    ))

    assert len(_flow()["lineas"]) == 2
    assert "esto no hay quien lo lea" in respuesta
    assert _flow()["step"] == "productos"             # sigue esperando lo que falló


@pytest.mark.asyncio
async def test_con_lineas_rechazadas_no_se_pasa_de_paso_aunque_pongas_fin(con_candidato):
    """Si algo no se entendió, cerrar con FIN dejaría el albarán incompleto sin
    que nadie se entere."""
    await m.iniciar_desde_ingestion(CHAT, CHAT, INGESTION)

    await m.manejar_texto(CHAT, "Pollos 15,4 2,75\nilegible\nFIN")

    assert _flow()["step"] == "productos"


@pytest.mark.asyncio
async def test_se_pueden_seguir_mandando_de_uno_en_uno(con_candidato):
    """El modo antiguo tiene que seguir funcionando: hay quien escribe así."""
    await m.iniciar_desde_ingestion(CHAT, CHAT, INGESTION)

    await m.manejar_texto(CHAT, "Tomate entero, 12, 1.81")
    respuesta = await m.manejar_texto(CHAT, "Cebolla, 3, 0,90")

    assert len(_flow()["lineas"]) == 2
    assert "Llevas 2 productos" in respuesta


@pytest.mark.asyncio
async def test_las_columnas_detalladas_conviven_con_las_simples(con_candidato):
    await m.iniciar_desde_ingestion(CHAT, CHAT, INGESTION)

    await m.manejar_texto(CHAT, (
        "Tomate | 10 | 2,00 | 10 | 1,80 | 18,00\n"
        "Cebolla 3 0,90\n"
        "FIN"
    ))

    assert [l["entrada_detallada"] for l in _flow()["lineas"]] == [True, False]


@pytest.mark.asyncio
async def test_cabecera_vuelve_atras_sin_perder_los_productos(con_candidato):
    await m.iniciar_desde_ingestion(CHAT, CHAT, INGESTION)
    await m.manejar_texto(CHAT, "Pollos 15,4 2,75")

    await m.manejar_texto(CHAT, "CABECERA")

    assert _flow()["step"] == "cabecera"
    assert len(_flow()["lineas"]) == 1


# ── E · El albarán ilegible: guardar solo el total ───────────────────────────

@pytest.mark.asyncio
async def test_sin_detalle_guarda_el_albaran_con_una_sola_linea(con_candidato):
    """Un albarán con proveedor, fecha y total ya sirve para controlar el gasto.
    La alternativa real no es tener el detalle: es que nadie registre el papel."""
    await m.iniciar_desde_ingestion(CHAT, CHAT, INGESTION)

    respuesta = await m.manejar_texto(CHAT, "SIN DETALLE 132,29")

    assert _flow()["step"] == "total"
    assert len(_flow()["lineas"]) == 1
    assert _flow()["lineas"][0]["nombre"] == m.LINEA_SIN_DETALLAR
    assert _flow()["lineas"][0]["importe"] == 132.29
    assert "no para las consultas por producto" in respuesta   # se dice lo que se pierde


@pytest.mark.asyncio
async def test_sin_detalle_admite_iva_encima(con_candidato):
    await m.iniciar_desde_ingestion(CHAT, CHAT, INGESTION)
    await m.manejar_texto(CHAT, "SIN DETALLE 132,29")

    await m.manejar_texto(CHAT, "IVA 13,23")

    assert _flow()["base_manual"] == 132.29
    assert _flow()["iva_manual"] == 13.23
    assert _flow()["total_manual"] == 145.52


@pytest.mark.asyncio
async def test_solo_total_es_la_misma_orden(con_candidato):
    await m.iniciar_desde_ingestion(CHAT, CHAT, INGESTION)

    await m.manejar_texto(CHAT, "solo el total 90")

    assert _flow()["lineas"][0]["importe"] == 90.0


@pytest.mark.asyncio
async def test_sin_detalle_sin_importe_pide_el_importe(con_candidato):
    await m.iniciar_desde_ingestion(CHAT, CHAT, INGESTION)

    respuesta = await m.manejar_texto(CHAT, "SIN DETALLE")

    assert "importe total" in respuesta
    assert _flow()["step"] == "productos"


# ── El recorrido entero, contando mensajes ───────────────────────────────────

@pytest.mark.asyncio
async def test_el_albaran_completo_se_registra_en_tres_mensajes(con_candidato):
    """Botón + productos + OK + OK. La forma de pago no se pregunta porque el
    proveedor conocido ya la tiene guardada, y la foto ya está subida."""
    con_candidato.proveedores.append({
        "id": "prov-9", "nombre": "Matadero INTESA", "forma_pago_habitual": "30 días",
    })
    await m.iniciar_desde_ingestion(CHAT, CHAT, INGESTION)

    mensajes = ["Pollos 15,4 2,75\nPechugas 3,2 5,70\nConejos 9,1 7\nSangre cocida 4 2\nFIN", "OK"]
    for mensaje in mensajes:
        respuesta = await m.manejar_texto(CHAT, mensaje)

    assert "30 días" in respuesta            # no la ha preguntado, la ha reutilizado
    assert _flow()["step"] == "confirmacion"
    final = await m.manejar_texto(CHAT, "OK")
    assert "guardado" in final.lower()
    assert len(con_candidato.albaranes) == 1
    assert con_candidato.albaranes[0]["total"] == 132.29


@pytest.mark.asyncio
async def test_el_albaran_ilegible_se_registra_en_tres_mensajes(con_candidato):
    con_candidato.proveedores.append({
        "id": "prov-9", "nombre": "Matadero INTESA", "forma_pago_habitual": "30 días",
    })
    await m.iniciar_desde_ingestion(CHAT, CHAT, INGESTION)

    await m.manejar_texto(CHAT, "SIN DETALLE 132,29")
    await m.manejar_texto(CHAT, "OK")
    final = await m.manejar_texto(CHAT, "OK")

    assert "guardado" in final.lower()
    assert con_candidato.albaranes[0]["total"] == 132.29
    assert len(con_candidato.lineas) == 1
