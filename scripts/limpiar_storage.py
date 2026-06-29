"""
Limpieza única del bucket de Storage de albaranes.

Lista TODAS las fotos del bucket, muestra el recuento, pide confirmación por consola
y las borra todas. Pensado para ejecutarse una sola vez para eliminar las fotos
huérfanas/duplicadas acumuladas durante los tests.

Uso (desde la raíz del proyecto):
    python scripts/limpiar_storage.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Permite importar el paquete `src` al ejecutar el script directamente
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import supabase_client as db  # noqa: E402

_BUCKET = "albaranes"


async def main() -> None:
    print(f"Listando archivos del bucket '{_BUCKET}'...")
    rutas = await db.listar_archivos_storage(_BUCKET)
    total = len(rutas)

    if total == 0:
        print("El bucket ya está vacío. Nada que borrar.")
        return

    print(f"Se encontraron {total} archivos. Ejemplos:")
    for r in rutas[:10]:
        print(f"  - {r}")
    if total > 10:
        print(f"  ... y {total - 10} más")

    respuesta = input(
        f"\n¿Borrar TODOS los {total} archivos del bucket '{_BUCKET}'? "
        "Esta acción no se puede deshacer. Escribe 'BORRAR' para confirmar: "
    ).strip()

    if respuesta != "BORRAR":
        print("Cancelado. No se ha borrado nada.")
        return

    borrados = await db.borrar_archivos_storage(_BUCKET, rutas)
    print(f"Listo. Se borraron {borrados} archivos.")

    # Verificación
    restantes = await db.listar_archivos_storage(_BUCKET)
    print(f"Archivos restantes en el bucket: {len(restantes)}")


if __name__ == "__main__":
    asyncio.run(main())
