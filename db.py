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
    category    TEXT NOT NULL DEFAULT 'Свободное время',
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    date        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'accepted', 'declined'))
);

CREATE TABLE IF NOT EXISTS checklist_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER NOT NULL,
    text        TEXT NOT NULL,
    is_done     INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (event_id) REFERENCES events (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_events_created ON events (created_by);
CREATE INDEX IF NOT EXISTS idx_events_target  ON events (target_user);
"""


@asynccontextmanager
async def connect():
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON;")
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
    async with connect() as conn:
        await conn.execute("UPDATE users SET partner_id = ? WHERE telegram_id = ?", (b, a))
        await conn.execute("UPDATE users SET partner_id = ? WHERE telegram_id = ?", (a, b))
        await conn.commit()


async def create_event(created_by: int, target_user: int, category: str, title: str, description: str, date: str, items: list = None):
    status = "accepted" if category == "Магазин" else "pending"
    async with connect() as conn:
        cur = await conn.execute(
            "INSERT INTO events (created_by, target_user, category, title, description, date, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (created_by, target_user, category, title, description, date, status),
        )
        event_id = cur.lastrowid
        if category == "Магазин" and items:
            for item in items:
                if str(item).strip():
                    await conn.execute(
                        "INSERT INTO checklist_items (event_id, text) VALUES (?, ?)",
                        (event_id, str(item).strip()),
                    )
        await conn.commit()
        return await get_event(event_id)


async def get_event(event_id: int):
    async with connect() as conn:
        cur = await conn.execute("SELECT * FROM events WHERE id = ?", (event_id,))
        row = await cur.fetchone()
        if not row:
            return None
        event = dict(row)
        event["items"] = []
        if event["category"] == "Магазин":
            cur_items = await conn.execute(
                "SELECT id, text, is_done FROM checklist_items WHERE event_id = ? ORDER BY id",
                (event_id,),
            )
            items_rows = await cur_items.fetchall()
            event["items"] = [{"id": i["id"], "text": i["text"], "is_done": bool(i["is_done"])} for i in items_rows]
        return event


async def list_events(user_id: int):
    async with connect() as conn:
        cur = await conn.execute(
            "SELECT id FROM events WHERE created_by = ? OR target_user = ? ORDER BY date, id",
            (user_id, user_id),
        )
        rows = await cur.fetchall()
        events = []
        for r in rows:
            e = await get_event(r["id"])
            if e:
                events.append(e)
        return events


async def set_status(event_id: int, status: str) -> None:
    async with connect() as conn:
        await conn.execute("UPDATE events SET status = ? WHERE id = ?", (status, event_id))
        await conn.commit()


async def toggle_checklist_item(item_id: int) -> None:
    async with connect() as conn:
        await conn.execute("UPDATE checklist_items SET is_done = NOT is_done WHERE id = ?", (item_id,))
        await conn.commit()


async def add_checklist_item(event_id: int, text: str):
    async with connect() as conn:
        await conn.execute("INSERT INTO checklist_items (event_id, text) VALUES (?, ?)", (event_id, text.strip()))
        await conn.commit()
