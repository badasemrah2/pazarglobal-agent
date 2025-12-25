# Frontend Entegrasyon Rehberi

Bu doküman, `pazarglobal-frontend` projesinin `pazarglobal-agent` API'sine nasıl bağlanacağını açıklar.

## 🔌 Bağlantı Kurulumu

### 1. Environment Variables (Frontend)

Frontend projesinde `.env` dosyasına ekleyin:

```env
VITE_AGENT_API_URL=http://localhost:8000
VITE_AGENT_WS_URL=ws://localhost:8000
```

Production için:
```env
VITE_AGENT_API_URL=https://your-app.railway.app
VITE_AGENT_WS_URL=wss://your-app.railway.app
```

### 2. Agent API Service

Frontend'de yeni bir servis dosyası oluşturun:

```typescript
// src/services/agent-api.ts
const AGENT_API_URL = import.meta.env.VITE_AGENT_API_URL || 'http://localhost:8000';
const AGENT_WS_URL = import.meta.env.VITE_AGENT_WS_URL || 'ws://localhost:8000';

export interface ChatMessage {
  session_id: string;
  message: string;
  user_id?: string;
  media_url?: string;
}

export interface ChatResponse {
  success: boolean;
  message: string;
  data?: {
    intent?: string;
    draft_id?: string;
    draft?: any;
    listings?: any[];
    type?: string;
  };
  intent?: string;
}

// Session Management
export async function createSession(userId?: string): Promise<{ session_id: string }> {
  const response = await fetch(`${AGENT_API_URL}/webchat/session/new`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_id: userId })
  });
  return response.json();
}

export async function getSession(sessionId: string) {
  const response = await fetch(`${AGENT_API_URL}/webchat/session/${sessionId}`);
  return response.json();
}

export async function deleteSession(sessionId: string) {
  const response = await fetch(`${AGENT_API_URL}/webchat/session/${sessionId}`, {
    method: 'DELETE'
  });
  return response.json();
}

// Messaging (REST)
export async function sendMessage(chatMessage: ChatMessage): Promise<ChatResponse> {
  const response = await fetch(`${AGENT_API_URL}/webchat/message`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(chatMessage)
  });
  return response.json();
}

export async function getChatHistory(sessionId: string, limit = 20) {
  const response = await fetch(
    `${AGENT_API_URL}/webchat/history/${sessionId}?limit=${limit}`
  );
  return response.json();
}

// WebSocket Connection
export class AgentWebSocket {
  private ws: WebSocket | null = null;
  private sessionId: string;
  private onMessageCallback?: (data: any) => void;
  private onErrorCallback?: (error: any) => void;

  constructor(sessionId: string) {
    this.sessionId = sessionId;
  }

  connect(
    onMessage: (data: any) => void,
    onError?: (error: any) => void
  ) {
    this.onMessageCallback = onMessage;
    this.onErrorCallback = onError;

    this.ws = new WebSocket(`${AGENT_WS_URL}/webchat/ws/${this.sessionId}`);

    this.ws.onopen = () => {
      console.log('WebSocket connected');
    };

    this.ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (this.onMessageCallback) {
        this.onMessageCallback(data);
      }
    };

    this.ws.onerror = (error) => {
      console.error('WebSocket error:', error);
      if (this.onErrorCallback) {
        this.onErrorCallback(error);
      }
    };

    this.ws.onclose = () => {
      console.log('WebSocket disconnected');
    };
  }

  send(message: string, userId?: string) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ message, user_id: userId }));
    }
  }

  close() {
    if (this.ws) {
      this.ws.close();
    }
  }
}
```

### 3. ChatBox Component Güncellemesi

Mevcut `ChatBox.tsx` dosyanızı güncelleyin:

```typescript
// src/components/feature/ChatBox.tsx
import { useState, useEffect, useRef } from 'react';
import { sendMessage, createSession, AgentWebSocket, ChatResponse } from '../../services/agent-api';
import './ChatBox.css';

interface Message {
  role: 'user' | 'assistant';
  content: string;
  timestamp: Date;
  data?: any;
}

export function ChatBox() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [sessionId, setSessionId] = useState<string>('');
  const [isLoading, setIsLoading] = useState(false);
  const [useWebSocket, setUseWebSocket] = useState(false);
  const wsRef = useRef<AgentWebSocket | null>(null);

  // Session oluştur
  useEffect(() => {
    const initSession = async () => {
      try {
        const { session_id } = await createSession();
        setSessionId(session_id);
        
        // WebSocket kullanımı için
        if (useWebSocket) {
          wsRef.current = new AgentWebSocket(session_id);
          wsRef.current.connect(handleWebSocketMessage);
        }
      } catch (error) {
        console.error('Session oluşturulamadı:', error);
      }
    };

    initSession();

    return () => {
      if (wsRef.current) {
        wsRef.current.close();
      }
    };
  }, [useWebSocket]);

  const handleWebSocketMessage = (data: any) => {
    if (data.type === 'message') {
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: data.message,
        timestamp: new Date(),
        data: data.data
      }]);
      setIsLoading(false);
    }
  };

  const handleSendMessage = async () => {
    if (!input.trim() || !sessionId) return;

    const userMessage: Message = {
      role: 'user',
      content: input,
      timestamp: new Date()
    };

    setMessages(prev => [...prev, userMessage]);
    setInput('');
    setIsLoading(true);

    try {
      if (useWebSocket && wsRef.current) {
        // WebSocket ile gönder
        wsRef.current.send(input);
      } else {
        // REST API ile gönder
        const response: ChatResponse = await sendMessage({
          session_id: sessionId,
          message: input
        });

        const assistantMessage: Message = {
          role: 'assistant',
          content: response.message,
          timestamp: new Date(),
          data: response.data
        };

        setMessages(prev => [...prev, assistantMessage]);
      }
    } catch (error) {
      console.error('Mesaj gönderilemedi:', error);
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: 'Üzgünüm, bir hata oluştu. Lütfen tekrar deneyin.',
        timestamp: new Date()
      }]);
    } finally {
      if (!useWebSocket) {
        setIsLoading(false);
      }
    }
  };

  return (
    <div className="chatbox">
      <div className="chatbox-header">
        <h3>PazarGlobal AI Asistan</h3>
        <div className="connection-toggle">
          <label>
            <input
              type="checkbox"
              checked={useWebSocket}
              onChange={(e) => setUseWebSocket(e.target.checked)}
            />
            WebSocket
          </label>
        </div>
      </div>

      <div className="chatbox-messages">
        {messages.map((msg, idx) => (
          <div key={idx} className={`message message-${msg.role}`}>
            <div className="message-content">{msg.content}</div>
            {msg.data?.draft && (
              <div className="message-data">
                <h4>İlan Taslağı:</h4>
                <p><strong>Başlık:</strong> {msg.data.draft.title}</p>
                <p><strong>Fiyat:</strong> {msg.data.draft.price_normalized} TL</p>
              </div>
            )}
            {msg.data?.listings && msg.data.listings.length > 0 && (
              <div className="message-data">
                <h4>{msg.data.count} İlan Bulundu:</h4>
                {msg.data.listings.slice(0, 3).map((listing: any, i: number) => (
                  <div key={i} className="listing-preview">
                    <p><strong>{listing.title}</strong></p>
                    <p>{listing.price} TL</p>
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
        {isLoading && (
          <div className="message message-assistant">
            <div className="message-content typing">Yazıyor...</div>
          </div>
        )}
      </div>

      <div className="chatbox-input">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyPress={(e) => e.key === 'Enter' && handleSendMessage()}
          placeholder="Mesajınızı yazın..."
          disabled={isLoading}
        />
        <button onClick={handleSendMessage} disabled={isLoading}>
          Gönder
        </button>
      </div>
    </div>
  );
}
```

### 4. SiteAI Component Güncellemesi

Mevcut AI özelliklerini entegre edin:

```typescript
// src/pages/home/components/SiteAI.tsx
import { useState } from 'react';
import { sendMessage, createSession } from '../../../services/agent-api';

export function SiteAI() {
  const [query, setQuery] = useState('');
  const [response, setResponse] = useState('');
  const [loading, setLoading] = useState(false);

  const handleAIQuery = async () => {
    if (!query.trim()) return;

    setLoading(true);
    try {
      // Session oluştur
      const { session_id } = await createSession();
      
      // Mesaj gönder
      const result = await sendMessage({
        session_id,
        message: query
      });

      setResponse(result.message);
    } catch (error) {
      setResponse('Bir hata oluştu. Lütfen tekrar deneyin.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <section className="site-ai">
      <h2>AI Asistan ile İlan Oluştur</h2>
      <div className="ai-input">
        <textarea
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Örn: iPhone 13 Pro 256GB sıfır kutusunda satmak istiyorum"
          rows={4}
        />
        <button onClick={handleAIQuery} disabled={loading}>
          {loading ? 'İşleniyor...' : 'AI ile Oluştur'}
        </button>
      </div>
      {response && (
        <div className="ai-response">
          <h3>Yanıt:</h3>
          <p>{response}</p>
        </div>
      )}
    </section>
  );
}
```

## 🎨 CSS Güncellemeleri

```css
/* ChatBox.css - Ek stiller */
.message-data {
  margin-top: 10px;
  padding: 10px;
  background: #f5f5f5;
  border-radius: 4px;
}

.message-data h4 {
  margin: 0 0 8px 0;
  font-size: 14px;
  color: #333;
}

.listing-preview {
  padding: 8px;
  margin: 4px 0;
  background: white;
  border-radius: 4px;
  border-left: 3px solid #4CAF50;
}

.connection-toggle {
  font-size: 12px;
}

.connection-toggle label {
  display: flex;
  align-items: center;
  gap: 5px;
  cursor: pointer;
}
```

## 🧪 Test Senaryoları

### 1. İlan Oluşturma Testi

```typescript
// Test mesajı
const message = "iPhone 13 Pro 256GB satmak istiyorum, fiyat 25000 TL";

// Beklenen response
{
  success: true,
  message: "✅ Draft updated successfully!",
  data: {
    intent: "create_listing",
    draft_id: "uuid",
    draft: {
      title: "iPhone 13 Pro 256GB",
      price_normalized: 25000,
      ...
    }
  }
}
```

### 2. Arama Testi

```typescript
// Test mesajı
const message = "20000 TL altında iPhone ara";

// Beklenen response
{
  success: true,
  message: "Search completed",
  data: {
    intent: "search_listings",
    listings: [...],
    count: 5
  }
}
```

## 🚀 Deployment

### Frontend (Vercel/Netlify)

Environment variables ekleyin:
```
VITE_AGENT_API_URL=https://your-agent-api.railway.app
VITE_AGENT_WS_URL=wss://your-agent-api.railway.app
```

### Backend (Railway)

Zaten yapılandırıldı. Deploy için:
```bash
cd pazarglobal-agent
railway up
```

## 🔒 Güvenlik Notları

1. **CORS**: Production'da CORS ayarlarını frontend domain'inize göre ayarlayın
2. **API Keys**: Backend API keys'lerini asla frontend'e göndermeyin
3. **Rate Limiting**: Agent API'de rate limiting aktif
4. **Session Management**: Session'lar 24 saat sonra otomatik silinir

## 📱 Responsive Design

ChatBox componentinin mobil uyumlu olduğundan emin olun:

```css
@media (max-width: 768px) {
  .chatbox {
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    height: 60vh;
  }
}
```

## 🐛 Debug

Browser console'da bağlantı durumunu kontrol edin:

```javascript
// Session kontrolü
console.log('Session ID:', sessionId);

// WebSocket durumu
console.log('WS State:', ws.readyState);
// 0: CONNECTING, 1: OPEN, 2: CLOSING, 3: CLOSED

// API response
console.log('Response:', response);
```

## 📚 Daha Fazla

- [Agent API Documentation](http://localhost:8000/docs)
- [Architecture README](../pazar_global_agent_architecture_readme%20(1).md)
- [OpenAI Function Calling](https://platform.openai.com/docs/guides/function-calling)
