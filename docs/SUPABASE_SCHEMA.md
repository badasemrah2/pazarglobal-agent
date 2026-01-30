# Supabase Database Schema

## 📊 Tablolar

### profiles (Kullanıcılar)

```sql
CREATE TABLE profiles (
    id UUID PRIMARY KEY REFERENCES auth.users(id),
    phone TEXT,
    display_name TEXT,
    full_name TEXT,
    avatar_url TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
```

### active_drafts (İlan Taslakları)

```sql
CREATE TABLE active_drafts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    state TEXT DEFAULT 'in_progress',
    listing_data JSONB DEFAULT '{}',
    images JSONB DEFAULT '[]',
    vision_product JSONB DEFAULT '{}',
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    
    -- Her kullanıcının tek taslağı olabilir
    CONSTRAINT active_drafts_user_id_key UNIQUE (user_id)
);

-- listing_data JSONB yapısı:
-- {
--   "title": "iPhone 14 Pro Max 256GB",
--   "description": "Temiz, kutulu...",
--   "price": 45000,
--   "category": "Elektronik",
--   "condition": "2. El",
--   "location": "İstanbul"
-- }
```

### listings (Yayınlanan İlanlar)

```sql
CREATE TABLE listings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES profiles(id),
    title TEXT NOT NULL,
    description TEXT,
    price NUMERIC,
    category TEXT,
    condition TEXT,
    location TEXT,
    images JSONB DEFAULT '[]',
    keywords JSONB DEFAULT '[]',
    status TEXT DEFAULT 'active',
    view_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Indexes
CREATE INDEX idx_listings_user_id ON listings(user_id);
CREATE INDEX idx_listings_category ON listings(category);
CREATE INDEX idx_listings_status ON listings(status);
CREATE INDEX idx_listings_keywords ON listings USING GIN(keywords);
```

### wallets (Kredi Cüzdanları)

```sql
CREATE TABLE wallets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES profiles(id) UNIQUE,
    balance INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
```

### wallet_transactions (Kredi İşlemleri)

```sql
CREATE TABLE wallet_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    wallet_id UUID NOT NULL REFERENCES wallets(id),
    amount INTEGER NOT NULL,
    type TEXT NOT NULL, -- 'credit', 'debit'
    description TEXT,
    reference TEXT, -- 'publish_listing:uuid'
    created_at TIMESTAMPTZ DEFAULT now()
);
```

### market_price_snapshots (Fiyat Cache)

```sql
CREATE TABLE market_price_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_key TEXT NOT NULL,
    category TEXT,
    min_price NUMERIC,
    max_price NUMERIC,
    avg_price NUMERIC,
    currency TEXT DEFAULT 'TRY',
    source TEXT, -- 'perplexity', 'listings_avg', 'manual'
    raw_response JSONB,
    created_at TIMESTAMPTZ DEFAULT now(),
    expires_at TIMESTAMPTZ,
    
    -- Cache key
    CONSTRAINT market_price_unique_key UNIQUE (product_key, category)
);

-- Index for lookups
CREATE INDEX idx_market_price_product ON market_price_snapshots(product_key);
CREATE INDEX idx_market_price_expires ON market_price_snapshots(expires_at);
```

### audit_logs (İşlem Logları)

```sql
CREATE TABLE audit_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES profiles(id),
    action TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Index for queries
CREATE INDEX idx_audit_logs_user ON audit_logs(user_id);
CREATE INDEX idx_audit_logs_action ON audit_logs(action);
```

---

## 🔐 Row Level Security (RLS)

### listings RLS

```sql
-- Users can only see active listings or their own
ALTER TABLE listings ENABLE ROW LEVEL SECURITY;

CREATE POLICY listings_select_policy ON listings
    FOR SELECT USING (
        status = 'active' OR user_id = auth.uid()
    );

CREATE POLICY listings_insert_policy ON listings
    FOR INSERT WITH CHECK (user_id = auth.uid());

CREATE POLICY listings_update_policy ON listings
    FOR UPDATE USING (user_id = auth.uid());

CREATE POLICY listings_delete_policy ON listings
    FOR DELETE USING (user_id = auth.uid());
```

### active_drafts RLS

```sql
ALTER TABLE active_drafts ENABLE ROW LEVEL SECURITY;

CREATE POLICY drafts_all_policy ON active_drafts
    FOR ALL USING (user_id = auth.uid());
```

---

## 📦 Edge Functions

### ai-assistant-cached

Perplexity API ile fiyat araştırması (24h cache).

```
URL: https://snovwbffwvmkgjulrtsm.supabase.co/functions/v1/ai-assistant-cached

POST Body:
{
    "action": "suggest_price",
    "title": "iPhone 14 Pro Max",
    "category": "Elektronik",
    "condition": "2. El"
}

Response:
{
    "success": true,
    "min_price": 40000,
    "max_price": 50000,
    "avg_price": 45000,
    "source": "perplexity",
    "cached": true
}
```

### ai-assistant (Fallback)

Cache bypass için direkt Perplexity çağrısı.

```
URL: https://snovwbffwvmkgjulrtsm.supabase.co/functions/v1/ai-assistant
```

---

## 🗄️ Storage Buckets

### product-images

```sql
-- Bucket: product-images
-- Public: Yes
-- Max file size: 5MB
-- Allowed types: image/jpeg, image/png, image/webp
```

URL Pattern:
```
https://snovwbffwvmkgjulrtsm.supabase.co/storage/v1/object/public/product-images/{user_id}/{filename}
```
