# Multi-Intent Detection & Ambiguous Intent Handling (v2 - Finalized)

## Problem

Kullanıcı tek bir mesajda birden fazla intent (niyet) belirttiğinde, sistem yalnızca bir tanesini seçiyordu. Bu kullanıcı deneyimini olumsuz etkiliyordu.

### Örnek Senaryo:
**Kullanıcı:** "bende iyim elimde bir samsung s21 var satmak istiyorum kaç para eder"

**Tespit edilen intentler:**
1. `create_listing` - "satmak istiyorum" 
2. `price_inquiry` - "kaç para eder"

**Eski davranış:** ❌ Direkt `create_listing`'e yönlendirdi, fiyat sorusunu görmezden geldi

**Yeni davranış:** ✅ Kullanıcıya iki seçenek sunuyor:
```
İki konuda yardımcı olabilirim:

1️⃣ Fiyatını öğrenmek (hızlı değerlendirme)
2️⃣ Satış ilanı oluşturmak

Hangisiyle başlayalım? (1 veya 2 yazın)
```

---

## Çözüm (v2 - ChatGPT Feedback İncorporated)

### ✅ İyileştirmeler (ChatGPT Feedback)

1. **`small_talk` → `clarify_intent`** ✓
   - Ambiguous durum artık özel bir state
   - FSM'de ayrı handling
   - Tool call yok, agent invocation yok

2. **Kısa mesajlar** ✓
   - Numbered options (1️⃣, 2️⃣)
   - Tek satır soru
   - Detaylar sonra

3. **TTL (Time To Live)** ✓
   - `pending_clarification` 2 dakika sonra expire
   - Ghost state önlendi
   - Auto-cleanup

4. **Hard Rule: price + create** ✓
   - Her zaman clarify zorunlu
   - Çünkü kullanıcı önce fiyat öğrenmek istiyor

### 1. Intent Router Güncellemeleri

#### `agents/intent_router.py`
- `classify_intent()` fonksiyonu artık **dict** döndürüyor (string yerine)
- Yeni response format:
  ```python
  {
      "intent": "ambiguous",  # veya "create_listing", "search_listings", etc.
      "detected_intents": ["create_listing", "price_inquiry"],  # ambiguous ise
      "confidence": "high"  # "high", "medium", "low"
  }
  ```

- Yeni intent tipi: `"ambiguous"` - birden fazla intent tespit edildiğinde
- Yeni intent kategorisi: `"price_inquiry"` - fiyat araştırması istekleri için

#### `config/prompts.py`
- **Multi-Intent Detection kuralları eklendi:**
  ```
  Ambiguous örnekleri:
  - "samsung s21 var satmak istiyorum kaç para eder" 
    → hem create_listing hem price_inquiry
  - "iPhone 13 aramak istiyorum ama benim iPhone 11'i de satayım"
    → hem search_listings hem create_listing
  - "kaç liraya satabilirim ve nasıl ilan veririm"
    → hem price_inquiry hem create_listing
  ```

### 2. Webchat FSM Güncellemeleri (v2)

#### `api/webchat.py`

**Yeni fonksiyonlar:**

1. **`_handle_ambiguous_intent()`** (v2 - iyileştirilmiş)
   ```python
   session["intent"] = "clarify_intent"  # NOT small_talk!
   session["locked_intent"] = None  # Explicitly unlock
   session["pending_clarification"] = {
       "detected_intents": detected_intents,
       "original_message": message_body,
       "expires_at": time.time() + 120  # TTL: 2 minutes
   }
   ```

2. **`_generate_clarification_message()`** (v2 - kısaltıldı)
   - Minimal mesaj
   - Numbered options (1️⃣, 2️⃣, 3️⃣)
   - Hard rule: price + create → her zaman sorulur
   
   ```
   İki konuda yardımcı olabilirim:
   
   1️⃣ Fiyatını öğrenmek (hızlı değerlendirme)
   2️⃣ Satış ilanı oluşturmak
   
   Hangisiyle başlayalım? (1 veya 2 yazın)
   ```

3. **`_parse_clarification_choice()`** (NEW)
   - Number-based: "1", "2", "3"
   - Keyword-based: "fiyat", "ilan", "ara"
   - Fuzzy matching: "bir", "iki", "birinci"

**FSM Flow değişiklikleri (v2):**

```python
# Intent routing'de clarify_intent özel handling:
if intent == "clarify_intent":
    # TTL check
    if expired:
        clear state, re-classify
    else:
        user_choice = _parse_clarification_choice(message, detected_intents)
        if valid:
            acknowledge, set intent, continue flow
        else:
            re-prompt
```

**Yeni Price Inquiry Flow:**
```python
elif intent == "price_inquiry":
    if no media:
        ask for photo or product details
    else:
        run vision analysis
        provide price research (placeholder)
        suggest next actions (create listing / search)
```

---

## Test

### Test Dosyası: `test_ambiguous_intent.py`

Test senaryoları:

**Ambiguous (çift intent):**
- ✅ "samsung s21 var satmak istiyorum kaç para eder"
- ✅ "iPhone 13 aramak istiyorum ama benim iPhone 11'i de satayım"
- ✅ "kaç liraya satabilirim ve nasıl ilan veririm"

**Clear (tek intent):**
- ✅ "iPhone 13 satmak istiyorum" → create_listing
- ✅ "laptop göz atmak istiyorum" → search_listings
- ✅ "nasılsın" → small_talk

**Clarification Response:**
- ✅ "1" → first detected intent
- ✅ "fiyat" → price_inquiry
- ✅ "ilan" → create_listing

### Test çalıştırma:
```bash
cd pazarglobal-agent
python test_ambiguous_intent.py
```

---

## Kullanım Senaryoları (v2)

### Senaryo 1: Fiyat + Satış İsteği (Hard Rule)
**Kullanıcı:** "elimde MacBook var satmak istiyorum kaç para eder"

**Sistem yanıtı:**
```
İki konuda yardımcı olabilirim:

1️⃣ Fiyatını öğrenmek (hızlı değerlendirme)
2️⃣ Satış ilanı oluşturmak

Hangisiyle başlayalım? (1 veya 2 yazın)
```

**Kullanıcı:** "1"

**Sistem:**
```
Tamam, fiyat araştırması yapıyorum... 📊

Fiyat araştırması için:
📷 Ürünün fotoğrafını gönderebilirsiniz
veya
📝 Ürün bilgilerini yazın (marka, model, durum)
```

### Senaryo 2: TTL Expiration
**00:00** - Kullanıcı: "samsung satmak istiyorum kaç para eder"
**00:00** - Sistem: Clarification soruyor

**02:01** - Kullanıcı: "naber" (2 dakika sonra)
**02:01** - Sistem: TTL expired, fresh classification → "small_talk"

### Senaryo 3: Invalid Choice
**Kullanıcı:** "samsung satmak istiyorum kaç para eder"
**Sistem:** Clarification soruyor (1️⃣ veya 2️⃣)

**Kullanıcı:** "bilmiyorum" (geçersiz)
**Sistem:** Re-prompt: "Lütfen seçim yapın: [tekrar soruyor]"

---

## Deployment Checklist

- [x] Intent Router güncellenmiş
- [x] Prompt'lar güncellenmiş  
- [x] Webchat FSM handling eklenmiş
- [x] TTL mechanism eklenmiş
- [x] `clarify_intent` state handling eklenmiş
- [x] `price_inquiry` flow eklenmiş
- [x] Hard rule: price + create → clarify
- [x] Kısa mesajlar
- [x] Choice parser eklenmiş
- [ ] Integration test (Railway'de test edilmeli)
- [ ] OpenAI function calling doğru çalışıyor mu kontrol et
- [ ] Telemetry/logging doğru çalışıyor mu kontrol et

---

## Architecture Notes

### State Machine Transitions

```
User Message
    ↓
Intent Router (classify)
    ↓
┌─────────────────┐
│  ambiguous?     │
├─────────────────┤
│  YES → clarify_intent
│        ↓
│     TTL check
│        ↓
│     valid? → parse choice → set intent
│     expired? → re-classify
│     invalid? → re-prompt
│
│  NO  → normal flow
│        ↓
│     create_listing / search / price_inquiry / small_talk
└─────────────────┘
```

### Hard Rules

1. **price_inquiry + create_listing = ambiguous (always)**
   - Rationale: User wants price first, then decides
   
2. **TTL = 120 seconds**
   - Prevents ghost states
   - Auto-cleanup
   
3. **No tool calls in clarify_intent**
   - Only message to user
   - No draft creation
   - No agent invocation

4. **Explicit unlock on ambiguous**
   - `locked_intent = None`
   - Fresh start after clarification

---

## Migration Notes

**Breaking Changes:** Yok - geriye uyumlu

**Behavior Changes:**
- Kullanıcı multi-intent mesaj gönderdiğinde artık clarification alacak
- `clarify_intent` yeni bir state (FSM'de handle edilmeli)
- TTL nedeniyle eski clarification'lar expire olabilir

**Rollback:** 
Eğer sorun çıkarsa:
1. `intent_router.py`'deki function'ı eski haline döndür (string return)
2. `webchat.py`'deki clarify_intent handling'i kaldır

---

## İlgili Dosyalar

- [agents/intent_router.py](c:\Users\emrah badas\OneDrive\Desktop\denemee\pazarglobal-agent\agents\intent_router.py#L20-L74)
- [config/prompts.py](c:\Users\emrah badas\OneDrive\Desktop\denemee\pazarglobal-agent\config\prompts.py#L6-L73)
- [api/webchat.py](c:\Users\emrah badas\OneDrive\Desktop\denemee\pazarglobal-agent\api\webchat.py#L138-L247)
  - _handle_ambiguous_intent (L138-164)
  - _generate_clarification_message (L167-192)
  - _parse_clarification_choice (L195-230)
  - clarify_intent FSM handling (L3800-3860)
  - price_inquiry flow (L4750-4820)

---

## Log Examples

**Ambiguous Intent tespit edildiğinde:**
```
INFO | Classified intent: ambiguous, detected_intents: ['create_listing', 'price_inquiry'], confidence: high
INFO | fsm_event: intent_ambiguous (session=xxx, detected_intents=['create_listing', 'price_inquiry'])
INFO | FSM state: clarify_intent (TTL: 120s)
```

**Kullanıcı choice yaptığında:**
```
INFO | Clarification choice parsed: price_inquiry (from '1')
INFO | fsm_event: intent_selected (session=xxx, intent='price_inquiry', source='clarification')
INFO | FSM state: price_inquiry
```

**TTL expire:**
```
WARN | Clarification expired (session=xxx, age=125s)
INFO | Re-classifying with fresh context
INFO | Classified intent: small_talk
```

### 1. Intent Router Güncellemeleri

#### `agents/intent_router.py`
- `classify_intent()` fonksiyonu artık **dict** döndürüyor (string yerine)
- Yeni response format:
  ```python
  {
      "intent": "ambiguous",  # veya "create_listing", "search_listings", etc.
      "detected_intents": ["create_listing", "price_inquiry"],  # ambiguous ise
      "confidence": "high"  # "high", "medium", "low"
  }
  ```

- Yeni intent tipi: `"ambiguous"` - birden fazla intent tespit edildiğinde
- Yeni intent kategorisi: `"price_inquiry"` - fiyat araştırması istekleri için

#### `config/prompts.py`
- **Multi-Intent Detection kuralları eklendi:**
  ```
  Ambiguous örnekleri:
  - "samsung s21 var satmak istiyorum kaç para eder" 
    → hem create_listing hem price_inquiry
  - "iPhone 13 aramak istiyorum ama benim iPhone 11'i de satayım"
    → hem search_listings hem create_listing
  - "kaç liraya satabilirim ve nasıl ilan veririm"
    → hem price_inquiry hem create_listing
  ```

### 2. Webchat FSM Güncellemeleri

#### `api/webchat.py`

**Yeni fonksiyonlar:**

1. **`_handle_ambiguous_intent()`**
   - Ambiguous durumu logluyor
   - Session'ı small_talk'a set ediyor (intent lock'lama)
   - `pending_clarification` bilgisini session'a kaydediyor

2. **`_generate_clarification_message()`**
   - Kullanıcıya friendly mesaj oluşturuyor
   - Tespit edilen intentlere göre seçenekler sunuyor
   - Örnek çıktı:
     ```
     Size nasıl yardımcı olabilirim? 🤔

     📊 Fiyat araştırması yapmak için görsel yükleyebilir veya 'kaç para eder' diye sorabilirsiniz
     📝 İlan oluşturmak için 'ilan vermek istiyorum' veya 'satmak istiyorum' diyebilirsiniz

     💡 Hangi işlemi yapmak istersiniz?
     ```

3. **`sanitize_classified_intent()`** - güncellendi
   - Artık dict alıyor ve dict döndürüyor
   - Legacy string desteği de var (geriye uyumluluk)

**FSM Flow değişiklikleri:**
```python
# Intent classification sonrası:
if intent == "ambiguous" and detected_intents:
    # Kullanıcıya sor, flow'u durdur
    await _handle_ambiguous_intent(...)
    return JSONResponse(content={
        "message": clarification_message,
        "intent": "ambiguous",
        "detected_intents": detected_intents
    })
```

---

## Test

### Test Dosyası: `test_ambiguous_intent.py`

Test senaryoları:

**Ambiguous (çift intent):**
- ✅ "samsung s21 var satmak istiyorum kaç para eder"
- ✅ "iPhone 13 aramak istiyorum ama benim iPhone 11'i de satayım"
- ✅ "kaç liraya satabilirim ve nasıl ilan veririm"

**Clear (tek intent):**
- ✅ "iPhone 13 satmak istiyorum" → create_listing
- ✅ "laptop göz atmak istiyorum" → search_listings
- ✅ "nasılsın" → small_talk

### Test çalıştırma:
```bash
cd pazarglobal-agent
python test_ambiguous_intent.py
```

---

## Kullanım Senaryoları

### Senaryo 1: Fiyat + Satış İsteği
**Kullanıcı:** "elimde MacBook var satmak istiyorum kaç para eder"

**Sistem yanıtı:**
```
Size nasıl yardımcı olabilirim? 🤔

📊 Fiyat araştırması yapmak için görsel yükleyebilir veya 'kaç para eder' diye sorabilirsiniz
📝 İlan oluşturmak için 'ilan vermek istiyorum' veya 'satmak istiyorum' diyebilirsiniz

💡 Hangi işlemi yapmak istersiniz?
```

**Kullanıcı seçer:**
- "fiyat araştırması yap" → Görsel yükleme akışı
- "ilan vermek istiyorum" → İlan oluşturma akışı

### Senaryo 2: Arama + Satış
**Kullanıcı:** "iPhone 13 bakmak istiyorum ama benimkini de satayım"

**Sistem yanıtı:**
```
Size nasıl yardımcı olabilirim? 🤔

🔍 İlan aramak için 'ara' veya 'göster' diyebilirsiniz
📝 İlan oluşturmak için 'ilan vermek istiyorum' veya 'satmak istiyorum' diyebilirsiniz

💡 Hangi işlemi yapmak istersiniz?
```

### Senaryo 3: Tek Intent (normal flow)
**Kullanıcı:** "iPhone 13 satmak istiyorum"

**Sistem:** Direkt `create_listing` flow'una girer, clarification yapmaz.

---

## Deployment Checklist

- [x] Intent Router güncellenmiş
- [x] Prompt'lar güncellenmiş
- [x] Webchat FSM handling eklenmiş
- [x] Test script oluşturulmuş
- [ ] Integration test (Railway'de test edilmeli)
- [ ] OpenAI function calling doğru çalışıyor mu kontrol et
- [ ] Telemetry/logging doğru çalışıyor mu kontrol et

---

## İleriye Dönük İyileştirmeler

### 1. Context-Aware Clarification
Kullanıcının geçmiş davranışına göre daha akıllı seçenekler:
- Eğer daha önce hiç ilan oluşturmadıysa → Önce fiyat araştırması öner
- Eğer aktif draft varsa → "Devam etmek ister misiniz?" ekle

### 2. Quick Actions
Mesaja doğrudan buton ekle:
```json
{
  "message": "...",
  "quick_actions": [
    {"text": "💰 Fiyat Araştır", "action": "price_inquiry"},
    {"text": "📝 İlan Ver", "action": "create_listing"}
  ]
}
```

### 3. Smart Intent Resolution
Bazı durumlarda otomatik karar ver:
- "kaç para eder" + görsel varsa → Direkt price inquiry
- "satmak istiyorum" + detaylı bilgi varsa → Direkt create_listing

### 4. Analytics
Ambiguous durumları takip et:
- Hangi kombinasyonlar en çok görülüyor?
- Kullanıcılar hangi seçeneği seçiyor?
- Clarification'dan sonra completion rate nedir?

---

## Migration Notes

**Breaking Changes:** Yok - geriye uyumlu

**Behavior Changes:**
- Kullanıcı multi-intent mesaj gönderdiğinde artık clarification alacak
- Bazı mesajlar artık `small_talk` olarak handle edilebilir (ambiguous durumlar)

**Rollback:** 
Eğer sorun çıkarsa, sadece intent_router.py'deki function'ı eski haline döndür:
```python
# Eski return:
return intent  # string

# Yeni return:
return {"intent": intent, "detected_intents": []}  # dict
```

---

## İlgili Dosyalar

- `agents/intent_router.py` - Intent classification logic
- `config/prompts.py` - INTENT_ROUTER_PROMPT
- `api/webchat.py` - FSM handling & clarification
- `test_ambiguous_intent.py` - Test script

## Log Examples

**Ambiguous Intent tespit edildiğinde:**
```
INFO | Classified intent: ambiguous, detected_intents: ['create_listing', 'price_inquiry'], confidence: high
INFO | fsm_event: intent_ambiguous (session=xxx, detected_intents=['create_listing', 'price_inquiry'])
```

**Kullanıcı clarification sonrası seçim yaptığında:**
```
INFO | Classified intent: create_listing, detected_intents: [], confidence: high
INFO | fsm_event: intent_lock (session=xxx, new_intent='create_listing')
```
