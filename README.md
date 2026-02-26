# PazarGlobal Agent API

PazarGlobal'in AI backend servisidir. WhatsApp bridge ve web frontend trafiğini yönetir; ilan oluşturma,
arama, yayınlama ve moderasyon akışlarını yürütür.

## ✅ Production Readiness (Şu Anki Durum)

- ✅ Canlı endpoint aktif (Railway)
- ✅ Admin health endpoint + JWT yetkilendirme
- ✅ Vision safety gate fail-closed davranışı
- ✅ Illegal report akışı ve admin moderasyon araçları
- ✅ Redis + Supabase bağlantı kontrolleri
- ⚠️ Yük/perf testleri ve otomatik canary rollout henüz sınırlı

> Özet: Çekirdek fonksiyonlar production'da çalışır durumda, ancak ölçekleme ve operasyonel sertleştirme
> adımlarıyla daha da güçlendirilmeli.

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

## 📡 Temel Endpointler

```bash
# Agent run (bridge üzerinden)
POST /agent/run

# Admin health
GET /api/admin/health

# Admin redis clear
POST /api/admin/redis/clear
```

## 🧪 Test

```bash
pytest tests/ -v
```

## 🚀 Go-Live Checklist

- [ ] Railway env değişkenleri güncel (`OPENAI_*`, `SUPABASE_*`, `REDIS_URL`)
- [ ] `CORS_ALLOWED_ORIGINS` production origin'leri ile sınırlandırıldı
- [ ] `/api/admin/health` canlı doğrulandı
- [ ] Vision safety + illegal report akışı smoke test edildi
- [ ] Redis erişimi ve Supabase latency gözlemlendi

## 🗺️ Gelecek Özellikler

- Concurrency-safe Redis session update (Lua/WATCH-MULTI)
- Daha detaylı moderasyon telemetry dashboard'u
- Safety flag review otomasyonları
- Rate-limit ve abuse tespiti için gelişmiş kurallar
- E2E test + load test pipeline

## 📖 Dokümantasyon

- [Mimari](./ARCHITECTURE.md) - Sistem tasarımı
- [Supabase Schema](./docs/SUPABASE_SCHEMA.md) - Veritabanı yapısı

---

*Detaylı mimari için [ARCHITECTURE.md](./ARCHITECTURE.md)*
