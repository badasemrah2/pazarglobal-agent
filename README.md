# PazarGlobal Agent System

OpenAI SDK tabanlı, paralel agent mimarisi ile çalışan PazarGlobal marketplace AI asistanı.

## 🎯 Özellikler

- **4 Ana Hat (Workflow)**:
  - Create Listing: İlan oluşturma ve düzenleme
  - Publish/Delete: İlan yayınlama ve silme
  - Search Listings: İlan arama
  - Small Talk: Genel sohbet

- **Paralel Agent Sistemi**: TitleAgent, DescriptionAgent, PriceAgent, ImageAgent paralel çalışır
- **WhatsApp Entegrasyonu**: Twilio üzerinden WhatsApp desteği
- **WebChat API**: Frontend için REST ve WebSocket desteği
- **OpenAI Vision**: Görsel analiz ve kategori tespiti
- **State Management**: Redis ile oturum yönetimi
- **Railway Ready**: Railway'e deploy için hazır

## 📁 Proje Yapısı

```
pazarglobal-agent/
├── agents/                 # Tüm AI agentlar
│   ├── base_agent.py      # Base agent class
│   ├── intent_router.py   # Intent classifier
│   ├── title_agent.py     # Başlık agent
│   ├── description_agent.py
│   ├── price_agent.py
│   ├── image_agent.py
│   ├── composer_agent.py  # Orkestra agent
│   ├── publish_delete_agent.py
│   ├── search_agents.py
│   └── small_talk_agent.py
├── tools/                  # Agent toolları
│   ├── base_tool.py       # Base tool class
│   ├── draft_tools.py     # Draft CRUD
│   ├── listing_tools.py   # Listing operations
│   ├── wallet_tools.py    # Kredi işlemleri
│   └── image_tools.py     # Görsel işleme
├── services/              # Servis katmanı
│   ├── openai_client.py   # OpenAI wrapper
│   ├── supabase_client.py # Supabase DB
│   └── redis_client.py    # Redis state
├── api/                   # API endpoints
│   ├── whatsapp.py        # WhatsApp webhook
│   └── webchat.py         # WebChat API
├── config/                # Konfigürasyon
│   ├── settings.py        # App settings
│   └── prompts.py         # Agent prompts
├── main.py               # FastAPI app
├── requirements.txt      # Python dependencies
├── Procfile             # Railway start command
└── railway.json         # Railway config
```

## 🚀 Kurulum

### 1. Environment Variables

`.env` dosyası oluşturun:

```bash
cp .env.example .env
```

Gerekli değişkenleri doldurun:
- `OPENAI_API_KEY`: OpenAI API anahtarı
- `SUPABASE_URL`: Supabase project URL
- `SUPABASE_SERVICE_KEY`: Supabase service key
- `REDIS_URL`: Redis connection URL
- `TWILIO_ACCOUNT_SID`: Twilio hesap SID
- `TWILIO_AUTH_TOKEN`: Twilio auth token

### 2. Dependencies

```bash
pip install -r requirements.txt
```

### 3. Redis

Redis başlatın (Docker ile):

```bash
docker run -d -p 6379:6379 redis:alpine
```

### 4. Çalıştırma

```bash
python main.py
```

API şu adreste çalışacak: `http://localhost:8000`

## 📡 API Kullanımı

### WhatsApp Webhook

Twilio'da webhook URL'ini ayarlayın:
```
https://your-domain.railway.app/whatsapp/webhook
```

### WebChat REST API

```bash
# Yeni session oluştur
POST /webchat/session/new

# Mesaj gönder
POST /webchat/message
{
  "session_id": "web_xxx",
  "message": "iPhone 13 satmak istiyorum",
  "user_id": "user123"
}

# Session bilgisi
GET /webchat/session/{session_id}

# Chat geçmişi
GET /webchat/history/{session_id}
```

### WebSocket

```javascript
const ws = new WebSocket('ws://localhost:8000/webchat/ws/session_123');

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log('Response:', data);
};

ws.send(JSON.stringify({
  message: "iPhone 13 satmak istiyorum",
  user_id: "user123"
}));
```

## 🌐 Frontend Entegrasyonu

Frontend projenizde (pazarglobal-frontend) şu bağlantıyı kullanın:

```typescript
// src/services/agent-api.ts
const AGENT_API_URL = process.env.VITE_AGENT_API_URL || 'http://localhost:8000';

export async function sendChatMessage(sessionId: string, message: string) {
  const response = await fetch(`${AGENT_API_URL}/webchat/message`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, message })
  });
  return response.json();
}
```

## 🚂 Railway Deploy

### 1. Railway Projesi Oluştur

```bash
# Railway CLI kur
npm install -g @railway/cli

# Login
railway login

# Proje oluştur
railway init
```

### 2. Environment Variables

Railway dashboard'da tüm environment variables'ları ekleyin.

### 3. Redis Ekle

Railway'de Redis service ekleyin:
```bash
railway add
# Redis seçin
```

### 4. Deploy

```bash
git add .
git commit -m "Initial deploy"
railway up
```

## 🔄 Mimari Akış

### Create Listing Flow

```
User Message
    ↓
IntentRouter → "create_listing"
    ↓
ComposerAgent
    ├── TitleAgent (parallel)
    ├── DescriptionAgent (parallel)
    ├── PriceAgent (parallel)
    └── ImageAgent (parallel)
    ↓
Draft Updated (same listing_id)
    ↓
Response to User
```

### Publish Flow

```
User: "yayınla"
    ↓
PublishDeleteAgent
    ├── Check wallet balance
    ├── Get user confirmation
    ├── Publish listing
    └── Deduct credits
    ↓
Listing Published
```

### Search Flow

```
User: "iPhone aramak istiyorum"
    ↓
SearchComposerAgent
    ├── CategorySearchAgent (parallel)
    ├── PriceSearchAgent (parallel)
    └── ContentSearchAgent (parallel)
    ↓
Results Combined & Deduplicated
    ↓
Response to User
```

## 🛠️ Geliştirme

### Yeni Tool Ekleme

```python
# tools/my_tool.py
from .base_tool import BaseTool

class MyTool(BaseTool):
    def get_name(self) -> str:
        return "my_tool"
    
    def get_description(self) -> str:
        return "Tool description"
    
    def get_parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {...},
            "required": [...]
        }
    
    async def execute(self, **kwargs) -> dict:
        # Implementation
        return self.format_success(data)
```

### Yeni Agent Ekleme

```python
# agents/my_agent.py
from .base_agent import BaseAgent
from tools import my_tool

class MyAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="MyAgent",
            system_prompt="Agent prompt here",
            tools=[my_tool]
        )
```

## 📊 Monitoring

Loglar için:
```bash
tail -f logs/app.log
```

Railway'de logs:
```bash
railway logs
```

## 🔐 Güvenlik

- API keys'leri asla commit etmeyin
- Production'da CORS ayarlarını düzenleyin
- Rate limiting aktif
- Environment variables ile gizli bilgileri yönetin

## 📝 Lisans

MIT

## 🤝 Katkıda Bulunma

Pull request'ler kabul edilir. Büyük değişiklikler için önce issue açın.

## 📧 İletişim

Sorularınız için issue açabilirsiniz.
