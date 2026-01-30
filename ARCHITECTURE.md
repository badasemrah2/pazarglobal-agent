# PazarGlobal Agent - Yeni Mimari (v2)

## 🎯 Vizyon

Türkiye'nin ilk **konuşarak ilan veren** pazar yeri platformu. WhatsApp veya WebChat üzerinden doğal dilde ilan oluşturma, arama ve yönetim.

---

## 🏗️ Mimari Genel Bakış

```
┌─────────────────────────────────────────────────────────────────────┐
│                         GATEWAY LAYER                                │
│  /webhook/whatsapp  │  /webchat/message  │  /api/v1/message         │
└──────────────┬───────────────┬───────────────┬──────────────────────┘
               │               │               │
               ▼               ▼               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       MESSAGE NORMALIZER                             │
│  WhatsApp/Web/API → Unified Message Format                          │
│  {user_id, message, media_urls[], channel, timestamp}               │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      INTENT CLASSIFIER                               │
│  4 Intent: CREATE | SEARCH | PUBLISH | CHAT                         │
│  Simple rules + LLM fallback                                         │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
         ┌─────────────────────┼─────────────────────┐
         │                     │                     │
         ▼                     ▼                     ▼
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│  LISTING FLOW   │  │  SEARCH FLOW    │  │   CHAT FLOW     │
│  (Yeni Mimari)  │  │  (Korunuyor ✅)  │  │  (Basit LLM)    │
└────────┬────────┘  └────────┬────────┘  └────────┬────────┘
         │                     │                     │
         ▼                     ▼                     ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      RESPONSE BUILDER                                │
│  Channel-specific formatting (WhatsApp buttons, Web rich text)      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 📦 Dosya Yapısı

```
pazarglobal-agent/
├── main.py                      # FastAPI app, health check
├── requirements.txt
├── Procfile                     # Railway deployment
├── railway.json
│
├── routers/
│   ├── __init__.py
│   └── gateway.py               # Unified /message endpoint
│
├── core/
│   ├── __init__.py
│   ├── state_machine.py         # 4-state FSM (IDLE→DRAFTING→PREVIEW→PUBLISHED)
│   ├── intent_classifier.py     # Rule-based + LLM fallback
│   ├── slot_filler.py           # Deterministic field extraction
│   └── response_builder.py      # Channel-aware response formatting
│
├── handlers/
│   ├── __init__.py
│   ├── listing_handler.py       # İlan oluşturma orchestration
│   ├── search_handler.py        # SearchComposerAgent wrapper
│   ├── publish_handler.py       # Yayınlama akışı
│   └── chat_handler.py          # Small talk handler
│
├── agents/                      # LLM Agents (controlled roles)
│   ├── __init__.py
│   ├── base_agent.py            # ✅ KORUNUYOR
│   ├── search_agents.py         # ✅ KORUNUYOR (SearchComposerAgent)
│   ├── enricher_agent.py        # Title/Description improvement (NEW)
│   └── intent_router.py         # Simplified classifier (NEW)
│
├── services/
│   ├── __init__.py
│   ├── supabase_client.py       # ✅ KORUNUYOR (DB operations)
│   ├── redis_client.py          # ✅ KORUNUYOR (Cache only)
│   ├── openai_client.py         # ✅ KORUNUYOR (LLM calls)
│   ├── vision_service.py        # Image analysis (extracted)
│   └── price_service.py         # Perplexity/Edge function calls
│
├── guards/
│   ├── __init__.py
│   └── vision_safety.py         # ✅ KORUNUYOR (VisionSafetyGate)
│
├── tools/                       # Agent tools
│   ├── __init__.py
│   ├── base_tool.py             # ✅ KORUNUYOR
│   ├── draft_tools.py           # ✅ KORUNUYOR
│   ├── listing_tools.py         # ✅ KORUNUYOR (search, market_price)
│   └── image_tools.py           # ✅ KORUNUYOR
│
├── config/
│   ├── __init__.py
│   ├── settings.py              # ✅ KORUNUYOR
│   └── prompts.py               # ✅ KORUNUYOR (sadece search kısmı)
│
└── tests/
    ├── test_state_machine.py
    ├── test_slot_filler.py
    ├── test_listing_flow.py
    └── test_search.py
```

---

## 🔄 State Machine (4 State)

```python
class ListingState(Enum):
    IDLE = "idle"           # Kullanıcı boşta, intent bekleniyor
    DRAFTING = "drafting"   # İlan oluşturuluyor, slotlar dolduruluyor
    PREVIEW = "preview"     # Önizleme, düzenleme yapılabilir
    PUBLISHED = "published" # Yayınlandı, flow sona erdi
```

### State Transitions

```
IDLE ──[create intent]──► DRAFTING
     ──[search intent]──► (Search flow, state değişmez)
     ──[chat intent]───► (Chat flow, state değişmez)

DRAFTING ──[all slots filled]──► PREVIEW
         ──[cancel command]────► IDLE

PREVIEW ──[publish command]──► PUBLISHED
        ──[edit command]─────► PREVIEW (loop)
        ──[cancel command]───► IDLE

PUBLISHED ──[new listing]──► IDLE
```

---

## 📋 İlan Verme Akışı (Listing Flow)

### Slot'lar (Zorunlu ve Opsiyonel)

| Slot | Zorunlu | Kaynak | Açıklama |
|------|---------|--------|----------|
| `title` | ✅ | User + Vision | Ürün başlığı |
| `description` | ✅ | User + LLM | Açıklama metni |
| `price` | ✅ | User + Perplexity | Fiyat (TL) |
| `category` | ✅ | Vision + Rules | Kategori |
| `condition` | ✅ | User | Sıfır / 2. El / Az Kullanılmış |
| `location` | ✅ | User | Şehir/İlçe |
| `images` | ⚠️ | User | Min 1 önerilir, opsiyonel |

### Akış Diyagramı

```
[User Message + Media]
        │
        ▼
┌───────────────────┐
│ VISION SAFETY     │  ← Illegal content check
│ GUARD             │  ← Block if unsafe
└────────┬──────────┘
         │ safe
         ▼
┌───────────────────┐
│ IMAGE ANALYZER    │  ← Product recognition
│ (Vision API)      │  ← Category inference
└────────┬──────────┘
         │
         ▼
┌───────────────────┐
│ SLOT FILLER       │  ← Deterministic extraction
│ (Rules-based)     │  ← price: regex, location: NER
└────────┬──────────┘
         │
         ▼
┌───────────────────┐
│ DRAFT UPDATE      │  ← Supabase active_drafts
│ (Single source)   │
└────────┬──────────┘
         │
    [All slots?]
    ┌────┴────┐
    No       Yes
    │         │
    ▼         ▼
[Ask next]  ┌───────────────────┐
   slot     │ ENRICHER AGENT    │  ← Title/Desc improvement
            │ (LLM, forced call)│
            └────────┬──────────┘
                     │
                     ▼
            ┌───────────────────┐
            │ PREVIEW BUILDER   │  ← Format preview message
            └────────┬──────────┘
                     │
                     ▼
            [User: "yayınla" or edit]
            ┌────────┴────────┐
            Edit            Publish
             │                │
             ▼                ▼
        [Apply edit]    ┌───────────────────┐
        [Show preview]  │ PUBLISH HANDLER   │
                        │ • Wallet check    │
                        │ • Insert listing  │
                        │ • Delete draft    │
                        └───────────────────┘
```

---

## 🛡️ Güvenlik Katmanları

### 1. Vision Safety Guard (Pre-routing)

```python
# guards/vision_safety.py
class VisionSafetyGate:
    """OpenAI Moderation API ile illegal içerik tespiti"""
    
    BLOCKED_CATEGORIES = [
        "sexual",      # Cinsel içerik
        "violence",    # Şiddet
        "hate",        # Nefret söylemi
        "harassment",  # Taciz
        "self-harm",   # Kendine zarar
        "illicit"      # Yasadışı (uyuşturucu, silah)
    ]
    
    async def check_media(self, urls: List[str]) -> SafetyResult:
        # Fail-open: API hatası durumunda içeriğe izin ver
        pass
```

### 2. Image Analysis (Product Recognition)

```python
# services/vision_service.py
class VisionService:
    """GPT-4 Vision ile ürün tanıma"""
    
    async def analyze_image(self, url: str) -> VisionResult:
        # Returns: product, category, condition_visual, features
        pass
```

---

## 💰 Piyasa Fiyat Araştırması

### 3 Katmanlı Fiyat Sistemi

```
1. CACHE CHECK (Redis + Supabase market_price_snapshots)
   │
   └─ HIT → Return cached price
   │
   └─ MISS ↓

2. EDGE FUNCTION (ai-assistant-cached)
   │
   └─ Perplexity API call
   └─ Cache result (24h TTL)
   └─ Return suggestion

3. FALLBACK (Local heuristics)
   │
   └─ Category-based average from listings
   └─ Return estimate with disclaimer
```

### Edge Function URLs

```
Primary:   https://snovwbffwvmkgjulrtsm.supabase.co/functions/v1/ai-assistant-cached
Fallback:  https://snovwbffwvmkgjulrtsm.supabase.co/functions/v1/ai-assistant
```

### Usage in Flow

```python
# User: "iPhone 14 Pro Max kaç para eder?"

# 1. Check if user is creating a listing
if state == ListingState.DRAFTING and slot == "price":
    suggestion = await price_service.suggest_price(
        title="iPhone 14 Pro Max",
        category="Elektronik",
        condition="2. El"
    )
    return f"Piyasa araştırmama göre {suggestion.min_price}-{suggestion.max_price} TL arası öneriyorum."
```

---

## 🔍 Search Flow (KORUNUYOR)

```python
# handlers/search_handler.py
async def handle_search(message: str, user_id: str) -> dict:
    """Search flow - mevcut SearchComposerAgent kullanılıyor"""
    composer = SearchComposerAgent()
    result = await composer.orchestrate_search(message, {"user_id": user_id})
    return result
```

Özellikler:
- ✅ Paralel search (category, price, content)
- ✅ Synonym expansion
- ✅ Market price integration
- ✅ Redis cache

---

## 📊 Supabase Tabloları

### active_drafts (İlan Taslakları)

```sql
CREATE TABLE active_drafts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES profiles(id),
    state TEXT DEFAULT 'in_progress', -- idle, drafting, preview
    listing_data JSONB DEFAULT '{}',
    images JSONB DEFAULT '[]',
    vision_product JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT active_drafts_user_id_key UNIQUE (user_id) -- 1 draft/user
);
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
    status TEXT DEFAULT 'active',
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
    source TEXT, -- 'perplexity', 'listings_avg'
    created_at TIMESTAMPTZ DEFAULT now(),
    expires_at TIMESTAMPTZ -- 24h TTL
);
```

---

## 🌐 API Endpoints

### Gateway (Unified)

```
POST /api/v1/message
{
    "user_id": "uuid",
    "message": "string",
    "media_urls": ["url1", "url2"],
    "channel": "whatsapp" | "webchat",
    "session_id": "string"
}

Response:
{
    "success": true,
    "message": "Yanıt metni",
    "data": {
        "type": "draft_update" | "preview" | "search_results" | "chat",
        "draft_id": "uuid",
        "state": "drafting" | "preview" | "published",
        ...
    }
}
```

### WhatsApp Webhook

```
POST /webhook/whatsapp
(Twilio → Edge Function → /api/v1/message)
```

### WebChat

```
POST /webchat/message
(Frontend → /api/v1/message)
```

---

## 🚀 Deployment

### Railway

```yaml
# railway.json
{
    "build": {"builder": "NIXPACKS"},
    "deploy": {"startCommand": "uvicorn main:app --host 0.0.0.0 --port $PORT"}
}
```

### Environment Variables

```env
# Required
OPENAI_API_KEY=sk-...
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_KEY=eyJ...
SUPABASE_SERVICE_KEY=eyJ...

# Optional
REDIS_URL=redis://...
PERPLEXITY_API_KEY=pplx-...
```

---

## 📈 Ölçeklenebilirlik (1000+ Kullanıcı)

### Stateless Design

- ❌ Session memory kullanılmaz
- ✅ Supabase = Single source of truth
- ✅ Redis = Cache only (düşerse sorun yok)

### Database Indexing

```sql
CREATE INDEX idx_active_drafts_user_id ON active_drafts(user_id);
CREATE INDEX idx_listings_user_id ON listings(user_id);
CREATE INDEX idx_listings_category ON listings(category);
CREATE INDEX idx_market_price_product_key ON market_price_snapshots(product_key);
```

### Rate Limiting

```python
# Per-user limits
RATE_LIMITS = {
    "messages_per_minute": 30,
    "listings_per_day": 10,
    "searches_per_minute": 60
}
```

---

## ✅ Migration Checklist

### Korunacaklar
- [x] `agents/base_agent.py`
- [x] `agents/search_agents.py` (SearchComposerAgent)
- [x] `agents/vision_safety_gate.py` → `guards/vision_safety.py`
- [x] `services/supabase_client.py`
- [x] `services/redis_client.py`
- [x] `services/openai_client.py`
- [x] `tools/listing_tools.py` (SearchListingsTool, MarketPriceTool)
- [x] `tools/image_tools.py` (ProcessImageTool)
- [x] `config/settings.py`

### Yeni Yazılacaklar
- [ ] `core/state_machine.py`
- [ ] `core/intent_classifier.py`
- [ ] `core/slot_filler.py`
- [ ] `handlers/listing_handler.py`
- [ ] `handlers/search_handler.py`
- [ ] `handlers/publish_handler.py`
- [ ] `routers/gateway.py`
- [ ] `services/vision_service.py`
- [ ] `services/price_service.py`

### Silinecekler
- [x] `api/webchat.py` (7600+ satır)
- [x] `api/whatsapp.py`
- [x] `agents/composer.py`
- [x] Eski MD dosyaları

---

## 🎯 Sonraki Adımlar

1. **Faz 1: Core Foundation** (3-4 gün)
   - State machine
   - Slot filler
   - Intent classifier

2. **Faz 2: Listing Handler** (3-4 gün)
   - Draft management
   - Vision integration
   - Price suggestion

3. **Faz 3: Gateway & Integration** (2-3 gün)
   - Unified endpoint
   - WhatsApp bridge update
   - E2E tests

---

*Son güncelleme: 31 Ocak 2026*
