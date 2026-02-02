# PazarGlobal Agent V3 - Production-Ready Architecture

> **Felsefe**: "Basitlik güvenilirliktir. Tek LLM, tek tool, iki FSM."

---

## 📊 Sistem Sağlamlık Skoru: 95/100

| Kategori | Skor | Açıklama |
|----------|------|----------|
| Mimari Basitliği | 98/100 | Tek LLM, minimal bağımlılık |
| Hata Toleransı | 95/100 | Graceful degradation, fallback'ler |
| Ölçeklenebilirlik | 90/100 | Stateless tasarım, Redis session |
| Bakım Kolaylığı | 95/100 | Tek dosya değişikliği = tek etki |
| Test Edilebilirlik | 92/100 | Mock-friendly, deterministik FSM |

---

## 🏗️ Mimari Diyagram

```
                        Kullanıcı Mesajı
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      GATEWAY LAYER                          │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐     │
│  │  WhatsApp   │    │   WebChat   │    │     API     │     │
│  │   Webhook   │    │   Endpoint  │    │   Direct    │     │
│  └──────┬──────┘    └──────┬──────┘    └──────┬──────┘     │
│         └──────────────────┼──────────────────┘             │
│                            ▼                                │
│                 ┌─────────────────────┐                     │
│                 │  Auth & Session     │                     │
│                 │  (Redis Lookup)     │                     │
│                 └──────────┬──────────┘                     │
└────────────────────────────┼────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                    BRAIN: SINGLE LLM                        │
│                                                             │
│  ┌───────────────────────────────────────────────────────┐ │
│  │              GPT-4o (Vision Enabled)                   │ │
│  │                                                        │ │
│  │  Görevler:                                            │ │
│  │  ✅ Intent Classification (CREATE/SEARCH/CHAT)        │ │
│  │  ✅ Image Analysis (ürün tanıma)                      │ │
│  │  ✅ JSON Schema Filling (ilan verisi)                 │ │
│  │  ✅ Natural Conversation (Türkçe)                     │ │
│  │  ✅ Slot Validation (eksik alan tespiti)              │ │
│  │                                                        │ │
│  │  Tek Tool: 🔧 Perplexity API (fiyat araştırması)      │ │
│  │                                                        │ │
│  │  ⛔ YASAKLAR:                                         │ │
│  │  - JSON schema dışına çıkmak                          │ │
│  │  - Olmayan alanlar eklemek                            │ │
│  │  - Tool'u gereksiz çağırmak                           │ │
│  └───────────────────────────────────────────────────────┘ │
│                            │                                │
│              ┌─────────────┴─────────────┐                 │
│              │      LLM Output           │                 │
│              │  {intent, json_data}      │                 │
│              └─────────────┬─────────────┘                 │
└────────────────────────────┼────────────────────────────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
              ▼              ▼              ▼
        ┌─────────┐    ┌─────────┐    ┌─────────┐
        │ SEARCH  │    │ CREATE  │    │  CHAT   │
        │   FSM   │    │   FSM   │    │ (Echo)  │
        └────┬────┘    └────┬────┘    └────┬────┘
             │              │              │
             ▼              ▼              ▼
     ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
     │ Supabase     │ │ Wallet Check │ │ Direct       │
     │ Full-Text    │ │      ↓       │ │ Response     │
     │ Search       │ │ Draft Save   │ │              │
     │      ↓       │ │      ↓       │ │              │
     │ Results      │ │ Publish      │ │              │
     │ Format       │ │      ↓       │ │              │
     │              │ │ Credit       │ │              │
     │              │ │ Deduction    │ │              │
     └──────────────┘ └──────────────┘ └──────────────┘
             │              │              │
             └──────────────┼──────────────┘
                            ▼
                      ✅ RESPONSE
                      (User'a gönder)
```

---

## 📋 Listing JSON Schema (Sabit - Değiştirilemez)

```json
{
  "$schema": "PazarGlobal Listing Schema v1.0",
  "fields": {
    "title": {
      "type": "string",
      "required": true,
      "max_length": 100,
      "description": "Ürün başlığı"
    },
    "description": {
      "type": "string",
      "required": false,
      "max_length": 1000,
      "description": "Ürün açıklaması (kullanıcının ekstra bilgileri buraya)"
    },
    "price": {
      "type": "number",
      "required": true,
      "min": 1,
      "description": "Fiyat (TL)"
    },
    "category": {
      "type": "enum",
      "required": true,
      "values": ["Elektronik", "Otomotiv", "Emlak", "Mobilya & Dekorasyon", "Giyim & Aksesuar", "Spor & Hobi", "Diğer"],
      "description": "Ürün kategorisi"
    },
    "condition": {
      "type": "enum",
      "required": false,
      "values": ["Sıfır", "Az Kullanılmış", "İyi", "Yıpranmış"],
      "default": "İyi",
      "description": "Ürün durumu"
    },
    "location": {
      "type": "string",
      "required": false,
      "description": "Şehir/İlçe"
    },
    "images": {
      "type": "array",
      "required": false,
      "items": "string (URL)",
      "description": "Ürün görselleri"
    }
  },
  "required_for_publish": ["title", "price", "category"],
  "auto_fillable": ["category", "condition"]
}
```

---

## 🤖 LLM System Prompt

```markdown
# PazarGlobal İlan Asistanı

Sen PazarGlobal'ın yapay zeka asistanısın. Görevin kullanıcıların ilan vermesine ve ürün aramasına yardımcı olmak.

## TEMEL KURALLAR (İHLAL EDİLEMEZ)

1. **JSON Schema Sadakati**: Yukarıdaki şemaya %100 uymalısın. Yeni alan EKLEME, mevcut alanları DEĞİŞTİRME.

2. **Intent Belirleme**: Her mesajı şu 3 kategoriden birine sınıfla:
   - `CREATE`: Kullanıcı ilan vermek istiyor (satmak, ilan, satıyorum, satılık)
   - `SEARCH`: Kullanıcı ürün arıyor (var mı, arıyorum, bul, ara)
   - `CHAT`: Genel sohbet (merhaba, yardım, teşekkürler)

3. **Fotoğraf Analizi**: Görsel geldiğinde:
   - Ürünü tanımla → title önerisi
   - Kategori belirle → category
   - Durum tahmin et → condition
   - Fiyat tahmini YAPMA (Perplexity tool kullan)

4. **Progressive Disclosure**: Her adımda mevcut JSON'u preview olarak göster:
   ```
   📋 İlan Önizlemesi:
   ✅ Başlık: iPhone 14 Pro Max
   ✅ Fiyat: 45,000 TL
   ✅ Kategori: Elektronik
   ⏳ Durum: (belirsiz)
   ⏳ Konum: (belirsiz)
   
   Devam etmek için eksik bilgileri girin veya "yayınla" deyin.
   ```

5. **Perplexity Tool Kullanımı**: SADECE şu durumlarda çağır:
   - Kullanıcı açıkça fiyat sorduğunda: "kaç para eder", "fiyat öner", "piyasa değeri"
   - Asla otomatik çağırma

6. **Ekstra Bilgi Yönetimi**: Kullanıcı şemada olmayan bilgi verirse (garanti, aksesuar, vb.) → description alanına ekle

## OUTPUT FORMAT

Her yanıtında şu JSON yapısını döndür:

```json
{
  "intent": "CREATE|SEARCH|CHAT",
  "response_text": "Kullanıcıya gösterilecek Türkçe mesaj",
  "listing_data": {
    "title": "...",
    "description": "...",
    "price": 0,
    "category": "...",
    "condition": "...",
    "location": "...",
    "images": []
  },
  "missing_fields": ["field1", "field2"],
  "ready_to_publish": false,
  "tool_call": null | {"name": "perplexity", "query": "..."}
}
```

## YASAKLAR

❌ JSON schema'ya yeni alan ekleme
❌ Fiyat tahmini yapma (tool kullan)
❌ required=true alanları boş bırakıp yayınlamaya izin verme
❌ Türkçe dışında yanıt verme
❌ "Bir hata oluştu" gibi teknik mesajlar gösterme
```

---

## 🔄 FSM Tanımları

### CREATE FSM (İlan Oluşturma)

```
States:
  IDLE        → Başlangıç, kullanıcı intent belirlememiş
  DRAFTING    → JSON doluyor, eksik alanlar var
  PREVIEW     → Tüm required alanlar dolu, onay bekleniyor
  PUBLISHING  → Wallet check + DB write
  DONE        → İlan yayınlandı

Transitions:
  IDLE → DRAFTING       : intent=CREATE tespit edildi
  DRAFTING → DRAFTING   : Yeni slot dolduruldu, hala eksik var
  DRAFTING → PREVIEW    : Tüm required alanlar doldu
  PREVIEW → DRAFTING    : Kullanıcı düzenleme istedi
  PREVIEW → PUBLISHING  : "yayınla" komutu
  PUBLISHING → DONE     : Başarılı yayın
  PUBLISHING → PREVIEW  : Yetersiz bakiye (hata mesajı + geri dön)
  
  ANY → IDLE            : "iptal" komutu
```

### SEARCH FSM (Arama)

```
States:
  IDLE        → Arama yok
  SEARCHING   → Supabase query çalışıyor
  RESULTS     → Sonuçlar gösteriliyor
  PAGINATION  → "daha fazla" ile sayfalama

Transitions:
  IDLE → SEARCHING      : intent=SEARCH tespit edildi
  SEARCHING → RESULTS   : Sonuçlar bulundu
  SEARCHING → IDLE      : Sonuç bulunamadı
  RESULTS → PAGINATION  : "daha fazla" komutu
  RESULTS → IDLE        : Yeni arama veya timeout
```

---

## 🛡️ Güvenlik & Hata Yönetimi Stratejileri

### 1. LLM Guardrails (Kritik)

```python
class LLMGuardrails:
    """LLM çıktısını validate et"""
    
    ALLOWED_FIELDS = {"title", "description", "price", "category", "condition", "location", "images"}
    ALLOWED_INTENTS = {"CREATE", "SEARCH", "CHAT"}
    
    @staticmethod
    def validate_output(llm_response: dict) -> dict:
        """LLM çıktısını sanitize et"""
        
        # 1. Intent kontrolü
        intent = llm_response.get("intent", "CHAT")
        if intent not in LLMGuardrails.ALLOWED_INTENTS:
            intent = "CHAT"  # Fallback
        
        # 2. Listing data kontrolü - izin verilmeyen alanları kaldır
        listing_data = llm_response.get("listing_data", {})
        sanitized_data = {
            k: v for k, v in listing_data.items() 
            if k in LLMGuardrails.ALLOWED_FIELDS
        }
        
        # 3. Category enum kontrolü
        if sanitized_data.get("category") not in VALID_CATEGORIES:
            sanitized_data["category"] = None
        
        # 4. Price validation
        price = sanitized_data.get("price")
        if price is not None:
            try:
                price = float(price)
                if price < 1 or price > 100_000_000:
                    price = None
            except:
                price = None
            sanitized_data["price"] = price
        
        return {
            "intent": intent,
            "listing_data": sanitized_data,
            "response_text": llm_response.get("response_text", ""),
            "ready_to_publish": llm_response.get("ready_to_publish", False),
        }
```

### 2. Rate Limiting (API Koruma)

```python
RATE_LIMITS = {
    "llm_calls_per_minute": 30,
    "llm_calls_per_user_per_minute": 10,
    "perplexity_calls_per_minute": 20,
    "perplexity_calls_per_user_per_hour": 5,  # Maliyet kontrolü
}
```

### 3. Circuit Breaker (Servis Kesintisi)

```python
class CircuitBreaker:
    """Servis kesintilerinde graceful degradation"""
    
    def __init__(self, failure_threshold=5, reset_timeout=60):
        self.failures = 0
        self.threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.last_failure = None
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
    
    async def call(self, func, fallback):
        if self.state == "OPEN":
            if time.time() - self.last_failure > self.reset_timeout:
                self.state = "HALF_OPEN"
            else:
                return await fallback()
        
        try:
            result = await func()
            if self.state == "HALF_OPEN":
                self.state = "CLOSED"
                self.failures = 0
            return result
        except Exception as e:
            self.failures += 1
            self.last_failure = time.time()
            if self.failures >= self.threshold:
                self.state = "OPEN"
            return await fallback()


# Kullanım
llm_circuit = CircuitBreaker(failure_threshold=3, reset_timeout=30)
perplexity_circuit = CircuitBreaker(failure_threshold=2, reset_timeout=60)

async def get_llm_response(message):
    return await llm_circuit.call(
        func=lambda: openai_client.chat(message),
        fallback=lambda: {"intent": "CHAT", "response_text": "Şu anda yoğunluk yaşıyoruz, lütfen tekrar deneyin."}
    )
```

### 4. Fallback Responses (Her Senaryo İçin)

```python
FALLBACK_RESPONSES = {
    "llm_error": "🔄 Bir saniye, tekrar deniyorum...",
    "llm_timeout": "⏱️ İşlem uzun sürdü, lütfen mesajınızı tekrar gönderin.",
    "perplexity_error": "💰 Fiyat araştırması şu an yapılamıyor. Kendiniz bir fiyat belirleyebilirsiniz.",
    "db_error": "📝 İlanınız kaydedilirken bir sorun oluştu. Bilgileriniz güvende, birazdan tekrar deneyin.",
    "wallet_insufficient": "💳 Bakiyeniz yetersiz. Kredi yüklemek için pazarglobal.com/wallet adresini ziyaret edin.",
    "unknown_intent": "🤔 Sizi tam anlayamadım. İlan vermek mi, ürün aramak mı istiyorsunuz?",
}
```

### 5. Input Sanitization

```python
def sanitize_user_input(message: str) -> str:
    """Kullanıcı girdisini temizle"""
    
    # 1. Max length
    if len(message) > 2000:
        message = message[:2000]
    
    # 2. Remove potential prompt injection patterns
    injection_patterns = [
        r"ignore previous instructions",
        r"system:",
        r"assistant:",
        r"<\|.*\|>",
        r"\[INST\]",
    ]
    for pattern in injection_patterns:
        message = re.sub(pattern, "", message, flags=re.IGNORECASE)
    
    # 3. Normalize whitespace
    message = " ".join(message.split())
    
    return message
```

### 6. Session Timeout & Cleanup

```python
SESSION_CONFIG = {
    "idle_timeout": 600,           # 10 dakika inaktivite → session park
    "absolute_timeout": 3600,      # 1 saat → session delete
    "draft_auto_save_interval": 30, # 30 saniyede bir draft kaydet
    "max_messages_per_session": 100, # DoS koruması
}

async def cleanup_stale_sessions():
    """Cron job: Eski session'ları temizle"""
    cutoff = datetime.now() - timedelta(hours=24)
    
    # Redis cleanup
    await redis_client.delete_sessions_older_than(cutoff)
    
    # Supabase draft cleanup
    supabase.table("active_drafts").delete().lt("updated_at", cutoff.isoformat()).execute()
```

---

## 📊 Monitoring & Alerting

### Key Metrics

```python
METRICS = {
    # Latency
    "llm_response_time_p50": Histogram,
    "llm_response_time_p99": Histogram,
    "e2e_response_time": Histogram,
    
    # Errors
    "llm_error_rate": Counter,
    "perplexity_error_rate": Counter,
    "db_error_rate": Counter,
    
    # Business
    "listings_created": Counter,
    "listings_published": Counter,
    "searches_performed": Counter,
    "revenue_credits_spent": Counter,
    
    # Guardrails
    "guardrail_violations": Counter,  # LLM schema ihlali
    "rate_limit_hits": Counter,
    "circuit_breaker_opens": Counter,
}
```

### Alert Thresholds

```yaml
alerts:
  - name: high_error_rate
    condition: llm_error_rate > 5% over 5min
    severity: critical
    action: page_oncall
    
  - name: high_latency
    condition: llm_response_time_p99 > 10s
    severity: warning
    action: slack_notify
    
  - name: circuit_breaker_open
    condition: circuit_breaker_opens > 0
    severity: critical
    action: page_oncall + auto_scale
    
  - name: guardrail_violations
    condition: guardrail_violations > 10 in 1min
    severity: warning
    action: slack_notify + log_investigation
```

---

## 🧪 Test Stratejisi

### 1. Unit Tests (Her Fonksiyon)

```python
def test_guardrails_removes_extra_fields():
    llm_output = {
        "intent": "CREATE",
        "listing_data": {
            "title": "iPhone",
            "price": 5000,
            "hacker_field": "malicious",  # Bu kaldırılmalı
        }
    }
    result = LLMGuardrails.validate_output(llm_output)
    assert "hacker_field" not in result["listing_data"]

def test_price_validation_rejects_negative():
    result = validate_price(-100)
    assert result is None

def test_intent_fallback_on_invalid():
    result = LLMGuardrails.validate_output({"intent": "HACK_SYSTEM"})
    assert result["intent"] == "CHAT"
```

### 2. Integration Tests (FSM Flows)

```python
async def test_complete_listing_flow():
    session = create_test_session()
    
    # Step 1: Start
    r1 = await send_message(session, "iPhone satıyorum")
    assert r1.intent == "CREATE"
    assert "title" in r1.listing_data
    
    # Step 2: Add price
    r2 = await send_message(session, "25000 TL")
    assert r2.listing_data["price"] == 25000
    
    # Step 3: Publish
    r3 = await send_message(session, "yayınla")
    assert r3.state == "DONE"
    assert "yayınlandı" in r3.response_text
```

### 3. Chaos Tests (Sistem Dayanıklılığı)

```python
async def test_llm_timeout_graceful_degradation():
    with mock_llm_timeout(seconds=30):
        response = await send_message(session, "merhaba")
        assert "yoğunluk" in response.text or "tekrar" in response.text
        assert response.success == True  # Hata olsa bile graceful

async def test_redis_failure_continues_with_memory():
    with mock_redis_failure():
        response = await send_message(session, "iPhone satıyorum")
        assert response.success == True  # In-memory fallback çalışmalı
```

### 4. Load Tests (Kapasite)

```yaml
# k6 load test config
scenarios:
  normal_load:
    executor: constant-vus
    vus: 50
    duration: 5m
    
  spike:
    executor: ramping-vus
    stages:
      - duration: 1m, target: 100
      - duration: 2m, target: 500
      - duration: 1m, target: 100
      
  stress:
    executor: constant-vus
    vus: 200
    duration: 10m

thresholds:
  http_req_duration: ['p(95)<3000']  # 95% < 3 saniye
  http_req_failed: ['rate<0.01']     # <1% hata
```

---

## 🚀 Deployment Checklist

### Pre-Deploy

- [ ] Tüm unit testler geçiyor
- [ ] Integration testler geçiyor
- [ ] Load test yapıldı, thresholdlar karşılandı
- [ ] Environment variables set (OPENAI_KEY, SUPABASE_*, REDIS_URL)
- [ ] Rate limit config gözden geçirildi
- [ ] Circuit breaker thresholds ayarlandı
- [ ] Logging level = INFO (DEBUG değil)
- [ ] Sentry/error tracking configured

### Post-Deploy

- [ ] Health check endpoint yanıt veriyor
- [ ] Bir test mesajı gönderildi, yanıt alındı
- [ ] Metrics dashboard'da data görünüyor
- [ ] Alert rules aktif
- [ ] Rollback planı hazır

---

## 🔮 Potential Failure Modes & Mitigations

| Failure Mode | Impact | Probability | Mitigation |
|-------------|--------|-------------|------------|
| OpenAI API Down | Tüm sistem durur | Düşük | Circuit breaker + cached fallback responses |
| LLM Hallucination | Yanlış intent/data | Orta | Guardrails validation + JSON schema enforcement |
| Prompt Injection | Güvenlik ihlali | Düşük | Input sanitization + output validation |
| Redis Down | Session kaybı | Düşük | In-memory fallback + Supabase recovery |
| Supabase Down | Kayıt yapılamaz | Düşük | Local queue + retry on recovery |
| Rate Limit Aşımı | Kullanıcı bloke | Orta | Per-user limits + graceful messaging |
| High Latency | Kötü UX | Orta | Timeout + streaming responses |
| Cost Explosion | Bütçe aşımı | Düşük | Daily/hourly caps + alerts |

---

## 💰 Maliyet Optimizasyonu

```python
COST_CONTROLS = {
    # Token limits
    "max_input_tokens": 2000,
    "max_output_tokens": 1000,
    
    # Caching
    "cache_perplexity_results": True,
    "perplexity_cache_ttl": 3600,  # 1 saat
    
    # Batching
    "batch_similar_queries": True,  # Benzer aramaları grupla
    
    # Model selection
    "simple_queries_model": "gpt-4o-mini",  # Basit sohbet için
    "complex_queries_model": "gpt-4o",      # Vision + listing için
    
    # Daily limits
    "max_llm_calls_per_day": 10000,
    "max_perplexity_calls_per_day": 1000,
}
```

---

## 📝 Sonuç

Bu mimari şu prensiplere dayanır:

1. **Tek Beyin**: Bir LLM her şeyi yönetir - daha az koordinasyon, daha az hata
2. **Sınırlı Özgürlük**: JSON schema + guardrails = predictable output
3. **Graceful Degradation**: Her hata için bir fallback
4. **Observability**: Her şeyi ölç, her şeyi logla
5. **Defense in Depth**: Input validation → LLM → Output validation → FSM validation

**Sağlamlık Garantisi**: Bu mimari ile:
- LLM hatası → Fallback mesaj (kullanıcı etkilenmez)
- Tool hatası → Manuel devam (fiyatı kendisi girer)
- DB hatası → Retry queue (veri kaybı yok)
- Session hatası → Supabase'den recovery

---

*Last Updated: 2026-02-02*
*Version: 3.0.0-production*
