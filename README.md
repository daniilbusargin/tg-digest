# tg-digest

Приватный Telegram-бот для ежедневного дайджеста публичных Telegram-каналов.
Парсит открытые web-превью каналов (`https://t.me/s/{username}`, без авторизации
в Telegram), складывает посты в SQLite и раз в сутки прогоняет их через
LangGraph-пайплайн с локальной LLM (Ollama, `qwen2.5:7b`), отправляя владельцу
структурированный markdown-дайджест.

## Архитектура

```
aiogram (polling)            APScheduler (cron, 09:00 MSK)
        │                              │
        └──────────┬───────────────────┘
                   ▼
       ┌──────────────────────┐
       │  LangGraph StateGraph │
       │                       │
       │  START → fetch ──┐    │
       │            │     │    │
       │   (no new) │     ▼    │
       │           END  summarize ── (Semaphore=2, ChatOllama + PydanticOutputParser)
       │                  │    │
       │                  ▼    │
       │              assemble │
       │                  │    │
       │                  ▼    │
       │                 END   │
       └───────────────────────┘
              │
              ▼
      SqliteSaver (thread_id="daily-digest")
```

- **`db.py`** — SQLAlchemy 2.0 async (aiosqlite). Модели `Channel` и `Post`,
  CRUD-хелперы.
- **`parser.py`** — `aiohttp` + `selectolax` парсер страницы канала t.me/s/.
- **`graph.py`** — `StateGraph` с тремя узлами и условным ребром после `fetch`.
  Состояние — `TypedDict` с `summaries: Annotated[list, operator.add]`.
- **`bot.py`** — aiogram 3 роутер, фильтр по `OWNER_CHAT_ID`, команды
  `/add`, `/remove`, `/list`, `/digest`.
- **`main.py`** — entrypoint: `init_db()`, бот, APScheduler с cron-триггером
  `09:00 Europe/Moscow`.

## Установка (macOS, Python 3.11+)

### 1. Ollama и модель

```bash
brew install ollama
ollama serve            # в отдельном терминале
ollama pull qwen2.5:7b
```

### 2. Зависимости

```bash
git clone https://github.com/daniilbusargin/tg-digest.git
cd tg-digest
python3.11 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

### 3. Конфиг

```bash
cp .env.example .env
```

Заполните:
- `BOT_TOKEN` — токен от [@BotFather](https://t.me/BotFather).
- `OWNER_CHAT_ID` — ваш Telegram chat id (узнать у
  [@userinfobot](https://t.me/userinfobot)). Бот молча игнорирует всех остальных.
- `OLLAMA_MODEL` — по умолчанию `qwen2.5:7b`.
- `SCHEDULE_TZ` / `SCHEDULE_HOUR` / `SCHEDULE_MINUTE` — расписание дайджеста
  (по умолчанию 09:00 Europe/Moscow).

### 4. Запуск

```bash
python main.py
```

В Telegram напишите боту `/start` и добавьте каналы:

```
/add @durov
/add @telegram
/list
/digest          # запустить сборку прямо сейчас
/remove @durov
```

Каждый день в `SCHEDULE_HOUR:SCHEDULE_MINUTE` бот пришлёт дайджест автоматически.

## Команды

| Команда            | Описание                                                |
|--------------------|---------------------------------------------------------|
| `/add @channel`    | Добавить публичный канал в подписки                     |
| `/remove @channel` | Удалить канал                                           |
| `/list`            | Показать все каналы и их `last_seen_msg_id`             |
| `/digest`          | Собрать и прислать дайджест немедленно                  |

## Замечания

- Парсер ходит только на публичные web-страницы `t.me/s/{username}`,
  никаких аккаунтов и MTProto.
- Между запросами к каналам выдерживается пауза 1.5 секунды.
- LLM-вызовы в `summarize_node` идут параллельно, ограничены
  `asyncio.Semaphore(2)` — чтобы не задавить локальную Ollama.
- Состояние графа сохраняется в `langgraph_checkpoints.db`
  (`SqliteSaver`, `thread_id="daily-digest"`), посты — в `digest.db`.
- При первом добавлении канала `last_seen_msg_id = 0`, поэтому первый
  дайджест втянет всё, что отдаст t.me/s/ (последние ~20 постов).

## Файлы

```
tg-digest/
├── bot.py              # aiogram-роутер
├── db.py               # SQLAlchemy модели + хелперы
├── graph.py            # LangGraph StateGraph
├── main.py             # entrypoint + APScheduler
├── parser.py           # t.me/s/ парсер
├── requirements.txt
├── .env.example
└── .gitignore
```
