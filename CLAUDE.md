# HabitRPG — Backend

Telegram Mini App трекер привычек с RPG-механикой. Бэкенд на FastAPI + PostgreSQL + aiogram 3.

## Деплой
- **Бэк**: Railway (автодеплой из этого репо, ветка `main`)
- **Фронт**: GitHub Pages — `https://github.com/NoMeanS1987/Habits`, ветка `main`, файл `index.html`
- **Бот**: `@MyAtonHabits_bot`
- **API**: `https://web-production-88b66.up.railway.app`

## Стек
- FastAPI + uvicorn + slowapi (rate limit)
- SQLAlchemy 2 (sync) + psycopg2 + PostgreSQL (Railway Postgres)
- aiogram 3 + APScheduler 3 (MemoryJobStore — не SQLAlchemy, были ConflictingIdError)
- zoneinfo + tzdata для таймзон

## Структура
```
main.py       — FastAPI app, lifespan, /api/state GET+POST, CORS, rate limit
bot.py        — aiogram бот, APScheduler, уведомления, reschedule_user()
auth.py       — HMAC-SHA256 валидация Telegram initData (24ч окно)
models.py     — User(telegram_id, state_json, is_active, tz)
database.py   — SQLAlchemy engine, SessionLocal, get_db()
```

## Ключевые решения

**Auth**: каждый запрос проверяется через `X-Init-Data` header (Telegram WebApp initData).
Без валидного initData → 403.

**Sync**: состояние хранится как JSON в `users.state_json`. GET отдаёт, POST перезаписывает.
После каждого POST в фоне вызывается `reschedule_user(telegram_id)`.

**Уведомления**: APScheduler с MemoryJobStore (не персистентный — был ConflictingIdError с SQLAlchemyJobStore).
- Глобальные: 9:00 и 21:00 МСК для всех активных юзеров
- Персональные: per-habit/todo `reminderTime` поле ("HH:MM"), 48ч горизонт
- При старте: `reschedule_all_active()` пересобирает джобы для всех
- Ночью в 03:00 UTC: `reschedule_all_active()` продлевает горизонт

**Таймзона**: фронт шлёт `tz` (через `Intl.DateTimeFormat().resolvedOptions().timeZone`),
бэк хранит в `users.tz`, использует при планировании уведомлений.

**Weekday mapping**: JS `getDay()` (0=Sun) ≠ Python `weekday()` (0=Mon).
Конвертация: `js_day = (py_weekday + 1) % 7`.

**Миграции**: `_migrate()` в lifespan через `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.
Alembic не настроен — TODO.

## Фронт (отдельный репо)
Один файл `index.html`. Важно: **обязательно** должен быть в `<head>`:
```html
<script src="https://telegram.org/js/telegram-web-app.js"></script>
```
Без этого `TG_ID = null` и весь sync молча отключается (данные в localStorage).

State shape:
```json
{
  "habits": [{"id", "name", "color", "repeat", "days", "intervalDays",
               "createdAt", "completions", "stat", "xp", "sp", "reminderTime"}],
  "todos":  [{"id", "text", "date", "repeat", "done", "reminderTime", ...}],
  "workouts": [...],
  "rpg": {"str", "int", "agi", "hp", "xp"},
  "tz": "Europe/Moscow"
}
```

## Пайплайн (текущее состояние)

### Этап 1 — стабильность (в процессе)
- [x] Уведомления в конкретное время + мультитаймзон
- [ ] Уведомления с интервалом (window-режим: каждые N часов между A и B)
- [ ] Sentry — нет мониторинга ошибок, проблемы узнаём от юзеров
- [ ] Alembic — сейчас миграции хардкодом в lifespan

### Этап 2 — удержание
- [ ] Недельный дайджест (воскресенье)
- [ ] Onboarding для новых юзеров
- [ ] Сортировка привычек и задач

### Этап 3 — рост
- [ ] /stats команда в боте
- [ ] Реферальная механика /invite
- [ ] Публичный профиль

### Этап 4 — полировка
- [ ] Экспорт данных (CSV/JSON)
- [ ] Web Share API для достижений

### Этап 5 — монетизация
- [ ] Telegram Stars / донат
- [ ] Достижения
- [ ] Аналитика

### Технический долг
- [ ] **Observability**: Sentry + smoke-тесты на критичные эндпоинты
  (проблемы должны обнаруживаться до фидбека от юзеров)
- [ ] Тесты: isHabitActive, reschedule_user, HMAC-валидация
- [ ] CI/CD когда появится команда
- [ ] Redis когда появится нагрузка

## Известные баги / история
- SQLAlchemyJobStore → ConflictingIdError при каждом рестарте → заменён на MemoryJobStore
- Telegram WebApp script отсутствовал в локальной копии фронта → sync молча не работал
- isHabitActive использовал UTC даты → баг с галочкой на interval-привычках по ночам
