import asyncio
import json
import logging
import os
from datetime import date

from aiogram import Bot, Dispatcher, types
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.orm import Session

from database import SessionLocal
from models import User

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://nomeans1987.github.io/habits")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# JS getDay(): 0=Sun, 1=Mon ... 6=Sat
# Python weekday(): 0=Mon ... 6=Sun
# Mapping: js = (py + 1) % 7
def _js_weekday(today: date) -> int:
    return (today.weekday() + 1) % 7


# ─── DB helpers (sync, run via asyncio.to_thread) ─────────────────────────────

def _get_active_users() -> list[User]:
    db: Session = SessionLocal()
    try:
        return db.query(User).filter(User.is_active == True).all()
    finally:
        db.close()


def _set_active(telegram_id: int, active: bool) -> None:
    db: Session = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        if user:
            user.is_active = active
            db.commit()
    finally:
        db.close()


# ─── Scheduling logic ─────────────────────────────────────────────────────────

def _parse_state(user: User) -> dict:
    try:
        return json.loads(user.state_json)
    except Exception:
        return {}


def _is_habit_due(habit: dict, today: date) -> bool:
    js_day = _js_weekday(today)
    repeat = habit.get("repeat", "daily")
    if repeat == "daily":
        return True
    if repeat == "weekdays":
        return 1 <= js_day <= 5
    if repeat == "weekend":
        return js_day == 0 or js_day == 6
    if repeat == "custom":
        return js_day in habit.get("days", [])
    if repeat == "interval":
        interval = habit.get("intervalDays", 2)
        try:
            created = date.fromisoformat(habit["createdAt"][:10])
            return (today - created).days % interval == 0
        except Exception:
            return False
    return True


def _is_done_today(habit: dict, today_str: str) -> bool:
    return bool(habit.get("completions", {}).get(today_str))


def _get_todos_for_today(todos: list, today: date) -> list:
    today_str = today.isoformat()
    js_day = _js_weekday(today)
    result = []
    for todo in todos:
        if todo.get("done"):
            continue
        repeat = todo.get("repeat", "none")
        if repeat == "none":
            if todo.get("date") == today_str:
                result.append(todo)
        elif repeat == "daily":
            result.append(todo)
        elif repeat == "weekdays" and 1 <= js_day <= 5:
            result.append(todo)
        elif repeat == "weekend" and (js_day == 0 or js_day == 6):
            result.append(todo)
        elif repeat == "custom" and js_day in todo.get("days", []):
            result.append(todo)
    return result


async def _send_safe(telegram_id: int, text: str, **kwargs) -> bool:
    """Send message, deactivate user if they blocked the bot."""
    try:
        await bot.send_message(telegram_id, text, **kwargs)
        return True
    except TelegramForbiddenError:
        logger.info("User %d blocked the bot — deactivating", telegram_id)
        await asyncio.to_thread(_set_active, telegram_id, False)
        return False
    except Exception as e:
        logger.warning("Failed to send to %d: %s", telegram_id, e)
        return False


# ─── Notifications ────────────────────────────────────────────────────────────

async def send_morning_reminder():
    today = date.today()
    users = await asyncio.to_thread(_get_active_users)
    sem = asyncio.Semaphore(25)  # stay safely under Telegram's 30 msg/sec limit

    async def notify(user: User):
        async with sem:
            state = _parse_state(user)
            due_habits = [h for h in state.get("habits", []) if _is_habit_due(h, today)]
            due_todos = _get_todos_for_today(state.get("todos", []), today)
            if not due_habits and not due_todos:
                return

            lines = ["☀️ <b>Доброе утро! Вот твой план на сегодня:</b>\n"]
            if due_habits:
                lines.append("🌱 <b>Привычки:</b>")
                for h in due_habits:
                    streak = h.get("streak", 0)
                    streak_str = f" 🔥{streak}" if streak >= 2 else ""
                    lines.append(f"  • {h['name']}{streak_str}")
            if due_todos:
                lines.append("\n✅ <b>Задачи:</b>")
                for t in due_todos:
                    lines.append(f"  • {t['text']}")
            lines.append(f"\n<a href='{WEBAPP_URL}'>Открыть трекер →</a>")
            await _send_safe(
                user.telegram_id, "\n".join(lines),
                parse_mode="HTML", disable_web_page_preview=True,
            )

    await asyncio.gather(*[notify(u) for u in users])
    logger.info("Morning reminder sent, %d active users", len(users))


async def send_evening_reminder():
    today = date.today()
    today_str = today.isoformat()
    users = await asyncio.to_thread(_get_active_users)
    sem = asyncio.Semaphore(25)

    async def notify(user: User):
        async with sem:
            state = _parse_state(user)
            habits = state.get("habits", [])
            undone = [
                h for h in habits
                if _is_habit_due(h, today) and not _is_done_today(h, today_str)
            ]
            if not undone:
                xp = state.get("rpg", {}).get("xp", 0)
                await _send_safe(
                    user.telegram_id,
                    f"🏆 <b>Отличный день!</b>\n\nВсе привычки выполнены.\nТекущий XP: <b>{xp}</b>",
                    parse_mode="HTML",
                )
            else:
                lines = ["🌙 <b>Вечерняя сводка</b>\n", "Ещё не отмечено:\n"]
                for h in undone:
                    lines.append(f"  • {h['name']}")
                lines.append(f"\n<a href='{WEBAPP_URL}'>Отметить →</a>")
                await _send_safe(
                    user.telegram_id, "\n".join(lines),
                    parse_mode="HTML", disable_web_page_preview=True,
                )

    await asyncio.gather(*[notify(u) for u in users])
    logger.info("Evening reminder sent, %d active users", len(users))


# ─── Commands ─────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await asyncio.to_thread(_set_active, message.from_user.id, True)
    await message.answer(
        "👋 Привет! Это бот для трекера привычек.\n\n"
        "Я буду присылать:\n"
        "• ☀️ <b>09:00</b> — план на день\n"
        "• 🌙 <b>21:00</b> — что не успел сделать\n\n"
        f"<a href='{WEBAPP_URL}'>Открыть трекер →</a>\n\n"
        "/off — отключить уведомления",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@dp.message(Command("off"))
async def cmd_off(message: types.Message):
    await asyncio.to_thread(_set_active, message.from_user.id, False)
    await message.answer("Уведомления отключены. /start — включить снова.")


# ─── Entry point (called from main.py lifespan) ───────────────────────────────

async def start_bot():
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(send_morning_reminder, "cron", hour=9, minute=0)
    scheduler.add_job(send_evening_reminder, "cron", hour=21, minute=0)
    scheduler.start()
    logger.info("Bot started")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown()
