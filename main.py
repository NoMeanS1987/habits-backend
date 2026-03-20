from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from typing import Any
import json

from database import get_db, engine
from models import Base, User

# Создаём таблицы при старте
Base.metadata.create_all(bind=engine)

app = FastAPI(title="HabitRPG API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # в проде заменить на свой домен
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/state/{telegram_id}")
def get_state(telegram_id: int, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        return {"state": None}
    return {"state": json.loads(user.state_json)}


@app.post("/api/state/{telegram_id}")
def save_state(telegram_id: int, payload: dict[str, Any], db: Session = Depends(get_db)):
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        user = User(telegram_id=telegram_id, state_json=json.dumps(payload))
        db.add(user)
    else:
        user.state_json = json.dumps(payload)
    db.commit()
    return {"ok": True}
