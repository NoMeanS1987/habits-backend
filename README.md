# habits-backend

FastAPI бэкенд + aiogram бот для [nomeans1987.github.io/habits](https://nomeans1987.github.io/habits)

## Что делает

- Хранит состояние трекера в PostgreSQL (привычки, задачи, RPG-стата)
- Бот шлёт утром список дел на день (09:00 МСК)
- Бот шлёт вечером что не отметил (21:00 МСК)

## Стек

- **FastAPI** — REST API
- **SQLAlchemy** — ORM
- **PostgreSQL** — база данных
- **aiogram 3** — Telegram бот
- **APScheduler** — планировщик задач

## Запуск локально

```bash
git clone https://github.com/nomeans1987/habits-backend
cd habits-backend

pip install -r requirements.txt

cp .env.example .env
# заполни BOT_TOKEN и DATABASE_URL в .env

# запустить API
uvicorn main:app --reload

# запустить бота (в отдельном терминале)
python bot.py
```
## API

| Метод | Эндпоинт | Описание |
|-------|----------|----------|
| GET | `/health` | Проверка сервера |
| GET | `/api/state/{telegram_id}` | Загрузить состояние |
| POST | `/api/state/{telegram_id}` | Сохранить состояние |

## Подключение к index.html

В `index.html` заменить функции `saveState` и `loadState`:

```js
const API_URL = 'https://your-backend.railway.app'
const TG_ID = window.Telegram?.WebApp?.initDataUnsafe?.user?.id

async function saveState() {
  await fetch(`${API_URL}/api/state/${TG_ID}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(state)
  })
}

async function loadState() {
  const res = await fetch(`${API_URL}/api/state/${TG_ID}`)
  const data = await res.json()
  if (data.state) state = data.state
}
```
