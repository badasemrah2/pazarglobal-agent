# 🚀 Quick Start Guide

PazarGlobal Agent sistemini hızlıca çalıştırmak için bu rehberi takip edin.

## ⚡ 5 Dakikada Başla

### 1. Environment Setup (2 dakika)

```bash
cd pazarglobal-agent

# .env dosyası oluştur
cp .env.example .env

# .env dosyasını düzenle (minimum gerekli alanlar)
# OPENAI_API_KEY=sk-...
# SUPABASE_URL=https://xxx.supabase.co
# SUPABASE_KEY=...
# SUPABASE_SERVICE_KEY=...
```

### 2. Dependencies (1 dakika)

```bash
# Python 3.10+ gerekli
python --version

# Virtual environment (önerilen)
python -m venv venv

# Windows
venv\Scripts\activate

# Mac/Linux
source venv/bin/activate

# Dependencies yükle
pip install -r requirements.txt
```

### 3. Redis Başlat (1 dakika)

**Docker ile (en kolay):**
```bash
docker run -d -p 6379:6379 --name redis redis:alpine
```

**veya Windows için Redis:**
- [Redis for Windows](https://github.com/microsoftarchive/redis/releases) indir
- Çalıştır

### 4. Çalıştır (1 dakika)

```bash
# API başlat
python main.py
```

API çalışıyor: http://localhost:8000

### 5. Test Et

**Browser'da aç:**
```
http://localhost:8000/docs
```

Swagger UI üzerinden test edebilirsiniz!

## 🧪 İlk Test

### REST API ile Test

```bash
# Yeni session oluştur
curl -X POST http://localhost:8000/webchat/session/new

# Response:
# {"session_id": "web_xxx", "message": "Session created successfully"}

# Mesaj gönder
curl -X POST http://localhost:8000/webchat/message \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "web_xxx",
    "message": "Merhaba! iPhone satmak istiyorum"
  }'
```

### Python ile Test

```python
import requests

# Session oluştur
response = requests.post("http://localhost:8000/webchat/session/new")
session_id = response.json()["session_id"]

# Mesaj gönder
response = requests.post(
    "http://localhost:8000/webchat/message",
    json={
        "session_id": session_id,
        "message": "iPhone 13 satmak istiyorum, fiyat 20000 TL"
    }
)

print(response.json())
```

### Test Script ile

```bash
python test_agent.py
```

## 📱 Frontend Entegrasyonu (5 dakika)

### 1. Frontend Projesine Git

```bash
cd ../pazarglobal-frontend
```

### 2. Agent Service Ekle

`src/services/agent-api.ts` dosyası oluştur:

```typescript
const AGENT_API_URL = 'http://localhost:8000';

export async function createSession() {
  const response = await fetch(`${AGENT_API_URL}/webchat/session/new`, {
    method: 'POST',
  });
  return response.json();
}

export async function sendMessage(sessionId: string, message: string) {
  const response = await fetch(`${AGENT_API_URL}/webchat/message`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, message })
  });
  return response.json();
}
```

### 3. ChatBox'ta Kullan

Mevcut `ChatBox.tsx` dosyanızı güncelleyin (detaylı kod `FRONTEND_INTEGRATION.md` dosyasında).

## 🎯 Temel Kullanım Senaryoları

### Senaryo 1: İlan Oluşturma

```
User: "iPhone 13 Pro 256GB satmak istiyorum, sıfır kutusunda, fiyat 25000 TL"

Agent: 
✅ İlan taslağınız güncellendi!

📝 Başlık: iPhone 13 Pro 256GB
📄 Açıklama: Sıfır kutusunda iPhone 13 Pro 256GB
💰 Fiyat: 25000 TL

Değişiklik yapmak ister misiniz? Yoksa yayınlamak için 'yayınla' yazın.
```

### Senaryo 2: İlan Düzenleme

```
User: "Fiyatı 24000 TL yap"

Agent:
✅ Fiyat güncellendi: 24000 TL
```

### Senaryo 3: İlan Yayınlama

```
User: "yayınla"

Agent:
Cüzdanınızda 10 kredi var. İlan yayınlamak 1 kredi gerektirir.
Onaylıyor musunuz? (evet/hayır)

User: "evet"

Agent:
✅ İlanınız başarıyla yayınlandı!
İlan ID: xxx-xxx-xxx
```

### Senaryo 4: İlan Arama

```
User: "20000 TL altında iPhone ara"

Agent:
🔍 5 ilan bulundu:

1. iPhone 12 Pro 128GB
   💰 18000 TL
   📍 Elektronik

2. iPhone 11 256GB
   💰 15000 TL
   📍 Elektronik

...
```

### Senaryo 5: Genel Sohbet

```
User: "PazarGlobal nedir?"

Agent:
PazarGlobal, AI destekli bir ilan platformudur. Kolayca ilan oluşturabilir,
arama yapabilir ve güvenli alışveriş yapabilirsiniz...
```

## 🔧 Konfigürasyon

### Minimal Konfigürasyon

`.env` dosyasında sadece bunlar yeterli:

```env
OPENAI_API_KEY=sk-...
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_KEY=...
SUPABASE_SERVICE_KEY=...
REDIS_URL=redis://localhost:6379
```

### Tam Konfigürasyon

Tüm seçenekler için `.env.example` dosyasına bakın.

## 📊 API Endpoints

### WebChat (Frontend için)

- `POST /webchat/session/new` - Yeni session
- `POST /webchat/message` - Mesaj gönder
- `GET /webchat/session/{id}` - Session bilgisi
- `GET /webchat/history/{id}` - Chat geçmişi
- `WS /webchat/ws/{id}` - WebSocket

### WhatsApp (Twilio için)

- `POST /whatsapp/webhook` - WhatsApp mesajları
- `GET /whatsapp/webhook` - Webhook verify

### Utility

- `GET /` - API bilgisi
- `GET /health` - Health check
- `GET /docs` - Swagger UI

## 🐛 Sorun Giderme

### "Connection refused" hatası

```bash
# Redis çalışıyor mu?
docker ps | grep redis

# Yoksa başlat
docker start redis
```

### "OpenAI API key not found"

```bash
# .env dosyası var mı?
ls -la .env

# OPENAI_API_KEY set edilmiş mi?
cat .env | grep OPENAI_API_KEY
```

### "Module not found" hatası

```bash
# Virtual environment aktif mi?
which python

# Dependencies yüklü mü?
pip list | grep openai
pip list | grep fastapi

# Yoksa tekrar yükle
pip install -r requirements.txt
```

### Port zaten kullanımda

```bash
# main.py'de farklı port kullan
# veya çalışan servisi durdur

# Windows
netstat -ano | findstr :8000
taskkill /PID <PID> /F

# Mac/Linux
lsof -i :8000
kill -9 <PID>
```

## 📚 Sonraki Adımlar

1. **Frontend Entegrasyonu**: `FRONTEND_INTEGRATION.md` dosyasını okuyun
2. **Deployment**: `DEPLOYMENT.md` dosyasını okuyun
3. **Architecture**: `pazar_global_agent_architecture_readme (1).md` dosyasını okuyun
4. **Customization**: Agent prompt'larını `config/prompts.py` dosyasında düzenleyin

## 💡 İpuçları

### Development Mode

```bash
# Auto-reload ile çalıştır
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### Debug Mode

```env
# .env
DEBUG=true
LOG_LEVEL=DEBUG
```

### Test Different Models

```env
# .env
OPENAI_MODEL=gpt-3.5-turbo  # Daha ucuz
# veya
OPENAI_MODEL=gpt-4-turbo-preview  # Daha güçlü
```

## 🎓 Öğrenme Kaynakları

- [OpenAI Function Calling](https://platform.openai.com/docs/guides/function-calling)
- [FastAPI Tutorial](https://fastapi.tiangolo.com/tutorial/)
- [Supabase Python](https://supabase.com/docs/reference/python/introduction)
- [Redis Python](https://redis-py.readthedocs.io/)

## ✅ Başarı Kontrolü

Herşey çalışıyorsa:

- [ ] `http://localhost:8000` açılıyor
- [ ] `/docs` sayfası görünüyor
- [ ] Session oluşturulabiliyor
- [ ] Mesaj gönderilebiliyor
- [ ] Response alınabiliyor
- [ ] Redis'e bağlanabiliyor
- [ ] Supabase'e bağlanabiliyor

## 🎉 Tebrikler!

PazarGlobal Agent sisteminiz çalışıyor! 

Sorularınız için:
- README.md - Genel bakış
- FRONTEND_INTEGRATION.md - Frontend bağlantısı
- DEPLOYMENT.md - Production deployment
- Architecture README - Sistem mimarisi
