import asyncio
import json
import logging
import os
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sentry_sdk
from aiogram import Bot, Dispatcher, types
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from apscheduler.events import EVENT_JOB_ERROR
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
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

# Single shared scheduler (MemoryJobStore — no DB state, no ConflictingIdError).
scheduler = AsyncIOScheduler(timezone="UTC")


def _on_scheduler_error(event) -> None:
    """Forward APScheduler job exceptions to Sentry."""
    if event.exception:
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("scheduler.job_id", event.job_id)
            scope.set_context("scheduler_event", {
                "job_id": event.job_id,
                "scheduled_run_time": str(event.scheduled_run_time),
            })
            sentry_sdk.capture_exception(event.exception)
        logger.error(
            "Scheduler job %s raised: %s", event.job_id, event.exception,
            exc_info=event.traceback,
        )


scheduler.add_listener(_on_scheduler_error, EVENT_JOB_ERROR)

REMINDER_HORIZON_HOURS = 48  # how far ahead per-habit reminders are scheduled
JOB_PREFIX = "rem_"  # all user-scheduled reminder jobs start with this


# ─── Weekday mapping ──────────────────────────────────────────────────────────
# JS getDay(): 0=Sun ... 6=Sat
# Python weekday(): 0=Mon ... 6=Sun
# Mapping: js = (py + 1) % 7
def _js_weekday(d: date) -> int:
    return (d.weekday() + 1) % 7


# ─── DB helpers (sync, run via asyncio.to_thread) ─────────────────────────────

def _get_active_users() -> list[User]:
    db: Session = SessionLocal()
    try:
        return db.query(User).filter(User.is_active == True).all()
    finally:
        db.close()


def _get_user(telegram_id: int) -> User | None:
    db: Session = SessionLocal()
    try:
        return db.query(User).filter(User.telegram_id == telegram_id).first()
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


def _parse_state(user: User) -> dict:
    try:
        return json.loads(user.state_json)
    except Exception:
        return {}


def _safe_tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "Europe/Moscow")
    except ZoneInfoNotFoundError:
        return ZoneInfo("Europe/Moscow")


# ─── Repeat-rule helpers ──────────────────────────────────────────────────────

def _is_habit_due(habit: dict, day: date) -> bool:
    js_day = _js_weekday(day)
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
        interval = habit.get("intervalDays", 2) or 2
        try:
            created = date.fromisoformat((habit.get("createdAt") or "2025-01-01")[:10])
            return (day - created).days >= 0 and (day - created).days % interval == 0
        except Exception:
            return False
    return True


def _is_todo_due(todo: dict, day: date) -> bool:
    repeat = todo.get("repeat", "none")
    if todo.get("done"):
        return False
    if repeat == "none":
        return todo.get("date") == day.isoformat()
    js_day = _js_weekday(day)
    if repeat == "daily":
        return True
    if repeat == "weekdays":
        return 1 <= js_day <= 5
    if repeat == "weekend":
        return js_day == 0 or js_day == 6
    if repeat == "custom":
        return js_day in todo.get("days", [])
    return False


def _is_done_today(habit: dict, today_str: str) -> bool:
    return bool(habit.get("completions", {}).get(today_str))


def _get_todos_for_today(todos: list, today: date) -> list:
    return [t for t in todos if _is_todo_due(t, today)]


def _parse_hhmm(s: str | None) -> tuple[int, int] | None:
    if not s or not isinstance(s, str):
        return None
    try:
        h, m = s.split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except Exception:
        pass
    return None


# ─── Sending ──────────────────────────────────────────────────────────────────

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


# ─── Per-habit / per-todo reminder callbacks ─────────────────────────────────
# These run in the scheduler event loop. Keep them idempotent-ish: re-check
# current state in case the habit was deleted / marked done / user deactivated.

async def send_habit_reminder(telegram_id: int, habit_id: int):
    user = await asyncio.to_thread(_get_user, telegram_id)
    if not user or not user.is_active:
        return
    state = _parse_state(user)
    habit = next((h for h in state.get("habits", []) if h.get("id") == habit_id), None)
    if not habit:
        return
    tz = _safe_tz(state.get("tz") or user.tz)
    today_local = datetime.now(tz).date()
    # Only fire if habit is due today AND not already done
    if not _is_habit_due(habit, today_local):
        return
    if _is_done_today(habit, today_local.isoformat()):
        return
    streak = habit.get("streak") or 0
    streak_str = f" 🔥{streak}" if streak >= 2 else ""
    text = (
        f"🌱 <b>Напоминание:</b> {habit['name']}{streak_str}\n\n"
        f"<a href='{WEBAPP_URL}'>Отметить →</a>"
    )
    await _send_safe(telegram_id, text, parse_mode="HTML", disable_web_page_preview=True)


async def send_todo_reminder(telegram_id: int, todo_id: int):
    user = await asyncio.to_thread(_get_user, telegram_id)
    if not user or not user.is_active:
        return
    state = _parse_state(user)
    todo = next((t for t in state.get("todos", []) if t.get("id") == todo_id), None)
    if not todo or todo.get("done"):
        return
    tz = _safe_tz(state.get("tz") or user.tz)
    today_local = datetime.now(tz).date()
    if not _is_todo_due(todo, today_local):
        return
    text = (
        f"✅ <b>Задача:</b> {todo['text']}\n\n"
        f"<a href='{WEBAPP_URL}'>Открыть →</a>"
    )
    await _send_safe(telegram_id, text, parse_mode="HTML", disable_web_page_preview=True)


# ─── Rescheduling logic ──────────────────────────────────────────────────────

def _remove_user_jobs(telegram_id: int) -> int:
    """Remove all reminder jobs belonging to this user. Returns count removed."""
    prefix = f"{JOB_PREFIX}{telegram_id}_"
    removed = 0
    for job in scheduler.get_jobs():
        if job.id.startswith(prefix):
            try:
                scheduler.remove_job(job.id)
                removed += 1
            except Exception:
                pass
    return removed


def _schedule_one(job_id: str, func, run_at_utc: datetime, args: list):
    # replace_existing means editing a habit silently replaces its old job
    scheduler.add_job(
        func,
        "date",
        run_date=run_at_utc,
        id=job_id,
        args=args,
        replace_existing=True,
        misfire_grace_time=300,  # tolerate 5 min lag after restart
    )


def _upcoming_fires(
    time_str: str,
    user_tz: ZoneInfo,
    is_due_fn,
    now_utc: datetime,
    horizon_hours: int,
) -> list[datetime]:
    """Yield UTC datetimes when a reminder should fire across the horizon."""
    hm = _parse_hhmm(time_str)
    if not hm:
        return []
    hour, minute = hm
    now_local = now_utc.astimezone(user_tz)
    horizon_end = now_utc + timedelta(hours=horizon_hours)
    fires: list[datetime] = []
    # Check up to (horizon_hours/24 + 2) days to cover edge cases
    max_days = horizon_hours // 24 + 2
    for offset in range(max_days):
        day_local = (now_local + timedelta(days=offset)).date()
        if not is_due_fn(day_local):
            continue
        fire_local = datetime.combine(
            day_local, dtime(hour, minute), tzinfo=user_tz
        )
        fire_utc = fire_local.astimezone(timezone.utc)
        if fire_utc <= now_utc:
            continue
        if fire_utc > horizon_end:
            break
        fires.append(fire_utc)
    return fires


async def reschedule_user(telegram_id: int) -> int:
    """Rebuild reminder jobs for one user. Must run on the event loop (not in a thread)."""
    # DB read is blocking — run in thread pool
    user = await asyncio.to_thread(_get_user, telegram_id)
    if not user:
        return 0
    state = _parse_state(user)
    tz = _safe_tz(state.get("tz") or user.tz)
    now_utc = datetime.now(timezone.utc)

    # Scheduler calls MUST happen here (event loop), not inside to_thread.
    # AsyncIOScheduler is not thread-safe.
    _remove_user_jobs(telegram_id)

    if not user.is_active:
        return 0

    added = 0
    for habit in state.get("habits", []):
        hid = habit.get("id")
        time_str = habit.get("reminderTime")
        if not hid or not time_str:
            continue
        fires = _upcoming_fires(
            time_str, tz, lambda d, h=habit: _is_habit_due(h, d),
            now_utc, REMINDER_HORIZON_HOURS,
        )
        for fire_utc in fires:
            job_id = f"{JOB_PREFIX}{telegram_id}_h{hid}_{int(fire_utc.timestamp())}"
            _schedule_one(job_id, send_habit_reminder, fire_utc, [telegram_id, hid])
            added += 1

    for todo in state.get("todos", []):
        tid = todo.get("id")
        time_str = todo.get("reminderTime")
        if not tid or not time_str or todo.get("done"):
            continue
        fires = _upcoming_fires(
            time_str, tz, lambda d, t=todo: _is_todo_due(t, d),
            now_utc, REMINDER_HORIZON_HOURS,
        )
        for fire_utc in fires:
            job_id = f"{JOB_PREFIX}{telegram_id}_t{tid}_{int(fire_utc.timestamp())}"
            _schedule_one(job_id, send_todo_reminder, fire_utc, [telegram_id, tid])
            added += 1

    logger.info("Rescheduled user %d: %d reminder jobs", telegram_id, added)
    return added


async def reschedule_all_active():
    """Nightly job: extend horizon for every active user."""
    users = await asyncio.to_thread(_get_active_users)
    total = 0
    for u in users:
        try:
            total += await reschedule_user(u.telegram_id)  # no to_thread — scheduler ops on event loop
        except Exception as e:
            logger.warning("reschedule failed for %d: %s", u.telegram_id, e)
    logger.info("Nightly reschedule: %d users, %d jobs total", len(users), total)


# ─── Legacy morning/evening summaries (Moscow, for now) ──────────────────────

async def send_morning_reminder():
    today = date.today()
    users = await asyncio.to_thread(_get_active_users)
    sem = asyncio.Semaphore(25)

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
    await reschedule_user(message.from_user.id)
    await message.answer(
        "👋 Привет! Это бот для трекера привычек.\n\n"
        "Я буду присылать:\n"
        "• ☀️ <b>09:00</b> — план на день\n"
        "• 🌙 <b>21:00</b> — что не успел сделать\n"
        "• 🔔 Персональные напоминания по каждой привычке/задаче (в твоей таймзоне)\n\n"
        f"<a href='{WEBAPP_URL}'>Открыть трекер →</a>\n\n"
        "/off — отключить все уведомления",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@dp.message(Command("off"))
async def cmd_off(message: types.Message):
    await asyncio.to_thread(_set_active, message.from_user.id, False)
    _remove_user_jobs(message.from_user.id)  # scheduler op — event loop only
    await message.answer("Уведомления отключены. /start — включить снова.")


# ─── Entry point (called from main.py lifespan) ───────────────────────────────

async def start_bot():
    scheduler.add_job(
        send_morning_reminder,
        CronTrigger(hour=9, minute=0, timezone="Europe/Moscow"),
        id="global_morning",
    )
    scheduler.add_job(
        send_evening_reminder,
        CronTrigger(hour=21, minute=0, timezone="Europe/Moscow"),
        id="global_evening",
    )
    scheduler.add_job(
        reschedule_all_active,
        CronTrigger(hour=3, minute=0, timezone="UTC"),
        id="nightly_reschedule",
    )
    scheduler.start()
    # On cold start, top up everyone's reminders so we don't rely on user saves
    try:
        await reschedule_all_active()
    except Exception as e:
        logger.warning("Initial reschedule failed: %s", e)
    logger.info("Bot started")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown()
