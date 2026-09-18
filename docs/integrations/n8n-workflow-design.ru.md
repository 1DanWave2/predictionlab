# n8n Workflow Design — Polymarket Bot Integration

## 📋 Обзор

Это ОДИН главный workflow `Polymarket Bot Main`, который:
1. **Приёмник:** Получает вебхук события от Python backend
2. **Роутер:** Разветвляется по типу события (signal, trade, report, etc.)
3. **Форматер:** Превращает события в понятные Telegram сообщения
4. **Отправитель:** Отправляет в Telegram
5. **Слушатель:** Получает команды из Telegram и callback actions
6. **Отправитель обратно:** Пересылает команды в Python backend
7. **Замыкатель:** Отправляет результат обратно в Telegram

---

## 🏗️ Структура workflow (шаг за шагом)

### Часть 1: Получение событий от Python

#### Node 1: Webhook (Trigger)
```
Name: "Webhook - Receive Events from Python"
Type: Webhook
Configuration:
  - HTTP Method: POST
  - Path: leave empty (auto-generates UUID)
  - Authentication: None (we'll check token manually)
  - Response Mode: "Respond Immediately"
```

**Output:**
```json
{
  "event_type": "signal_event",
  "market_id": "...",
  "source": "polymarket_bot",
  "emitted_at": "...",
  ...
}
```

---

#### Node 2: Check Webhook Secret
```
Name: "Verify Token"
Type: Code (JavaScript)
Code:
```javascript
const token = $request.headers['x-webhook-token'];
const expectedToken = $env.N8N_WEBHOOK_SECRET;

if (token !== expectedToken) {
  return [{ json: { error: "Invalid webhook token" }, pairedItem: 0 }];
}

return [{ json: { valid: true }, pairedItem: 0 }];
```

---

#### Node 3: Switch by Event Type
```
Name: "Router - Event Type"
Type: Switch
Condition:
  - Check value: {{ $json.event_type }}
  - Default: Log unexpected
Cases:
  1. signal_event
  2. paper_trade_open_event
  3. paper_trade_close_event
  4. risk_alert_event
  5. daily_report_event
```

---

### Часть 2: Обработка каждого типа события

#### Case: signal_event

**Node: Format Signal Message**
```
Type: Set
Field: body.text
Value: 
```
📊 **Signal Detected**
Market: {{ $json.slug }}
Category: {{ $json.category }}
Strategy: {{ $json.strategy }}

💰 **Pricing**
Fair: {{ $json.fair_price }} | Current: {{ $json.price }}
Bid/Ask: {{ $json.bid }}/{{ $json.ask }} (spread: {{ $json.spread }})

📈 **Decision**
Side: {{ $json.side }} | Edge: {{ Math.round($json.edge * 100) }}%
Confidence: {{ Math.round($json.confidence * 100) }}%
Volume: {{ $json.volume }}

💭 Reason: {{ $json.reason }}
```

**Node: Send Signal to Telegram**
```
Type: Telegram
Resource: Message
Operation: Send
Chat ID: {{ $env.TELEGRAM_CHAT_ID }}
Text: {{ $node["Format Signal Message"].json.body.text }}
Reply Markup: Keyboard (inline buttons)
  - Button 1: [Status] → action: STATUS
  - Button 2: [Positions] → action: POSITIONS
```

---

#### Case: paper_trade_open_event

**Node: Format Trade Open Message**
```
Type: Set
Field: body.text
Value:
```
✅ **Position Opened**
Order ID: {{ $json.order_id }}
Market: {{ $json.market_id }}
Side: {{ $json.side }}
Strategy: {{ $json.strategy }}

💵 **Execution**
Price: {{ $json.price }} | Size: {{ $json.size }}
Quantity: {{ $json.quantity }}
Avg Price: {{ $json.avg_price }}

📊 **PnL**
Realized: {{ $json.realized_pnl }}
Unrealized: {{ $json.unrealized_pnl }}

📝 {{ $json.note }}
```

**Node: Send Trade Open to Telegram**
```
Type: Telegram
Resource: Message
Operation: Send
Chat ID: {{ $env.TELEGRAM_CHAT_ID }}
Text: {{ $node["Format Trade Open Message"].json.body.text }}
Reply Markup: Simple keyboard
  - [Show Positions]
  - [Show All Trades]
```

---

#### Case: paper_trade_close_event

**Node: Format Trade Close Message**
```
Type: Set
Field: body.text
Value:
```
🔴 **Position Closed**
Order ID: {{ $json.order_id }}
Market: {{ $json.market_id }}
Side: {{ $json.side }}
Strategy: {{ $json.strategy }}

💵 **Execution**
Price: {{ $json.price }} | Size: {{ $json.size }}
Closed Qty: {{ $json.closed_quantity }}

📊 **PnL**
Realized Delta: {{ $json.realized_pnl_delta }}
Realized Total: {{ $json.realized_pnl_total }}
Unrealized Remaining: {{ $json.unrealized_pnl }}

📝 {{ $json.note }}
```

**Node: Send Trade Close to Telegram**
```
Type: Telegram
Resource: Message
Operation: Send
Chat ID: {{ $env.TELEGRAM_CHAT_ID }}
Text: {{ $node["Format Trade Close Message"].json.body.text }}
```

---

#### Case: risk_alert_event

**Node: Format Risk Alert Message**
```
Type: Set
Field: body.text
Value:
```
⚠️ **Risk Alert** ({{ $json.severity }})
Market: {{ $json.market_id }}
Side: {{ $json.side || "N/A" }}
Strategy: {{ $json.strategy || "N/A" }}

❌ Reason: {{ $json.reason }}

Order Details: {{ JSON.stringify($json.order) }}
```

**Node: Send Risk Alert to Telegram**
```
Type: Telegram
Resource: Message
Operation: Send
Chat ID: {{ $env.TELEGRAM_CHAT_ID }}
Text: {{ $node["Format Risk Alert Message"].json.body.text }}
Reply Markup: Keyboard
  - [Check Status] → action: STATUS
  - [Pause Bot] → action: PAUSE
```

---

#### Case: daily_report_event

**Node: Format Daily Report Message**
```
Type: Set
Field: body.text
Value:
```
📅 **Daily Report** – {{ $json.report_date }}
Status: {{ $json.paused ? "⏸️ PAUSED" : "🟢 RUNNING" }}

📊 **Statistics**
Total Trades: {{ $json.total_trades }}
Open Positions: {{ $json.open_positions }}
Closed Positions: {{ $json.closed_positions }}

💰 **PnL**
Realized: +{{ $json.total_realized_pnl }}
Unrealized: +{{ $json.total_unrealized_pnl }}
Total: +{{ $json.total_realized_pnl + $json.total_unrealized_pnl }}

🎯 **Win Rate**
Winning: {{ $json.winning_positions }} | Losing: {{ $json.losing_positions }}
Win Ratio: {{ Math.round(100 * $json.winning_positions / ($json.winning_positions + $json.losing_positions)) }}%

📈 Gross Exposure: {{ $json.gross_exposure }}
```

**Node: Send Daily Report to Telegram**
```
Type: Telegram
Resource: Message
Operation: Send
Chat ID: {{ $env.TELEGRAM_CHAT_ID }}
Text: {{ $node["Format Daily Report Message"].json.body.text }}
Reply Markup: Full keyboard
  - [Status] → action: STATUS
  - [Positions] → action: POSITIONS
  - [Trades] → action: TRADES
  - [Resume/Pause] → action: RESUME / PAUSE
  - [Reset] → action: RESET_PAPER_ACCOUNT
```

---

### Часть 3: Получение команд из Telegram

#### Node: Telegram Trigger (Chat Trigger)
```
Name: "Chat - Listen Telegram Commands"
Type: Chat Trigger (or Telegram Trigger depending on n8n version)
Configuration:
  - Chat System: Telegram
  - Bot Token: {{ $env.TELEGRAM_BOT_TOKEN }}
  - Listen to: Messages and Button Clicks
  - Auto-respond: No
```

**Output для текстовой команды:**
```json
{
  "message": "/status",
  "from": { "id": 123456789 },
  "chat": { "id": -1001234567890 }
}
```

**Output для callback button:**
```json
{
  "callback_query": {
    "id": "...",
    "data": "STATUS",
    "from": { "id": 123456789 }
  },
  "message": { "chat": { "id": -1001234567890 } }
}
```

---

#### Node: Parse Command Type
```
Name: "Detect - Command or Callback"
Type: Code (JavaScript)
Code:
```javascript
let command = null;
let isCallback = false;
const userId = $json.from?.id || $json.callback_query?.from?.id;
const chatId = $json.chat?.id || $json.message?.chat?.id;

if ($json.message?.text) {
  // Text command like "/status"
  command = $json.message.text.split(' ')[0]; // extract /command
  isCallback = false;
} else if ($json.callback_query?.data) {
  // Callback button action like "STATUS"
  command = "/" + $json.callback_query.data.toLowerCase();
  isCallback = true;
}

if (!command) {
  return [{ json: { error: "Unknown command" } }];
}

return [{
  json: {
    command,
    isCallback,
    userId: userId.toString(),
    chatId: chatId.toString(),
    args: {}
  }
}];
```

---

#### Node: Extract Args (if set_mode)
```
Name: "Extract - Mode Argument"
Type: Code (JavaScript)
Code:
```javascript
const command = $json.command;
const text = $inputData[0].json.message?.text || "";

if (command === "/set_mode") {
  const parts = text.split(' ');
  if (parts.length > 1) {
    return [{
      json: {
        ...JSON.parse(JSON.stringify($json)),
        args: { mode: parts[1] }
      }
    }];
  }
}

return [$json];
```

---

#### Node: Send Command to Python
```
Name: "HTTP - Forward Command to Python"
Type: HTTP Request
Configuration:
  - Method: POST
  - URL: http://localhost:8000/webhook/telegram-command
         OR https://yourdomain.com/webhook/telegram-command (if remote)
  - Authentication: None (token in headers)
  - Headers:
    - Content-Type: application/json
    - X-Webhook-Token: {{ $env.N8N_WEBHOOK_SECRET }}
  - Body (JSON):
```json
{
  "command": "{{ $json.command }}",
  "args": {{ JSON.stringify($json.args) }},
  "user_id": "{{ $json.userId }}",
  "chat_id": "{{ $json.chatId }}"
}
```
  - Timeout: 30 seconds
  - Continue on Fail: true (чтобы можно было обработать ошибку)
```

---

#### Node: Format Python Response
```
Name: "Set - Response Message"
Type: Set
Fields:
  - body.success: {{ $json.success }}
  - body.command: {{ $json.command }}
  - body.data: {{ JSON.stringify($json.data, null, 2) }}
  - body.error: {{ $json.error || null }}
```

---

#### Node: Send Response Back to Telegram
```
Name: "Telegram - Send Response"
Type: Telegram
Resource: Message
Operation: Send
Chat ID: {{ $inputData[0].json.chatId }}
Text (JavaScript):
```javascript
const data = $json.body;

if (!data.success) {
  return `❌ Command failed: ${data.command}\n\nError: ${data.error}`;
}

let text = `✅ Command: ${data.command}\n\n`;

// Customize response based on command
if (data.command === "/status") {
  const status = data.data;
  text += `Mode: ${status.mode}\n`;
  text += `Paused: ${status.paused}\n`;
  text += `Open Positions: ${status.open_positions || 0}\n`;
  text += `Last Tick: ${status.last_tick_at || "Never"}\n`;
  text += `Last Error: ${status.last_error || "None"}`;
} else if (data.command === "/positions") {
  text += `Fetching positions from /admin/positions...`;
} else if (data.command === "/trades") {
  text += `Fetching trades from /admin/trades...`;
} else {
  text += JSON.stringify(data.data, null, 2);
}

return text;
```
```

---

## 🔗 Полный граф connections

```
Webhook (Receive Events from Python)
    ↓
Verify Token
    ↓
Router - Event Type
    ├─ signal_event
    │    ├─ Format Signal Message
    │    └─ Send Signal to Telegram
    │
    ├─ paper_trade_open_event
    │    ├─ Format Trade Open Message
    │    └─ Send Trade Open to Telegram
    │
    ├─ paper_trade_close_event
    │    ├─ Format Trade Close Message
    │    └─ Send Trade Close to Telegram
    │
    ├─ risk_alert_event
    │    ├─ Format Risk Alert Message
    │    └─ Send Risk Alert to Telegram
    │
    └─ daily_report_event
         ├─ Format Daily Report Message
         └─ Send Daily Report to Telegram

Chat - Listen Telegram Commands (PARALLEL TRIGGER)
    ↓
Detect - Command or Callback
    ↓
Extract - Mode Argument
    ↓
HTTP - Forward Command to Python
    ↓
Format Python Response
    ↓
Telegram - Send Response
```

---

## 🚀 Как создать в n8n UI

1. **Create new workflow** → Polymarket Bot Main

2. **Drag Webhook node** → configurate as "Webhook - Receive Events from Python"

3. **Drag Code node** → add token verification

4. **Drag Switch node** → create 5 cases

5. **Для каждого case:**
   - Drag Set node (format message)
   - Drag Telegram node (send message)

6. **Drag Chat Trigger node** → parallel to Webhook

7. **Drag Code node** → parse command type

8. **Drag Code node** → extract args

9. **Drag HTTP Request node** → forward to Python

10. **Drag Set node** → format response

11. **Drag Telegram node** → send back

12. **Test** с помощью Webhook URL и Telegram commands

---

## 🧪 Тестирование workflow

### Test 1: Signal Event
```bash
curl -X POST https://YOUR-N8N-HOST/webhook/abc123 \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Token: your-secret" \
  -d @example_signal_payload.json
```

Check: Сообщение должно прийти в Telegram

### Test 2: Telegram Command
Отправь боту в Telegram: `/status`

Check: Bot должен ответить со статусом

### Test 3: Callback Button
Жми кнопку в Telegram

Check: Bot должен выполнить action

---

## 🎯 Результат

✅ **Закрыт вебхук от Python** (эмит событий)
✅ **Отформатированы сообщения** (readable в Telegram)
✅ **Telegram отправляет команды** (обратно в Python)
✅ **Python обрабатывает** и возвращает ответы
✅ **Telegram показывает результаты** (пользователю)

Всё работает в ONE workflow, все события оркеструются в n8n, Python остаётся источником истины для торговой логики.

