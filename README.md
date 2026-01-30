# PazarGlobal Agent v2

Türkiye'nin ilk **konuşarak ilan veren** pazar yeri platformu.

## 🎯 Özellikler

- **İlan Oluşturma**: Doğal dilde ilan verme
- **Görsel Analiz**: Resimden ürün tanıma (GPT-4 Vision)
- **Güvenlik**: Illegal içerik engelleme
- **Piyasa Fiyatı**: Perplexity API ile fiyat araştırması
- **Arama**: Akıllı ilan arama
- **Multi-Channel**: WhatsApp + WebChat

## 🚀 Kurulum

```bash
# Clone & setup
git clone https://github.com/badasemrah2/pazarglobal-agent.git
cd pazarglobal-agent
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt

# Environment
cp .env.example .env
# Edit .env with your keys

# Run
python main.py
```

## 🔧 Environment Variables

```env
OPENAI_API_KEY=sk-...
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_KEY=eyJ...
SUPABASE_SERVICE_KEY=eyJ...
REDIS_URL=redis://localhost:6379  # Optional
```

## 📡 API

```bash
# Health
GET /health

# Message
POST /api/v1/message
{
    "user_id": "uuid",
    "message": "iPhone 14 satmak istiyorum",
    "media_urls": [],
    "channel": "webchat"
}
```

## 🧪 Test

```bash
pytest tests/ -v
```

## 📖 Dokümantasyon

- [Mimari](./ARCHITECTURE.md) - Sistem tasarımı
- [Supabase Schema](./docs/SUPABASE_SCHEMA.md) - Veritabanı yapısı

---

*Detaylı mimari için [ARCHITECTURE.md](./ARCHITECTURE.md)*
