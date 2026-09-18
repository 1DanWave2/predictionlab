# Setup: n8n + Telegram + Polymarket Bot

Workflow уже создан в твоём n8n.
- **ID:** `<workflow-id>`
- **Имя:** `Polymarket Bot Main`
- **Webhook URL (prod):** `https://YOUR-N8N-HOST/webhook/polymarket-events`
- **Webhook URL (test):** `https://YOUR-N8N-HOST/webhook-test/polymarket-events`

---

## ✅ Что мне нужно от тебя, чтобы всё заработало

### 1. Telegram Bot Token (от BotFather)

Формат: `1234567890:ABCdefGHIJKlmnoPQRstuvWXYZabcDEfg`. Присылай строкой.

### 2. Telegram Chat ID

- **Личный чат:** напиши боту `/start`, открой `https://api.telegram.org/bot<TOKEN>/getUpdates`, возьми `chat.id` (положительное число).
- **Группа:** добавь бота в группу → сделай админом → напиши сообщение → в `getUpdates` возьми `chat.id` (вида `-100...`).

### 3. Секрет для вебхука

Сгенерю сам или пришли свой:

```bash
openssl rand -hex 32
```

### 4. Где крутится Python backend?

- **На той же машине, где n8n (Docker)?** → оставляю `http://host.docker.internal:8000`.
- **На другом хосте / публичном URL?** → скажи URL, подставлю в ноду `Forward to Python`.

---

## 🔧 Что делаешь ты руками в n8n UI

### А. Создать Telegram credential
1. n8n → **Credentials** → **New** → **Telegram API**.
2. Вставь `TELEGRAM_BOT_TOKEN`.
3. Сохрани.

### Б. Привязать credential к трём telegram-нодам
Открой `Polymarket Bot Main` и в нодах:
- `Send to Telegram`
- `Reply to Telegram`
- `Telegram Trigger`

выбери твой Telegram credential (сейчас там placeholder `REPLACE_WITH_TELEGRAM_CRED_ID`).

### В. Env-переменные на сервере n8n
В `.env` сервиса n8n:

```env
N8N_WEBHOOK_SECRET=<тот же секрет, что в Python .env>
TELEGRAM_CHAT_ID=<chat id>
```

Перезапусти n8n: `docker compose restart n8n`.

### Г. Активировать workflow
Тумблер **Active** справа сверху.

---

## 🐍 Python backend .env

```env
APP_MODE=paper_auto
ENABLE_LIVE_TRADING=false
DATABASE_URL=sqlite:///./paper_bot.db

N8N_WEBHOOK_URL=https://YOUR-N8N-HOST/webhook/polymarket-events
N8N_WEBHOOK_SECRET=<тот же секрет>

TELEGRAM_BOT_TOKEN=<от BotFather>
TELEGRAM_CHAT_ID=<chat id>
```

Запуск:

```bash
cd polymarket_bot
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app.main
```

Swagger: `http://127.0.0.1:8000/docs`.

---

## 🧪 Проверка end-to-end

### Тест 1 — webhook от Python → Telegram

```bash
curl -X POST https://YOUR-N8N-HOST/webhook/polymarket-events \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Token: <secret>" \
  -d @example_signal_payload.json
```

Ожидается: сообщение `📊 *Signal* …` в Telegram.

### Тест 2 — команда Telegram → Python
В боте: `/status` → ответ `✅ /status` с режимом и состоянием.

### Тест 3 — смена режима
`/set_mode shadow` → `/status` → `/set_mode paper_auto`.

### Тест 4 — одно сканирование
```bash
python -m app.main --once
```

---

## 📋 Поддерживаемые команды и callback actions

| Telegram           | HTTP → Python                  | Что делает                |
|--------------------|--------------------------------|---------------------------|
| `/status`          | POST /webhook/telegram-command | Runtime-снапшот           |
| `/pause`           | POST /webhook/telegram-command | Пауза трейдинга           |
| `/resume`          | POST /webhook/telegram-command | Снять паузу               |
| `/set_mode shadow` / `paper_auto` | POST /webhook/telegram-command | Переключить режим |
| `/reset_paper_account` | POST /webhook/telegram-command | Очистить paper-данные |
| `/positions`       | POST /webhook/telegram-command | Открытые позиции          |
| `/trades`          | POST /webhook/telegram-command | Последние 20 ордеров      |

Callback buttons: `PAUSE` / `RESUME` / `STATUS` / `POSITIONS` / `TRADES` / `RESET_PAPER_ACCOUNT`
— маппятся на те же команды через `/webhook/telegram-callback`.

---

## 🚨 Частые ошибки

| Симптом                        | Причина                                 | Фикс                                      |
|--------------------------------|-----------------------------------------|-------------------------------------------|
| `Invalid webhook token`        | `N8N_WEBHOOK_SECRET` разный             | Синхронизируй .env                        |
| `ECONNREFUSED` в Forward       | n8n в докере не видит localhost:8000    | `host.docker.internal` или IP хоста       |
| Нет сообщений в Telegram       | Credential не привязан / не тот токен   | Проверь пп. А+Б                           |
| Бот не видит команды           | Workflow не активен / бот не в чате     | Включи Active, добавь бота                |

---

## 🗂️ Архитектура ONE workflow

```
     Python → POST /webhook/polymarket-events
                           │
                  [Webhook - Events]
                           │
                  [Verify Token & Format]   ← проверка токена + формат по event_type
                           │
                  [Send to Telegram]
                           │
                    [Respond OK]

     Telegram user → /status, /pause, …
                           │
                  [Telegram Trigger]
                           │
                  [Parse Command]            ← /cmd или callback_query → payload
                           │
                  [Forward to Python]        ← POST :8000/webhook/telegram-{command|callback}
                           │
                  [Format Python Response]
                           │
                  [Reply to Telegram]
```

Retry: 3 попытки с задержкой 2с (нода `Forward to Python`).
Timeout: 30с.
Secret: `X-Webhook-Token` в обе стороны.
