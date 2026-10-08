import os
import secrets
from contextlib import asynccontextmanager

import aiosqlite

DB_PATH = os.getenv("DB_PATH", "calendar.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    partner_id  INTEGER,
    invite_code TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_by  INTEGER NOT NULL,
    target_user INTEGER NOT NULL,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    date        TEXT NOT NULL,                      -- YYYY-MM-DD
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'accepted', 'declined'))
);

CREATE INDEX IF NOT EXISTS idx_events_created ON events (created_by);
CREATE INDEX IF NOT EXISTS idx_events_target  ON events (target_user);
"""


@asynccontextmanager
async def connect():
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        yield conn


async def init_db() -> None:
    async with connect() as conn:
        await conn.executescript(SCHEMA)
        await conn.commit()


async def get_user(telegram_id: int):
    async with connect() as conn:
        cur = await conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        return await cur.fetchone()


async def get_or_create_user(telegram_id: int):
    user = await get_user(telegram_id)
    if user:
        return user
    async with connect() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO users (telegram_id, invite_code) VALUES (?, ?)",
            (telegram_id, secrets.token_urlsafe(6)),
        )
        await conn.commit()
    return await get_user(telegram_id)


async def get_user_by_code(code: str):
    async with connect() as conn:
        cur = await conn.execute("SELECT * FROM users WHERE invite_code = ?", (code,))
        return await cur.fetchone()


async def link_partners(a: int, b: int) -> None:
    """Связывает двух пользователей в одной транзакции."""
    async with connect() as conn:
        await conn.execute("UPDATE users SET partner_id = ? WHERE telegram_id = ?", (b, a))
        await conn.execute("UPDATE users SET partner_id = ? WHERE telegram_id = ?", (a, b))
        await conn.commit()


async def create_event(created_by: int, target_user: int, title: str, description: str, date: str):
    async with connect() as conn:
        cur = await conn.execute(
            "INSERT INTO events (created_by, target_user, title, description, date) "
            "VALUES (?, ?, ?, ?, ?)",
            (created_by, target_user, title, description, date),
        )
        await conn.commit()
        cur = await conn.execute("SELECT * FROM events WHERE id = ?", (cur.lastrowid,))
        return await cur.fetchone()


async def list_events(user_id: int):
    async with connect() as conn:
        cur = await conn.execute(
            "SELECT * FROM events WHERE created_by = ? OR target_user = ? ORDER BY date, id",
            (user_id, user_id),
        )
        return await cur.fetchall()


async def get_event(event_id: int):
    async with connect() as conn:
        cur = await conn.execute("SELECT * FROM events WHERE id = ?", (event_id,))
        return await cur.fetchone()


async def set_status(event_id: int, status: str) -> None:
    async with connect() as conn:
        await conn.execute("UPDATE events SET status = ? WHERE id = ?", (status, event_id))
        await conn.commit()
