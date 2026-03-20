import asyncio
import json
import os
from datetime import datetime, date

from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.orm import Session

from database import SessionLocal
from models import User

BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ─── Helpers ────────────────────────────────────────────────────────────────

def get_all_users() -> list[User]:
    db: Session = SessionLocal()
    try:
        return db.query(User).all()
    finally:
        db.close()


def parse_state(user: User) -> dict:
    try:
        return json.loads(user.state_json)
    except Exception:
        return {}


WEEKDAY_MAP = {0: "пн", 1: "вт", 2: "ср", 3: "чт", 4: "пт", 5: "сб", 6: "вс"}
WEEKDAY_IDX = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}


def is_habit_due_today(habit: dict) -> bool:
    """Проверяет, нужно ли выполнять привычку сегодня."""
    today = date.today()
    weekday = today.weekday()  # 0=пн, 6=вс
    repeat = habit.get("repeat", "daily")

    if repeat == "daily":
        return True
    elif repeat == "weekdays":
        return weekday < 5
    elif repeat == "weekend":
        return weekday >= 5
    elif repeat == "custom":
        days = habit.get("days", [])
        return WEEKDAY_MAP.get(weekday) in days
    elif repeat == "interval":
        interval = habit.get("intervalDays", 2)
        created_str = habit.get("createdAt", "")
        try:
            created = date.fromisoformat(created_str[:10])
            delta = (today - created).days
            return delta % interval == 0
        except Exception:
            return False
    return True


def is_completed_today(habit: dict) -> bool:
    today_str = date.today().isoformat()
    return bool(habit.get("completions", {}).get(today_str))


def get_todos_for_today(todos: list) -> list:
    today_str = date.today().isoformat()
    today_weekday = WEEKDAY_MAP.get(date.today().weekday(), "")
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
        elif repeat == "weekdays" and date.today().weekday() < 5:
            result.append(todo)
        elif repeat == "weekend" and date.today().weekday() >= 5:
            result.append(todo)
        elif repeat == "custom":
            if today_weekday in todo.get("days", []):
                result.append(todo)
    return result


# ─── Notifications ──────────────────────────────────────────────────────────

async def send_morning_reminder():
    """09:00 — список привычек и задач на день."""
    users = get_all_users()
    for user in users:
        state = parse_state(user)
        habits = state.get("habits", [])
        todos = state.get("todos", [])

        due_habits = [h for h in habits if is_habit_due_today(h)]
        due_todos = get_todos_for_today(todos)

        if not due_habits and not due_todos:
            continue

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

        lines.append("\n<a href='https://nomeans1987.github.io/habits'>Открыть трекер →</a>")

        try:
            await bot.send_message(
                user.telegram_id,
                "\n".join(lines),
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        except Exception as e:
            print(f"Ошибка отправки {user.telegram_id}: {e}")


async def send_evening_reminder():
    """21:00 — что не сделал за день."""
    users = get_all_users()
    for user in users:
        state = parse_state(user)
        habits = state.get("habits", [])

        undone = [
            h for h in habits
            if is_habit_due_today(h) and not is_completed_today(h)
        ]

        if not undone:
            # Всё сделано — поздравляем
            try:
                rpg = state.get("rpg", {})
                xp = rpg.get("xp", 0)
                await bot.send_message(
                    user.telegram_id,
                    f"🏆 <b>Отличный день!</b>\n\nВсе привычки выполнены. +XP в копилку.\nТекущий XP: <b>{xp}</b>",
                    parse_mode="HTML"
                )
            except Exception as e:
                print(f"Ошибка отправки {user.telegram_id}: {e}")
            continue

        lines = ["🌙 <b>Вечерняя сводка</b>\n", "Ещё не отмечено сегодня:\n"]
        for h in undone:
            lines.append(f"  • {h['name']}")

        lines.append(f"\n<a href='https://nomeans1987.github.io/habits'>Отметить →</a>")

        try:
            await bot.send_message(
                user.telegram_id,
                "\n".join(lines),
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        except Exception as e:
            print(f"Ошибка отправки {user.telegram_id}: {e}")


# ─── Commands ────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! Это бот для трекера привычек.\n\n"
        "Я буду присылать:\n"
        "• ☀️ <b>09:00</b> — план на день\n"
        "• 🌙 <b>21:00</b> — что не успел сделать\n\n"
        "Открывай трекер и начинай отмечать привычки 👇\n"
        "<a href='https://nomeans1987.github.io/habits'>nomeans1987.github.io/habits</a>",
        parse_mode="HTML",
        disable_web_page_preview=True
    )


# ─── Main ────────────────────────────────────────────────────────────────────

async def main():
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(send_morning_reminder, "cron", hour=9, minute=0)
    scheduler.add_job(send_evening_reminder, "cron", hour=21, minute=0)
    scheduler.start()

    print("Бот запущен ✅")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
