import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
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
        return {"state": None}
    return {"state": json.loads(user.state_json)}


@app.post("/api/state/{telegram_id}")
@limiter.limit("30/minute")
def save_state(
    request: Request,
    telegram_id: int,
    payload: dict[str, Any],
    x_init_data: str = Header(alias="X-Init-Data"),
    db: Session = Depends(get_db),
):
    _require_auth(x_init_data, telegram_id)
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        user = User(telegram_id=telegram_id, state_json=json.dumps(payload))
        db.add(user)
    else:
        user.state_json = json.dumps(payload)
    db.commit()
    logger.info("State saved for user %d", telegram_id)
    return {"ok": True}
