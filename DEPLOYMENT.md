# Deployment Rehberi

Bu doküman, PazarGlobal Agent sistemini Railway'e nasıl deploy edeceğinizi adım adım açıklar.

## 📋 Ön Gereksinimler

1. **Railway Hesabı**: [railway.app](https://railway.app) üzerinden ücretsiz hesap oluşturun
2. **GitHub Repository**: Projenizi GitHub'a push'layın
3. **OpenAI API Key**: [platform.openai.com](https://platform.openai.com/api-keys)
4. **Supabase Projesi**: [supabase.com](https://supabase.com)
5. **Twilio Hesabı** (WhatsApp için): [twilio.com](https://www.twilio.com)

## 🚂 Railway Deployment

### 1. Railway CLI Kurulumu (Opsiyonel)

```bash
npm install -g @railway/cli
railway login
```

### 2. Yeni Proje Oluştur

**Seçenek A: GitHub ile (Önerilen)**

1. [Railway Dashboard](https://railway.app/dashboard) açın
2. "New Project" → "Deploy from GitHub repo" seçin
3. `pazarglobal-agent` repository'sini seçin
4. Railway otomatik olarak `railway.json` ve `Procfile` algılayacak

**Seçenek B: CLI ile**

```bash
cd pazarglobal-agent
railway init
railway up
```

### 3. Redis Servis Ekle

Railway dashboard'da:
1. Projenize tıklayın
2. "+ New" → "Database" → "Add Redis"
3. Redis otomatik olarak oluşturulacak

### 4. Environment Variables Ayarla

Railway dashboard → "Variables" sekmesine gidin ve şunları ekleyin:

#### OpenAI
```
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4-turbo-preview
OPENAI_VISION_MODEL=gpt-4o-mini
```

#### Supabase
```
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_KEY=eyJ...
SUPABASE_SERVICE_KEY=eyJ...
```

#### Redis (Otomatik oluşturuldu)
```
REDIS_URL=${{Redis.REDIS_URL}}
```

#### Twilio (WhatsApp için)
```
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_WHATSAPP_NUMBER=whatsapp:+14155238886
```

#### API Config
```
API_ENV=production
DEBUG=false
LOG_LEVEL=INFO
WEBHOOK_BASE_URL=https://your-app.railway.app
```

### 5. Deploy

Railway otomatik olarak deploy edecek. Logları izleyin:

```bash
railway logs
```

veya Dashboard'da "Deployments" sekmesinden.

### 6. Domain Ayarla

1. Railway dashboard → "Settings" → "Networking"
2. "Generate Domain" butonuna tıklayın
3. Domain'inizi alın (örn: `your-app.railway.app`)

## 🔧 Supabase Konfigürasyonu

### Database Tables

Supabase SQL Editor'da şu tabloları oluşturun:

```sql
-- Active Drafts
CREATE TABLE active_drafts (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  user_id TEXT NOT NULL,
  phone_number TEXT NOT NULL,
  title TEXT,
  description TEXT,
  price_normalized NUMERIC,
  detected_category TEXT,
  status TEXT DEFAULT 'in_progress',
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Listings
CREATE TABLE listings (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  user_id TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT,
  price NUMERIC,
  category TEXT,
  status TEXT DEFAULT 'active',
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Listing Images
CREATE TABLE listing_images (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  listing_id UUID REFERENCES listings(id) ON DELETE CASCADE,
  image_url TEXT NOT NULL,
  metadata JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Wallets
CREATE TABLE wallets (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  user_id TEXT UNIQUE NOT NULL,
  balance NUMERIC DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Transactions
CREATE TABLE transactions (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  user_id TEXT NOT NULL,
  amount NUMERIC NOT NULL,
  type TEXT NOT NULL, -- 'credit' or 'debit'
  description TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Audit Logs
CREATE TABLE audit_logs (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  action TEXT NOT NULL,
  data JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes
CREATE INDEX idx_drafts_user ON active_drafts(user_id);
CREATE INDEX idx_listings_user ON listings(user_id);
CREATE INDEX idx_listings_category ON listings(category);
CREATE INDEX idx_listings_status ON listings(status);
CREATE INDEX idx_wallets_user ON wallets(user_id);
CREATE INDEX idx_transactions_user ON transactions(user_id);
```

### Row Level Security (RLS)

```sql
-- Enable RLS
ALTER TABLE active_drafts ENABLE ROW LEVEL SECURITY;
ALTER TABLE listings ENABLE ROW LEVEL SECURITY;
ALTER TABLE listing_images ENABLE ROW LEVEL SECURITY;
ALTER TABLE wallets ENABLE ROW LEVEL SECURITY;
ALTER TABLE transactions ENABLE ROW LEVEL SECURITY;

-- Service role bypass (backend kullanımı için)
CREATE POLICY "Service role bypass" ON active_drafts FOR ALL USING (true);
CREATE POLICY "Service role bypass" ON listings FOR ALL USING (true);
CREATE POLICY "Service role bypass" ON listing_images FOR ALL USING (true);
CREATE POLICY "Service role bypass" ON wallets FOR ALL USING (true);
CREATE POLICY "Service role bypass" ON transactions FOR ALL USING (true);
```

## 📱 WhatsApp Webhook Ayarları

### Twilio Console

1. [Twilio Console](https://console.twilio.com) → "Messaging" → "Try it out" → "Send a WhatsApp message"
2. "Sandbox Settings" tıklayın
3. "WHEN A MESSAGE COMES IN" webhook URL'sini ayarlayın:
   ```
   https://your-app.railway.app/whatsapp/webhook
   ```
4. HTTP Method: `POST`
5. Save

### Test

WhatsApp'tan Twilio sandbox numarasına mesaj gönderin:
```
join [your-sandbox-code]
```

Sonra test mesajı:
```
Merhaba!
```

## 🌐 Frontend Bağlantısı

Frontend `.env` dosyasını güncelleyin:

```env
VITE_AGENT_API_URL=https://your-app.railway.app
VITE_AGENT_WS_URL=wss://your-app.railway.app
```

Frontend'i redeploy edin (Vercel/Netlify).

## 🔍 Health Check

Deploy sonrası test edin:

```bash
curl https://your-app.railway.app/health
```

Beklenen response:
```json
{
  "status": "healthy",
  "service": "pazarglobal-agent",
  "environment": "production"
}
```

## 📊 Monitoring

### Railway Metrics

Railway dashboard'da:
- CPU usage
- Memory usage
- Network traffic
- Deploy logs

### Custom Logging

Logları görüntüle:
```bash
railway logs --follow
```

veya Dashboard → "Observability" sekmesi

## 🔄 CI/CD

### Automatic Deployments

Railway, GitHub'a her push'ta otomatik deploy eder.

Branch ayarları:
1. Railway dashboard → "Settings" → "Source"
2. "Branch" seçin (main/master)
3. Her commit otomatik deploy olur

### Manual Deployment

```bash
railway up
```

## 🐛 Troubleshooting

### Build Hatası

```bash
# Logs kontrol et
railway logs

# Environment variables kontrol et
railway variables
```

### Connection Errors

1. Redis bağlantısını kontrol et:
   ```bash
   railway run python -c "import redis; r = redis.from_url('$REDIS_URL'); print(r.ping())"
   ```

2. Supabase bağlantısını kontrol et:
   ```bash
   curl https://YOUR_SUPABASE_URL/rest/v1/
   ```

### WhatsApp Webhook Çalışmıyor

1. Twilio webhook URL doğru mu?
2. HTTPS mi? (Railway otomatik HTTPS sağlar)
3. Twilio logs kontrol et: [Twilio Console → Monitor → Logs](https://console.twilio.com/us1/monitor/logs)

## 💰 Maliyet Optimizasyonu

### Railway Free Tier

- $5 ücretsiz kullanım/ay
- 500 saat çalışma süresi
- Kart gerekli ama otomatik charge olmaz

### Resource Limits

```json
// railway.json
{
  "deploy": {
    "restartPolicyType": "ON_FAILURE",
    "restartPolicyMaxRetries": 10
  }
}
```

### Redis Optimizasyonu

Session TTL'yi ayarlayın (daha az memory kullanımı):

```python
# redis_client.py
await client.setex(
    f"session:{session_id}",
    ttl=3600,  # 1 saat (24 saat yerine)
    json.dumps(data)
)
```

## 🔒 Production Güvenlik

### CORS Ayarları

```python
# main.py
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://your-frontend-domain.com",
        "https://www.your-frontend-domain.com"
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)
```

### Rate Limiting

Rate limiting zaten aktif:
- 60 request/dakika
- 1000 request/saat

### API Keys

- OpenAI API key'i asla frontend'e göndermeyin
- Supabase service key'i yalnızca backend'de kullanın
- Environment variables'ı asla commit etmeyin

## 📈 Scaling

### Horizontal Scaling

Railway Pro plan ile:
1. Dashboard → "Settings" → "Deploy"
2. "Replicas" ayarını artırın

### Vertical Scaling

Resource limits artırın:
1. Dashboard → "Settings" → "Resources"
2. CPU/Memory limit'leri ayarlayın

## 🎯 Post-Deployment Checklist

- [ ] Health check başarılı
- [ ] Redis bağlantısı çalışıyor
- [ ] Supabase bağlantısı çalışıyor
- [ ] WhatsApp webhook ayarlandı
- [ ] Frontend bağlantısı çalışıyor
- [ ] WebSocket çalışıyor
- [ ] Test mesajları başarılı
- [ ] Loglar temiz
- [ ] Error tracking aktif
- [ ] Monitoring kurulu

## 📚 Kaynaklar

- [Railway Docs](https://docs.railway.app)
- [FastAPI Deployment](https://fastapi.tiangolo.com/deployment/)
- [Twilio WhatsApp API](https://www.twilio.com/docs/whatsapp)
- [Supabase Docs](https://supabase.com/docs)
