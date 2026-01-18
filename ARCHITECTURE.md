# 🏗️ Sistem Mimarisi (Güncel)

Bu doküman, PazarGlobal Agent sisteminin **güncel** teknik mimarisini açıklar.

Bu workspace’te mimari 3 ayrı repo olarak kurgulanır:

- **pazarglobal-frontend**: WebChat paneli (lokalde çalışıyor).
- **pazarglobal-agent**: FastAPI Agent API (Railway deploy). İstekler burada işlenir (özellikle `/agent/run`).
- **pazarglobal-whatsapp-bridge**: Twilio WhatsApp webhook → **Supabase Edge WhatsApp Traffic Controller** → Agent API köprüsü (Railway deploy).

## 📐 Genel Mimari

```text
┌───────────────────────────────────────────────────────────────────────────┐
│                              User Interfaces                               │
│                                                                           │
│  ┌──────────────┐                                     ┌──────────────┐   │
│  │   WhatsApp    │                                     │   WebChat    │   │
│  │   (Twilio)    │                                     │  (Frontend)  │   │
│  └──────┬────────┘                                     └──────┬───────┘   │
│         │                                                      │           │
└─────────┼──────────────────────────────────────────────────────┼───────────┘
          │                                                      │
          ▼                                                      ▼
┌───────────────────────────────┐                    ┌──────────────────────┐
│ WhatsApp Bridge (Railway)      │                    │ Agent API (FastAPI)  │
│ /webhook/whatsapp              │                    │ WebChat: /webchat/*  │
└──────────────┬────────────────┘                    │ Core:   /agent/run    │
               │                                     └──────────┬───────────┘
               ▼                                                │
┌───────────────────────────────┐                               │
│ Supabase Edge Function         │                               │
│ whatsapp-traffic-controller    │                               │
│ (PIN + 10min session gate)     │                               │
└──────────────┬────────────────┘                               │
               │                                                │
               └───────────────────────────────►────────────────┘
                               forward
```

## 🔄 Create Listing Workflow (Güncel / Hibrit)

Create Listing akışı pratikte **hibrit** çalışır:

- **Deterministik katman (webchat.py)**: slot-filling, preview/edit, “image-first” buffer, kategori çıkarımı gibi kararlar kod ile verilir.
- **LLM katmanı (ComposerAgent + alt agentlar)**: eksik alanları tool’lar üzerinden doldurma/iyileştirme yapar.

Bu sayede Railway’de sticky session olmadığı senaryolarda bile akış “kendi kendine toparlayabilir”.

> **Manifesto:** ComposerAgent karar vermez, tutarlılığı denetler.
### 🔐 Vision Safety Gate (Step 0)

**Sprint 1 Implementation (January 2026)**

Güvensiz içerik sisteme GİRMEDEN bloklanır:

```text
User uploads media → Vision Safety Gate (Pre-Routing)
                            ↓
                     ┌──────────────┐
                     │ OpenAI       │
                     │ Moderation   │
                     │ API          │
                     └──────┬───────┘
                            │
                    ┌───────┴────────┐
                    │                │
              🔴 UNSAFE          ✅ SAFE
                    │                │
                    ▼                ▼
            ┌──────────────┐   ┌──────────────┐
            │ Block +      │   │ Proceed to   │
            │ Empathetic   │   │ FSM/Router   │
            │ Message      │   │              │
            └──────────────┘   └──────────────┘
```

**Blocked Categories:**
- Sexual content (explicit)
- Violence (graphic)
- Hate symbols
- Harassment/Threats
- Self-harm content
- Illicit content (drugs, weapons)

**Key Design Decisions:**
- **Fail-open behavior**: If moderation API fails, allow content (avoids blocking legitimate users)
- **Storage-first**: Media URLs checked AFTER upload, verdict blocks downstream processing
- **FSM/Router never sees unsafe content**: Safety check runs in `/media/analyze` endpoint BEFORE vision analysis

**Implementation:**
- `agents/vision_safety_gate.py`: VisionSafetyGate class
- `api/webchat.py`: Pre-routing check in `/media/analyze` endpoint
- Test coverage: `test_vision_safety_gate.py` (8 tests)
```text
User: "iPhone 13 satmak istiyorum, fiyat 20000 TL"
                    ↓
        ┌──────────────────────────────┐
        │ WebChat Workflow Engine      │
        │ (deterministic slot filling) │
        └──────────┬───────────────────┘
                │
                │ 1. Draft recover / create (active_drafts)
                │
                ├──────────────────────────────────┐
                │                                  │
                │  2. Optional LLM extraction      │
                │  (ComposerAgent only for missing  │
                │   fields or explicit edits)       │
                │                                  │
    ┌───────────┼──────────┬──────────┬───────────┼────────┐
    │           │          │          │           │        │
    ▼           ▼          ▼          ▼           ▼        ▼
┌─────────┐ ┌──────────┐ ┌────────┐ ┌─────────┐ ┌────────────┐
│ Title   │ │Description│ │ Price  │ │  Image  │ │   GUARD    │
│ Agent   │ │  Agent    │ │ Agent  │ │  Agent  │ │  CHECKER   │
└────┬────┘ └─────┬─────┘ └───┬────┘ └────┬────┘ └──────┬─────┘
     │            │            │           │             │
     │ Tool:      │ Tool:      │ Tool:     │ Tool:       │
     │ update_    │ update_    │ update_   │ process_    │
     │ title()    │ description│ price()   │ image()     │
     │            │ ()         │           │             │
     └────────────┴────────────┴───────────┴─────────────┘
                              │
                              │ 3. Draft integrity guard
                              │ (tek draft_id kuralı)
                              ▼
                    ┌──────────────────┐
                    │  Composer Agent  │
                    │  Validates IDs   │
                    └────────┬─────────┘
                             │
                    ┌────────┴─────────┐
                    │                  │
            ✅ Same ID        ❌ Conflict
                    │                  │
                    ▼                  ▼
            ┌──────────────┐   ┌──────────────┐
            │ Draft Update │   │ ABORT Flow   │
            │   Success    │   │ User Restart │
            └──────────────┘   └──────────────┘
```

## 🗄️ Data Flow (Güncel)

```text
┌──────────────────────────────────────────────────────────────┐
│                     State Management                          │
│                                                               │
│  ┌────────────┐         ┌──────────────┐                    │
│  │   Redis    │         │  Supabase    │                    │
│  │  (State)   │         │ (Persistent) │                    │
│  └────────────┘         └──────────────┘                    │
│                                                               │
│  • Session State        • active_drafts                       │
│  • locked_intent        • listings                            │
│  • active_draft_id      • product_images / listing images     │
│  • message history      • wallets / wallet_transactions       │
│  • rate limit           • audit_logs                          │
└──────────────────────────────────────────────────────────────┘

Not: Supabase şeması repo içinde [pazarglobal-agent/supabase_table_schema.md](pazarglobal-agent/supabase_table_schema.md) dosyasında özetlenmiştir.
```

## 🛠️ Tool System Architecture

```text
┌──────────────────────────────────────────────────────────┐
│                    Base Tool Class                        │
│                                                           │
│  • to_openai_tool() - OpenAI function format            │
│  • execute() - Async execution                          │
│  • format_success/error() - Response formatting         │
└───────────────────────┬──────────────────────────────────┘
                        │
        ┌───────────────┼───────────────┬──────────────┐
        │               │               │              │
        ▼               ▼               ▼              ▼
┌──────────────┐ ┌──────────┐ ┌──────────────┐ ┌───────────┐
│ Draft Tools  │ │ Listing  │ │ Wallet Tools │ │   Image   │
│              │ │  Tools   │ │              │ │   Tools   │
├──────────────┤ ├──────────┤ ├──────────────┤ ├───────────┤
│• create      │ │• publish │ │• get_balance │ │• process  │
│• read        │ │• delete  │ │• deduct      │ │• analyze  │
│• update_*    │ │• search  │ │              │ │• moderate │
└──────────────┘ └──────────┘ └──────────────┘ └───────────┘
```

### 💰 Wallet Read APIs (Sprint 2)

- `GET /webchat/wallet/balance` → Kullanıcı bakiyesi (credits)
- `GET /webchat/wallet/history` → Son işlemler (limit 50, varsayılan 20)

**Tasarım Notları:**
- `session_id` → `user_id` fallback (kimlik boşsa 400)
- Supabase `wallets` + `wallet_transactions` tabloları kullanılır
- Tablo eksikse fail-soft (boş liste döner, servis kesilmez)

## 🤖 Agent Architecture

```text
┌────────────────────────────────────────────────────────────┐
│                   Base Agent Class                          │
│                                                             │
│  • name: str                                               │
│  • system_prompt: str                                      │
│  • tools: List[BaseTool]                                   │
│  • run() - Main execution loop                            │
│  • run_simple() - No tools execution                      │
└──────────────────┬─────────────────────────────────────────┘
                   │
                   │ Inherits
                   │
    ┌──────────────┼──────────────┬──────────────┐
    │              │              │              │
┌───▼────────┐ ┌──▼──────────┐ ┌─▼────────────┐ ┌──▼─────────┐
│  Intent    │ │   Title     │ │ Description  │ │   Price    │
│  Router    │ │   Agent     │ │    Agent     │ │   Agent    │
└────────────┘ └─────────────┘ └──────────────┘ └────────────┘

┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────┐
│    Image     │ │  Composer    │ │   Publish    │ │  Search  │
│    Agent     │ │    Agent     │ │   Delete     │ │ Composer │
└──────────────┘ └──────────────┘ └──────────────┘ └──────────┘

┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────┐
│  Category    │ │    Price     │ │   Content    │ │   Small  │
│   Search     │ │   Search     │ │   Search     │ │   Talk   │
└──────────────┘ └──────────────┘ └──────────────┘ └──────────┘
```

## 🔐 Security & State Management

```text
┌────────────────────────────────────────────────────────────┐
│                    Security Layers                          │
└────────────────────────────────────────────────────────────┘
         │                          │                    │
         ▼                          ▼                    ▼
┌────────────────┐      ┌────────────────┐    ┌─────────────┐
│  Rate Limiting │      │  Input Valid.  │    │ API Keys    │
│                │      │                │    │ Protection  │
│ 60/min         │      │ Sanitization   │    │             │
│ 1000/hour      │      │ Type checking  │    │ Env Vars    │
└────────────────┘      └────────────────┘    └─────────────┘

┌────────────────────────────────────────────────────────────┐
│                  Session Management                         │
└────────────────────────────────────────────────────────────┘
         │
         ▼
┌────────────────────────────────────────────────────────────┐
│  Redis (opsiyonel)                                          │
│                                                             │
│  session:{id} →                                            │
│    {                                                        │
│      "user_id": "...",                                     │
│      "intent": "create_listing",                           │
│      "locked_intent": "create_listing",                    │
│      "active_draft_id": "...",                             │
│      "phone_number": "...",                                │
│      "fsm_state": "active",                                │
│      "fsm_state_reason": null,                             │
│      "fsm_state_updated_at": "2026-01-06T...",             │
│      "parked_intent": null,                                │
│      "last_user_at": "2026-01-06T...",                     │
│      "last_bot_at": "2026-01-06T..."                       │
│    }                                                        │
Redis yoksa veya load-balancer nedeniyle istek farklı instance’a düşerse:

- Redis **varsa** → latency düşer, UX daha akıcı olur (sticky intent + history + kısa süreli state daha hızlı).
- Redis **yoksa** → sistem bozulmaz; sadece **DB recover** (Supabase `active_drafts`) daha sık devreye girer.

- `active_draft_id` DB’den (Supabase `active_drafts`) deterministik olarak **recover** edilir.
- “image-first” akışında `pending_media_urls` gibi geçici bilgiler in-memory olabilir; kritik state DB’ye yazılır.
│                                                             │
│  messages:{id} → List of messages (last 100)              │
└────────────────────────────────────────────────────────────┘

## 🧯 Failure Modes (Mini)

| Durum | Sistem Ne Yapar |
|---|---|
| LLM timeout / LLM hatası | Deterministik slot soruları ile devam eder; kritik akışlar (publish/delete) deterministiktir |
| Tool error (DB/RPC/HTTP) | İşlem durdurulur, kullanıcıya hata döner; audit log ile izlenebilirlik korunur |
| Draft conflict (birden fazla `draft_id` / tutarsız ID) | Akış ABORT edilir; kullanıcıdan yeniden başlatması istenir; audit log’a conflict yazılır |
| Redis yok / sticky session yok | Draft state Supabase `active_drafts` üzerinden recover edilir; geçici media buffer DB’ye yazılabilir |
| Edge Function (WhatsApp gate) down | WhatsApp istekleri reject edilir veya güvenli fail-close yapılır; backend'e kontrolsüz forward edilmez |
| **FSM Loop Trap (user hesitation)** | Kullanıcı "dur bi", "belki", "satmayabilirim" gibi kararsızlık sinyali gösterirse: `locked_intent` temizlenir, flow nazikçe kapatılır, aynı soru tekrar tekrar sorulmaz (same question suppression) |
| **ComposerAgent timeout** | 45s içinde LLM dönmezse → `fsm_state=timeout`, flow parked olur, kullanıcı "devam" veya "iptal" ile yönetir |
| **User inactivity (parked)** | 10 dakika sessizlik → `fsm_state=parked`, flow askıya alınır, kullanıcı "devam" ile kaldığı yerden başlatabilir |

### 🔁 FSM Loop Trap Prevention (Yeni)

**Problem:** Kullanıcı ilan akışına başlıyor ama kararsız kalıyor:
```
Kullanıcı: "ilan vermek istiyorum ama dur bi"
Sistem: "Ürün adı nedir?"
Kullanıcı: "aslında bakayım"
Sistem: "Fiyat nedir?" [❌ YANLIŞ - aynı soru tekrarı]
Kullanıcı: "satmayabilirim"
Sistem: "Fiyat nedir?" [❌ LOOP]
```

**Çözüm:**

1. **IntentRouter pattern detection:**
   - Kararsızlık: "dur bi", "bekle", "aslında bakayım", "belki", "emin değilim", "düşüneyim"
   - İptal: "satmayabilirim", "vermeyebilirim", "vazgeç"
   - Bu pattern'ler tespit edilirse → `small_talk` intent'ine yönlendir

2. **ComposerAgent same question suppression:**
   - Son 2 response aynı soruyu içeriyorsa, farklı yaklaşım kullan
   - Örnek: "Görüyorum ki kararsızsın. Karar verdiğinde söylersin."

3. **Webchat hesitation override:**
   - `is_hesitation_signal()` fonksiyonu `create_listing` flow'dayken tetiklenirse:
     - `locked_intent` ve `intent` temizlenir
     - "Tamam, acele yok. Karar verdiğinde söylersin, birlikte ilan oluştururuz. 😊" şeklinde empatik yanıt verilir
     - Flow nazikçe kapatılır, kullanıcı yeniden başlayabilir

**Test coverage:** `test_hesitation_signals_exit_create_listing_flow` (28/28 passing)

### 🕒 FSM Parked/Timeout States (Sprint 3, January 2026)

**Problem:** Kullanıcı taslak başlatıyor, uzun süre yanıt vermiyor, sonra tekrar mesaj yazdığında flow otomatik devam ediyor; ama kullanıcı context kaybetmiş olabilir ve bot yanlış noktadan devam ediyorsa UX kötü oluyor.

**Çözüm: Parked & Timeout States + Telemetry**

Session state genişletildi:
- `fsm_state` (active | parked | timeout | hesitation_exit)
- `fsm_state_reason` (inactivity | composer_timeout | user_hesitation)
- `fsm_state_updated_at`, `fsm_state_intent`
- `parked_intent` (kullanıcı "devam" derse hangi intent'e döneceğiz)
- `last_user_at`, `last_bot_at` (timestamp ISO8601)

**Parked** (inactivity):
- `last_user_at` 10 dakika geçtikten sonra session locked ise → `parked` state
- `locked_intent` temizlenir, `parked_intent` doldurulur
- Bot: "Bir süredir yanıt alamadım. Akışı park ettim. Devam etmek için 'devam' yaz."
- Kullanıcı "devam" derse → intent restore + `fsm_state=active`
- Kullanıcı "iptal" derse → flow tamamen temizlenir

**Timeout** (ComposerAgent timeout):
- `ComposerAgent.orchestrate_listing_creation()` 45 saniye içinde dönmezse → asyncio.TimeoutError
- `fsm_state=timeout`, `parked_intent` set, bot: "Şu an yanıt veremedim, beklemeye aldım."
- Kullanıcı "devam" ya da "iptal" ile akışı yönetir

**Hesitation Exit** (user signal):
- `is_hesitation_signal()` tetiklenirse → `fsm_state=hesitation_exit`
- Flow nazikçe kapatılır

**Telemetry:**
- Her FSM state değişimi `audit_logs` tablosuna yazılır (action=`fsm_event`)
- Metadata içinde: event (`parked`, `timeout`, `resumed`, `hesitation_exit`, `intent_lock`), session_id, intent, locked_intent, fsm_state, fsm_state_reason
- Fail-soft: telemetry başarısız olsa bile flow kesilmez

**Test coverage:** `test_fsm_sprint3.py` (5/5 passing)
``````

## 🌐 Communication Protocols

### REST API (WebChat)

```text
Client                    Server
  │                         │
  │ POST /session/new       │
  ├────────────────────────►│
  │                         │
  │ ◄────────────────────────┤
  │   {session_id: "xxx"}   │
  │                         │
  │ POST /message           │
  ├────────────────────────►│
  │ {session_id, message}   │
  │                         │
  │         [Process]       │
  │         Agent →         │
  │         OpenAI →        │
  │         Tools →         │
  │                         │
  │ ◄────────────────────────┤
  │   {response, data}      │
  │                         │
```

### WebSocket (Real-time)

```text
Client                    Server
  │                         │
  │ WS Connect              │
  ├────────────────────────►│
  │                         │
  │ ◄────────────────────────┤
  │   {type: "connection"}  │
  │                         │
  │ Send: {message: "..."}  │
  ├────────────────────────►│
  │                         │
  │         [Process]       │
  │                         │
  │ ◄────────────────────────┤
  │   {type: "message"}     │
  │                         │
  │ Send: {message: "..."}  │
  ├────────────────────────►│
  │                         │
  ⋮                         ⋮
  │                         │
  │ Disconnect              │
  ├────────────────────────►│
  │                         │
```

### WhatsApp (Webhook)

```text
Twilio                 WhatsApp Bridge              Supabase Edge Fn               Agent API
        │                        │                              │                         │
        │ POST /twilio webhook    │                              │                         │
        ├───────────────────────►│                              │                         │
        │ Form: {From, Body, Media}                             │                         │
        │                        │ POST /functions/.../whatsapp-traffic-controller         │
        │                        ├─────────────────────────────►│                         │
        │                        │ {source:"whatsapp", phone, message, history, media}   │
        │                        │                              │  if no session → require_pin
        │                        │                              │  if PIN → verify_pin RPC + open 10min session
        │                        │                              │  if session → forward /agent/run
        │                        │                              ├────────────────────────►│
        │                        │                              │  {user_id, phone, message, history, media, session_token}
        │                        │                              │                         │
        │                        │                              │◄────────────────────────┤
        │                        │◄─────────────────────────────┤
        │◄───────────────────────┤
        │  Twilio API: send msg   │
        ├────────────────────────►
        │  Delivers to WhatsApp
        │
```

Not: Güncel WhatsApp entegrasyonunda backend’in ana entrypoint’i `/agent/run`’dır (Edge function buraya forward eder). FastAPI içindeki `/whatsapp/*` route’ları doğrudan-Twilio entegrasyonu için opsiyonel/legacy kalabilir.

### WhatsApp Security Gate (Supabase Edge Traffic Controller)

WhatsApp kanalında güvenlik için mesajlar Agent API’ye gitmeden önce **Supabase Edge Function** üzerinden geçer:

- **Edge function:** `pazarglobal-frontend/supabase/functions/whatsapp-traffic-controller/index.ts`
- **Amaç:** WhatsApp hesabı ele geçirilse bile, saldırganın agent aksiyonlarını tetiklemesini zorlaştırmak (PIN + kısa süreli oturum).

Davranış (özet):

- Kullanıcı için **aktif session** varsa (varsayılan $10$ dakika) istek **backend’e forward** edilir.
- Session yoksa:
        - Mesaj $4$-$6$ haneli sayıysa PIN kabul edilir → `verify_pin` RPC çağrılır.
        - PIN doğruysa `user_sessions` içine yeni bir session açılır (10dk) ve kullanıcıya “giriş başarılı” mesajı döner.
        - PIN değilse kullanıcıdan PIN istenir (`require_pin: true`).
- Kullanıcı “iptal/vazgeç/çık/stop…” derse session kapatılır.
- Edge ayrıca backend response’unda “operation completed” sezilirse session’ı kapatmayı dener (şu an `backendData.intent` içinde `complet` araması).

Bu gate’in kullandığı Supabase objeleri:

- `user_sessions` tablosu: zamanlı oturum takibi (`is_active`, `expires_at`, `last_activity`, `end_reason`)
- `verify_pin` RPC: telefon + PIN doğrulama (PIN hash/lockout mantığı DB tarafında)

İlgili env/config (yüksek seviye):

- WhatsApp Bridge → Edge:
        - `EDGE_FUNCTION_URL` (bridge’in çağırdığı Edge endpoint)
        - `SUPABASE_SERVICE_KEY` (server-to-server auth için)
- Edge → Agent API:
        - `BACKEND_URL` (Edge’in forward edeceği base backend URL)

## 📊 OpenAI Integration Pattern

```text
Agent receives user message
        │
        ▼
┌────────────────────────────┐
│  Prepare Messages          │
│  [                         │
│    {role: "system", ...},  │
│    {role: "user", ...}     │
│  ]                         │
└──────────┬─────────────────┘
           │
           ▼
┌────────────────────────────┐
│  OpenAI Chat Completion    │
│  + Tools (if available)    │
└──────────┬─────────────────┘
           │
           ├─────────────────┐
           │                 │
    ✅ Text Response   🔧 Tool Calls
           │                 │
           │                 ▼
           │        ┌─────────────────┐
           │        │  Execute Tools  │
           │        │  Parallel       │
           │        └────────┬────────┘
           │                 │
           │                 ▼
           │        ┌─────────────────┐
           │        │ Append Results  │
           │        │ to Messages     │
           │        └────────┬────────┘
           │                 │
           │                 ▼
           │        ┌─────────────────┐
           │        │  Call OpenAI    │
           │        │  Again          │
           │        └────────┬────────┘
           │                 │
           └─────────────────┘
                     │
                     ▼
            Final Response
```

## 🔄 Deployment Architecture

```text
┌──────────────────────────────────────────────────────────┐
│                    Railway Platform                       │
│                                                           │
│  ┌────────────────────────────────────────────────────┐  │
│  │           Main Service (pazarglobal-agent)         │  │
│  │                                                     │  │
│  │  • FastAPI Application                             │  │
│  │  • Uvicorn ASGI Server                            │  │
│  │  • Auto-scaling                                    │  │
│  │  • HTTPS/WSS Support                              │  │
│  └────────────────────────────────────────────────────┘  │
│                                                           │
│  ┌────────────────────────────────────────────────────┐  │
│  │              Redis Service (opsiyonel)              │  │
│  │                                                     │  │
│  │  • Managed Redis Instance                          │  │
│  │  • Automatic Backups                               │  │
│  │  • High Availability                               │  │
│  └────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│          Railway Platform (2. Service - Bridge)           │
│                                                           │
│  ┌────────────────────────────────────────────────────┐  │
│  │     WhatsApp Bridge (pazarglobal-whatsapp-bridge)  │  │
│  │  • Twilio webhook alır                             │  │
│  │  • Mesajı Supabase Edge gate’e iletir              │  │
│  │  • Gate sonrası Agent API (/agent/run) çağrılır    │  │
│  └────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│                  External Services                        │
│                                                           │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌─────────┐ │
│  │  OpenAI  │  │ Supabase │  │  Twilio  │  │ Frontend│ │
│  │   API    │  │ DB+Edge  │  │ WhatsApp │  │  Local  │ │
│  └──────────┘  └──────────┘  └──────────┘  └─────────┘ │
└──────────────────────────────────────────────────────────┘

```

Not: Frontend deploy stratejisi ayrı olabilir; bu doküman workspace’teki güncel durumu (frontend local) baz alır.

## 🔁 Workflow’lar (Güncel davranış)

### 1) Create Listing

Ana hedef: `active_drafts` içinde tek bir `draft_id` üzerinde ilerlemek ve kullanıcıya **sadece bir sonraki eksik alanı** sormak.

- Deterministik parçalar (WebChat):
        - `locked_intent` ile sticky workflow
        - `next_missing_slot()` ile slot prompt
        - tek mesajdan alan çıkarımı (`extract_listing_fields_from_freeform`)
        - kategori auto-infer (`infer_category_from_draft`)
        - image-first buffer + vision özetini draft’a yazma
- LLM parçaları (ComposerAgent):
        - draft create/read
        - Title/Description/Price/Image agentlarını **sadece gerekirse** çalıştırır
        - “tek draft_id” guard

### 2) Publish / Delete

WebChat tarafında publish akışı **deterministiktir** (LLM döngüsü yok):

- Önce draft publishable mı kontrol edilir (başlık/açıklama/fiyat/kategori + foto ya da allow_no_images)
- Önizleme hazırlanır (preview)
- Kullanıcı `evet/onayla` derse tool çağrılır:
        - `get_wallet_balance_tool` (bilgi amaçlı)
        - `publish_listing_tool` (draft → listings, kredi düşümü, log)

Not: `PublishDeleteAgent` repo’da mevcut olsa da WebChat path’inde asıl kontrol/UX `handle_publish_or_delete_flow` içindedir.

### 3) Search

SearchComposerAgent paralel arama yapıp sonuçları birleştirir; “listing atomikliği” guard’ı ile farklı ilanların alanlarını karıştırmaz.

## 🧰 Kritik Tool’lar (Özet)

- Draft:
        - `create_draft`, `read_draft`, `update_title`, `update_description`, `update_price`
- Image:
        - `process_image`
- Publish:
        - `publish_listing`
- Wallet:
        - `get_wallet_balance` (ve gerekiyorsa debit/deduct)
- Search:
        - `search_listings`, `get_market_price_data`

## 🎯 Design Principles

### 1. **Explicit over Implicit**

- Clear agent responsibilities
- Explicit tool definitions
- No framework magic

### 2. **Deterministic Behavior**

- Same input → Same output
- Predictable workflows
- Testable components

### 3. **ID-Centric State**

- All operations centered on `draft_id` (taslak) ve `listing_id` (yayın)
- Prevents data conflicts
- Easy to audit

### 4. **Parallel Execution with Guards**

- Agents run in parallel for speed
- Composer validates consistency
- Abort on conflicts

### 5. **Tool-Driven Architecture**

- Agents call tools (not direct DB)
- Tools encapsulate business logic
- Easy to mock/test

### 6. **Separation of Concerns**

- Agents: AI logic
- Tools: Business operations
- Services: Infrastructure
- API: Communication

## 📈 Scalability Considerations

### Horizontal Scaling

- Stateless API layer
- Redis for shared state
- Load balancer ready

### Vertical Scaling

- Async Python (asyncio)
- Parallel agent execution
- Connection pooling

### Performance Optimizations

- Redis caching
- OpenAI streaming (future)
- Database connection pooling
- Tool result caching

## 🔍 Monitoring & Observability

```text
┌────────────────────────────────────────────────────────┐
│                    Logging Stack                        │
│                                                         │
│  Application → Loguru → Railway Logs → Monitoring      │
│                                                         │
│  Levels:                                               │
│  • INFO: Normal operations                            │
│  • WARNING: Degraded performance                      │
│  • ERROR: Failures                                    │
│  • DEBUG: Development details                         │
└────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────┐
│                   Audit Trail                           │
│                                                         │
│  audit_logs table:                                     │
│  • Agent actions                                       │
│  • Tool executions                                     │
│  • Conflict detections                                │
│  • User operations                                     │
└────────────────────────────────────────────────────────┘
```

## 🏆 Key Architectural Benefits

1. **Maintainability**: Clear separation of concerns
2. **Testability**: Mockable tools and services
3. **Scalability**: Stateless design
4. **Reliability**: Guard mechanisms
5. **Extensibility**: Easy to add new agents/tools
6. **Debuggability**: Comprehensive logging
7. **Performance**: Parallel execution
8. **Safety**: Data integrity checks

---

## ⚠️ Technical Debt Notice (v1 - MVP)

```
Current implementation includes hard-coded flexibility
inside FSM (webchat.py) for MVP speed.

Intent interpretation functions like:
- is_search_command()
- is_cancel_command()
- detects_product_change()
- is_resume_listing_command()

are currently embedded in the FSM logic.

This works but violates separation of concerns.
```

### Current vs Target Architecture

**Current (v1):**
```
User Message → FSM (intent + state + response) → Response
```

**Target (v2):**
```
User Message → ContextInterpreter → ContextDecision → FSM (state only) → Response
```

### ContextDecision Actions (Planned)

| Action | Description |
|--------|-------------|
| `CONTINUE` | Continue current flow |
| `PAUSE` | Pause current flow, do side action (e.g., search) |
| `SWITCH` | Switch to different flow entirely |
| `CANCEL` | Cancel current flow |
| `MODIFY` | Modify current context (e.g., change product) |

### Migration Path

| Phase | Status | Description |
|-------|--------|-------------|
| Phase 1 (MVP) | ✅ Done | Hard-coded flexibility in FSM |
| Phase 2 | 🔜 Planned | Extract ContextInterpreter layer |
| Phase 3 | 📋 Backlog | ML-based intent classification |

### Decision Log

| Date | Decision | Reason |
|------|----------|--------|
| 2026-01-19 | Hard-coded flexibility in FSM | MVP speed, user-facing bug fix |
| 2026-01-19 | Added `paused_context` | Enable flow switching without losing state |
| TBD | Extract ContextInterpreter | When FSM complexity becomes unmanageable |

---

Bu mimari, OpenAI'ın resmi SDK desenlerini takip eder ve production-ready bir sistem sağlar.
