-- Idempotent baseline for fresh installations and pre-migration environments.
-- Production hardening, RLS and RPC contracts are applied by 001.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS public.proveedores (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nombre TEXT NOT NULL,
    nif TEXT UNIQUE NOT NULL,
    nombre_normalizado TEXT GENERATED ALWAYS AS (lower(trim(nombre))) STORED,
    nif_normalizado TEXT GENERATED ALWAYS AS (
        upper(regexp_replace(nif, '[^A-Z0-9]', '', 'g'))
    ) STORED,
    direccion TEXT,
    telefono TEXT,
    email TEXT,
    forma_pago_habitual TEXT,
    creado_en TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.productos_catalogo (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nombre_normalizado TEXT NOT NULL,
    proveedor_id UUID REFERENCES public.proveedores(id) ON DELETE CASCADE,
    variantes JSONB DEFAULT '[]'::jsonb,
    unidad_base TEXT,
    formato_habitual TEXT,
    precio_ultima_compra NUMERIC(10,4),
    precio_medio_historico NUMERIC(10,4),
    ultima_compra_fecha DATE,
    creado_en TIMESTAMPTZ DEFAULT now(),
    UNIQUE(nombre_normalizado, proveedor_id)
);

CREATE TABLE IF NOT EXISTS public.albaranes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    numero_albaran TEXT,
    numero_albaran_norm TEXT GENERATED ALWAYS AS (
        regexp_replace(lower(trim(coalesce(numero_albaran, ''))), '[^a-z0-9]', '', 'g')
    ) STORED,
    fecha DATE,
    proveedor_id UUID REFERENCES public.proveedores(id),
    forma_pago TEXT,
    base_imponible NUMERIC(10,2),
    total_iva NUMERIC(10,2),
    detalle_iva JSONB,
    total NUMERIC(10,2),
    imagen_url TEXT,
    imagen_hash TEXT,
    origen TEXT DEFAULT 'ocr',
    creado_en TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.lineas_albaran (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    albaran_id UUID REFERENCES public.albaranes(id) ON DELETE CASCADE,
    producto_catalogo_id UUID REFERENCES public.productos_catalogo(id),
    descripcion_original TEXT,
    descripcion_limpia TEXT,
    cantidad NUMERIC(10,3),
    unidad TEXT,
    peso_unitario_g NUMERIC(10,2),
    unidades_por_envase INT,
    peso_total_kg NUMERIC(10,3),
    volumen_unitario_l NUMERIC(10,3),
    formato_envase TEXT,
    numero_lote TEXT,
    caducidad DATE,
    precio_unitario NUMERIC(10,4),
    descuento_pct NUMERIC(5,2),
    importe_neto NUMERIC(10,2),
    confianza INT DEFAULT 100,
    requiere_revision BOOLEAN DEFAULT false
);

CREATE TABLE IF NOT EXISTS public.auditoria (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tipo TEXT NOT NULL,
    albaran_id UUID REFERENCES public.albaranes(id) ON DELETE SET NULL,
    telegram_user_id BIGINT,
    imagen_url TEXT,
    modelo_ocr TEXT,
    modelo_llm TEXT,
    tokens_consumidos INT,
    coste_estimado_usd NUMERIC(10,6),
    resultado TEXT CHECK (resultado IN ('ok','error','revision')),
    detalle JSONB,
    creado_en TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_user_id BIGINT,
    imagen_url TEXT,
    estado TEXT DEFAULT 'pendiente'
        CHECK (estado IN ('pendiente','procesando','completado','error')),
    intentos INT DEFAULT 0,
    error_detalle TEXT,
    creado_en TIMESTAMPTZ DEFAULT now(),
    actualizado_en TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.correcciones (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    linea_albaran_id UUID REFERENCES public.lineas_albaran(id) ON DELETE CASCADE,
    campo TEXT NOT NULL,
    valor_original TEXT,
    valor_corregido TEXT NOT NULL,
    corregido_por TEXT DEFAULT 'usuario',
    creado_en TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_albaranes_fecha ON public.albaranes(fecha DESC);
CREATE INDEX IF NOT EXISTS idx_albaranes_proveedor_fecha
    ON public.albaranes(proveedor_id, fecha DESC);
CREATE INDEX IF NOT EXISTS idx_albaranes_num_norm ON public.albaranes(numero_albaran_norm);
CREATE UNIQUE INDEX IF NOT EXISTS idx_albaranes_imagen_hash
    ON public.albaranes(imagen_hash) WHERE imagen_hash IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uniq_albaran_prov_numnorm
    ON public.albaranes(proveedor_id, numero_albaran_norm)
    WHERE numero_albaran_norm <> '';
CREATE INDEX IF NOT EXISTS idx_lineas_producto ON public.lineas_albaran(producto_catalogo_id);
CREATE INDEX IF NOT EXISTS idx_lineas_albaran_id ON public.lineas_albaran(albaran_id);
CREATE INDEX IF NOT EXISTS idx_auditoria_albaran ON public.auditoria(albaran_id);
CREATE INDEX IF NOT EXISTS idx_correcciones_linea ON public.correcciones(linea_albaran_id);
CREATE INDEX IF NOT EXISTS idx_productos_proveedor ON public.productos_catalogo(proveedor_id);
CREATE INDEX IF NOT EXISTS idx_proveedores_nombre_norm ON public.proveedores(nombre_normalizado);
CREATE INDEX IF NOT EXISTS idx_proveedores_nif_norm ON public.proveedores(nif_normalizado);
