import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import text
from sqlalchemy.orm import Session

from auth import validate_init_data
from database import engine, get_db
from models import Base, User

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

WEBAPP_ORIGIN = os.getenv("WEBAPP_ORIGIN", "https://nomeans1987.github.io")


def _migrate() -> None:
    """Lightweight schema sync. Adds columns that create_all() can't add."""
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        try:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                "tz VARCHAR(64) NOT NULL DEFAULT 'Europe/Moscow'"
            ))
        except Exception as e:
            logger.warning("tz column migration failed (may already exist): %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _migrate()
    from bot import bot as tg_bot
    from bot import start_bot
    bot_task = asyncio.create_task(start_bot())
    yield
    bot_task.cancel()
    try:
        await bot_task
    except asyncio.CancelledError:
        pass
    await tg_bot.session.close()


limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="HabitRPG API", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[WEBAPP_ORIGIN],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def _require_auth(init_data: str, expected_id: int) -> None:
    user_data = validate_init_data(init_data)
    if not user_data or user_data.get("id") != expected_id:
        raise HTTPException(status_code=403, detail="Forbidden")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/state/{telegram_id}")
@limiter.limit("60/minute")
def get_state(
    request: Request,
    telegram_id: int,
    x_init_data: str = Header(alias="X-Init-Data"),
    db: Session = Depends(get_db),
):
    _require_auth(x_init_data, telegram_id)
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        return {"state": None, "tz": None}
    return {"state": json.loads(user.state_json), "tz": user.tz}


async def _reschedule_after_save(telegram_id: int) -> None:
    # Lazy import so lifespan can initialise scheduler first
    try:
        from bot import reschedule_user
        await reschedule_user(telegram_id)
    except Exception as e:
        logger.warning("reschedule_user failed for %d: %s", telegram_id, e)


@app.post("/api/state/{telegram_id}")
@limiter.limit("30/minute")
def save_state(
    request: Request,
    telegram_id: int,
    payload: dict[str, Any],
    background_tasks: BackgroundTasks,
    x_init_data: str = Header(alias="X-Init-Data"),
    db: Session = Depends(get_db),
):
    _require_auth(x_init_data, telegram_id)
    # Extract & persist tz if provided at top level
    incoming_tz = payload.get("tz") if isinstance(payload, dict) else None
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        user = User(
            telegram_id=telegram_id,
            state_json=json.dumps(payload),
            tz=incoming_tz or "Europe/Moscow",
        )
        db.add(user)
    else:
        user.state_json = json.dumps(payload)
        if incoming_tz and isinstance(incoming_tz, str) and incoming_tz != user.tz:
            user.tz = incoming_tz
    db.commit()
    logger.info("State saved for user %d", telegram_id)
    # Recompute reminder schedule outside the request cycle
    background_tasks.add_task(_reschedule_after_save, telegram_id)
    return {"ok": True}
