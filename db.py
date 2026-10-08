import os
import secrets
from libsql_client import create_client

TURSO_URL = os.getenv("TURSO_URL", "").strip()
TURSO_TOKEN = os.getenv("TURSO_TOKEN", "").strip()


def get_client():
    if TURSO_URL and TURSO_TOKEN:
        url = TURSO_URL
        if "://" in url:
            url = "https://" + url.split("://", 1)[1]
        else:
            url = f"https://{url}"
        return create_client(url=url, auth_token=TURSO_TOKEN)
    return create_client(url="file:calendar.db")


async def init_db() -> None:
    async with get_client() as client:
        await client.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                partner_id  INTEGER,
                invite_code TEXT NOT NULL UNIQUE
            )
            """
        )
        await client.execute(
            """
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
            )
            """
        )
        await client.execute(
            """
            CREATE TABLE IF NOT EXISTS checklist_items (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id    INTEGER NOT NULL,
                text        TEXT NOT NULL,
                is_done     INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (event_id) REFERENCES events (id) ON DELETE CASCADE
            )
            """
        )
        await client.execute("CREATE INDEX IF NOT EXISTS idx_events_created ON events (created_by)")
        await client.execute("CREATE INDEX IF NOT EXISTS idx_events_target ON events (target_user)")


def _user_to_dict(r):
    if not r:
        return None
    return {
        "telegram_id": r[0],
        "partner_id": r[1],
        "invite_code": r[2],
    }


async def get_user(telegram_id: int):
    async with get_client() as client:
        rs = await client.execute(
            "SELECT telegram_id, partner_id, invite_code FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        if rs.rows:
            return _user_to_dict(rs.rows[0])
        return None


async def get_or_create_user(telegram_id: int):
    user = await get_user(telegram_id)
    if user:
        return user
    code = secrets.token_urlsafe(6)
    async with get_client() as client:
        await client.execute(
            "INSERT OR IGNORE INTO users (telegram_id, invite_code) VALUES (?, ?)",
            (telegram_id, code),
        )
    return await get_user(telegram_id)


async def get_user_by_code(code: str):
    async with get_client() as client:
        rs = await client.execute(
            "SELECT telegram_id, partner_id, invite_code FROM users WHERE invite_code = ?",
            (code,),
        )
        if rs.rows:
            return _user_to_dict(rs.rows[0])
        return None


async def link_partners(a: int, b: int) -> None:
    async with get_client() as client:
        await client.execute("UPDATE users SET partner_id = ? WHERE telegram_id = ?", (b, a))
        await client.execute("UPDATE users SET partner_id = ? WHERE telegram_id = ?", (a, b))


async def create_event(created_by: int, target_user: int, category: str, title: str, description: str, date: str, items: list = None):
    status = "accepted" if category == "Магазин" else "pending"

    async with get_client() as client:
        rs = await client.execute(
            "INSERT INTO events (created_by, target_user, category, title, description, date, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (created_by, target_user, category, title, description, date, status),
        )
        event_id = rs.rows[0][0]

        if category == "Магазин" and items:
            for item in items:
                if str(item).strip():
                    await client.execute(
                        "INSERT INTO checklist_items (event_id, text) VALUES (?, ?)",
                        (event_id, str(item).strip()),
                    )

        return await get_event(event_id)


async def get_event(event_id: int):
    async with get_client() as client:
        rs = await client.execute(
            "SELECT id, created_by, target_user, category, title, description, date, status FROM events WHERE id = ?",
            (event_id,),
        )
        if not rs.rows:
            return None
        r = rs.rows[0]
        event = {
            "id": r[0],
            "created_by": r[1],
            "target_user": r[2],
            "category": r[3],
            "title": r[4],
            "description": r[5],
            "date": r[6],
            "status": r[7],
            "items": [],
        }
        if event["category"] == "Магазин":
            items_rs = await client.execute(
                "SELECT id, text, is_done FROM checklist_items WHERE event_id = ? ORDER BY id",
                (event_id,),
            )
            event["items"] = [{"id": i[0], "text": i[1], "is_done": bool(i[2])} for i in items_rs.rows]
        return event


async def list_events(user_id: int):
    async with get_client() as client:
        rs = await client.execute(
            "SELECT id FROM events WHERE created_by = ? OR target_user = ? ORDER BY date, id",
            (user_id, user_id),
        )
        events = []
        for row in rs.rows:
            e = await get_event(row[0])
            if e:
                events.append(e)
        return events


async def set_status(event_id: int, status: str) -> None:
    async with get_client() as client:
        await client.execute("UPDATE events SET status = ? WHERE id = ?", (status, event_id))


async def toggle_checklist_item(item_id: int) -> None:
    async with get_client() as client:
        await client.execute("UPDATE checklist_items SET is_done = NOT is_done WHERE id = ?", (item_id,))


async def add_checklist_item(event_id: int, text: str):
    async with get_client() as client:
        await client.execute("INSERT INTO checklist_items (event_id, text) VALUES (?, ?)", (event_id, text.strip()))
