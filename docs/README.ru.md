# Polymarket Bot MVP

Локальный MVP universal Polymarket paper bot с FastAPI, SQLite, paper execution, webhook-интеграцией и тестами.

## Что уже работает

- `paper_auto` как основной режим запуска
- `shadow` режим без записи paper fills
- `live_auto` заблокирован
- FastAPI API и фоновые задачи в одном процессе
- signal generation, risk manager, paper open/close, PnL
- outbound webhook payloads для n8n / Telegram pipeline
- входящие webhook endpoints для manual approve/reject
- pytest тесты для signals, risk, paper execution и API

## Установка зависимостей

```bash
cd polymarket_bot
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

## Локальный запуск

Один тик paper bot:

```bash
python -m app.main --once
```

Постоянный запуск bot + API:

```bash
python -m app.main
```

Только API:

```bash
python -m app.main --api-only
```

Swagger:

```text
http://127.0.0.1:8000/docs
```

## Запуск через Docker Compose

Подготовка:

```bash
cp .env.example .env
docker compose up --build
```

С PostgreSQL профилем:

```bash
docker compose --profile postgres up --build
```

По умолчанию проект работает на SQLite. PostgreSQL оставлен как опциональный сервис для следующего шага.

## Тесты

Все тесты:

```bash
pytest
```

С подробным выводом:

```bash
pytest -v
```

Выборочно:

```bash
pytest tests/test_signal_engine.py
pytest tests/test_risk_manager.py
pytest tests/test_paper_execution.py
pytest tests/test_api.py
```

## Основные файлы

- [app/main.py](app/main.py) — запуск runtime и FastAPI
- [app/api/main.py](app/api/main.py) — API app
- [app/api/admin.py](app/api/admin.py) — endpoints управления (status, pause, resume, positions, trades)
- [app/api/webhooks.py](app/api/webhooks.py) — webhook endpoints (telegram-command, telegram-callback)
- [app/tasks/scanner.py](app/tasks/scanner.py) — scanner и signal flow
- [app/tasks/trader.py](app/tasks/trader.py) — execution flow
- [app/execution/execution_router.py](app/execution/execution_router.py) — paper routing и risk checks
- [app/execution/position_manager.py](app/execution/position_manager.py) — позиции и PnL
- [app/pricing/signal_engine.py](app/pricing/signal_engine.py) — fair price и signal
- [app/integrations/payloads.py](app/integrations/payloads.py) — webhook payload models
- [app/integrations/n8n.py](app/integrations/n8n.py) — outbound webhook client к n8n

## Важные настройки

`.env.example` уже настроен под безопасный запуск:

```env
APP_MODE=paper_auto
ENABLE_LIVE_TRADING=false
DATABASE_URL=sqlite:///./paper_bot.db
N8N_WEBHOOK_URL=
N8N_WEBHOOK_SECRET=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

## n8n + Telegram Интеграция

Python backend интегрирован с n8n для Telegram управления и уведомлений. Подробнее:

**📖 Документация:** 
- [setup_n8n_telegram.md](./setup_n8n_telegram.md) — полное руководство по настройке
- [n8n_workflow_notes.md](./n8n_workflow_notes.md) — архитектура ONE workflow'а
- Примеры payloads: [signal](./example_signal_payload.json), [trade](./example_paper_trade_open_payload.json), [report](./example_daily_report_payload.json)

**🔄 Поток событий:**

Python → n8n → Telegram (notifications)
Telegram → n8n → Python (commands)

**✅ Implemented:**
- Webhook приём событий от Python в n8n (signal, trade open/close, risk alert, daily report)
- Telegram endpoint приём команд в Python (`/webhook/telegram-command`, `/webhook/telegram-callback`)
- Admin API endpoints для управления: `/admin/status`, `/admin/pause`, `/admin/resume`, `/admin/set-mode`, `/admin/positions`, `/admin/trades`, `/admin/reset-paper-account`
- Payload models для всех типов событий

**⏳ To Do (в n8n UI):**
1. Создать главный workflow `Polymarket Bot Main` с Webhook триггером
2. Добавить Switch по event_type (signal, trade, report, etc.)
3. Для каждого типа события: форматировать и отправлять в Telegram
4. Добавить Chat Trigger для Telegram команд
5. Обработать команды и отправить в Python API
6. Вернуть результаты обратно в Telegram

Шаг за шагом инструкции в [setup_n8n_telegram.md](./setup_n8n_telegram.md).

## Что пока недоделано

- нет настоящей Polymarket auth/exchange integration
- websocket stream пока abstraction + TODO
- manual approve/reject endpoints пока stubbed
- нет Alembic migrations и production Docker image
- Production Telegram webhook URL (используй n8n публичный URL)
