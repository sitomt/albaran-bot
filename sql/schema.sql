-- =============================================================
-- Albaran Bot — Schema Supabase
-- Proyecto: Chatbot albaranes (tdyeivstcmtbmzuzrimd)
-- =============================================================

-- 1. proveedores
CREATE TABLE IF NOT EXISTS proveedores (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nombre TEXT NOT NULL,
    nif TEXT UNIQUE NOT NULL,
    -- Columnas generadas automáticamente por PostgreSQL (no escribir desde Python)
    nombre_normalizado TEXT GENERATED ALWAYS AS (LOWER(TRIM(nombre))) STORED,
    nif_normalizado TEXT GENERATED ALWAYS AS (UPPER(REGEXP_REPLACE(nif, '[^A-Z0-9]', '', 'g'))) STORED,
    direccion TEXT,
    telefono TEXT,
    email TEXT,
    forma_pago_habitual TEXT,
    creado_en TIMESTAMPTZ DEFAULT now()
);

-- 2. productos_catalogo (depende de proveedores)
CREATE TABLE IF NOT EXISTS productos_catalogo (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nombre_normalizado TEXT NOT NULL,
    proveedor_id UUID REFERENCES proveedores(id) ON DELETE CASCADE,
    variantes JSONB DEFAULT '[]',
    unidad_base TEXT,
    formato_habitual TEXT,
    precio_ultima_compra NUMERIC(10,4),
    precio_medio_historico NUMERIC(10,4),
    ultima_compra_fecha DATE,                -- fecha de la última actualización de precio
    creado_en TIMESTAMPTZ DEFAULT now(),
    UNIQUE(nombre_normalizado, proveedor_id)
);

-- 3. albaranes (depende de proveedores)
CREATE TABLE IF NOT EXISTS albaranes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    numero_albaran TEXT,
    -- Columna generada automáticamente por PostgreSQL (no escribir desde Python)
    numero_albaran_norm TEXT GENERATED ALWAYS AS (
        REGEXP_REPLACE(LOWER(TRIM(COALESCE(numero_albaran, ''))), '[^a-z0-9]', '', 'g')
    ) STORED,
    fecha DATE,
    proveedor_id UUID REFERENCES proveedores(id),
    forma_pago TEXT,
    base_imponible NUMERIC(10,2),
    total_iva NUMERIC(10,2),
    detalle_iva JSONB,                       -- [{tipo, base, cuota}] por tramo de IVA
    total NUMERIC(10,2),
    imagen_url TEXT,
    imagen_hash TEXT,                        -- SHA-256 de la foto (unicidad vía índice parcial abajo)
    origen TEXT DEFAULT 'ocr',               -- 'ocr' (foto procesada) | 'manual' (alta por /manual)
    creado_en TIMESTAMPTZ DEFAULT now()
);

-- 4. lineas_albaran (depende de albaranes y productos_catalogo)
CREATE TABLE IF NOT EXISTS lineas_albaran (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    albaran_id UUID REFERENCES albaranes(id) ON DELETE CASCADE,
    producto_catalogo_id UUID REFERENCES productos_catalogo(id),
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

-- 5. auditoria
CREATE TABLE IF NOT EXISTS auditoria (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tipo TEXT NOT NULL,
    albaran_id UUID REFERENCES albaranes(id) ON DELETE SET NULL,  -- FK al albarán procesado
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

-- 6. jobs (cola de procesamiento — se limpian automáticamente al arrancar)
CREATE TABLE IF NOT EXISTS jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_user_id BIGINT,
    imagen_url TEXT,
    estado TEXT DEFAULT 'pendiente' CHECK (estado IN ('pendiente','procesando','completado','error')),
    intentos INT DEFAULT 0,
    error_detalle TEXT,
    creado_en TIMESTAMPTZ DEFAULT now(),
    actualizado_en TIMESTAMPTZ DEFAULT now()
);

-- 7. correcciones (auditoría de correcciones manuales)
CREATE TABLE IF NOT EXISTS correcciones (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    linea_albaran_id UUID REFERENCES lineas_albaran(id) ON DELETE CASCADE,
    campo TEXT NOT NULL,
    valor_original TEXT,
    valor_corregido TEXT NOT NULL,
    corregido_por TEXT DEFAULT 'usuario',
    creado_en TIMESTAMPTZ DEFAULT now()
);

-- Índices
CREATE INDEX IF NOT EXISTS idx_albaranes_fecha ON albaranes(fecha DESC);
CREATE INDEX IF NOT EXISTS idx_albaranes_proveedor_fecha ON albaranes(proveedor_id, fecha DESC);
CREATE INDEX IF NOT EXISTS idx_albaranes_num_norm ON albaranes(numero_albaran_norm);

-- ── Deduplicación a nivel BD (backstop real del catch 23505; imprescindible bajo
--    concurrencia de workers). Estos índices DEBEN existir en cualquier reprovisión.
-- 1) Misma foto exacta (hash SHA-256).
CREATE UNIQUE INDEX IF NOT EXISTS idx_albaranes_imagen_hash
    ON albaranes(imagen_hash) WHERE imagen_hash IS NOT NULL;
-- 2) Mismo proveedor + mismo número de albarán (clave fuerte para albaranes numerados).
CREATE UNIQUE INDEX IF NOT EXISTS uniq_albaran_prov_numnorm
    ON albaranes(proveedor_id, numero_albaran_norm) WHERE numero_albaran_norm <> '';
-- 3) Mismo proveedor + fecha + total exacto (cubre albaranes sin número).
--    NOTA: match EXACTO de total; dos entregas distintas con idéntico total el mismo
--    día se tratarían como duplicado (raro). La capa Python añade tolerancia ±0,50€.
CREATE UNIQUE INDEX IF NOT EXISTS idx_albaran_duplicado
    ON albaranes(proveedor_id, fecha, total) WHERE total IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_lineas_producto ON lineas_albaran(producto_catalogo_id);
CREATE INDEX IF NOT EXISTS idx_productos_proveedor ON productos_catalogo(proveedor_id);
CREATE INDEX IF NOT EXISTS idx_proveedores_nombre_norm ON proveedores(nombre_normalizado);
CREATE INDEX IF NOT EXISTS idx_proveedores_nif_norm ON proveedores(nif_normalizado);

-- RPC function para query_engine (SELECT dinámico seguro)
CREATE OR REPLACE FUNCTION execute_select(query text)
RETURNS json
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE result json;
BEGIN
    IF upper(trim(query)) NOT LIKE 'SELECT%' THEN
        RAISE EXCEPTION 'Solo se permiten consultas SELECT';
    END IF;
    EXECUTE 'SELECT json_agg(t) FROM (' || query || ') t' INTO result;
    RETURN COALESCE(result, '[]'::json);
END;
$$;
