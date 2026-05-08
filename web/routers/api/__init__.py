"""
JSON REST API — пакет web.routers.api.

Основной роутер с префиксом /api собирается здесь из sub-роутеров.
Для callers вида ``from web.routers import api`` / ``api.router``
интерфейс остался прежним — ничего менять снаружи не нужно.

Структура пакета:
  auth.py         — POST /login, POST /logout
  bots.py         — CRUD /bots/*, GET /status, /bots/{id}/chats, /groups
  pairs.py        — CRUD /pairs/*
  verification.py — PATCH /pairs/{id}/verification
  welcome.py      — CRUD /welcome/configs/*
  assistant.py    — CRUD /assistant/configs/*
  posts.py        — CRUD /posts/*
  logs.py         — GET /logs, POST /webhook/{bot_id}
  inbox.py        — /bots/{id}/inbox/* (groups, users, messages, profile, send, upload, proxy)
"""
from fastapi import APIRouter

from web.routers.api import (
    assistant,
    auth,
    bots,
    inbox,
    kb,
    logs,
    pairs,
    posts,
    verification,
    welcome,
)

router = APIRouter(prefix="/api")
router.include_router(auth.router)
router.include_router(bots.router)
router.include_router(pairs.router)
router.include_router(verification.router)
router.include_router(welcome.router)
router.include_router(assistant.router)
router.include_router(kb.router)
router.include_router(posts.router)
router.include_router(logs.router)
router.include_router(inbox.router)
