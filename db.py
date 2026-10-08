import os
from libsql_client import create_client_async

TURSO_URL = os.getenv("TURSO_URL")
TURSO_TOKEN = os.getenv("TURSO_TOKEN")


def get_client():
    if TURSO_URL and TURSO_TOKEN:
        return create_client_async(url=TURSO_URL, auth_token=TURSO_TOKEN)
    return create_client_async(url="file:calendar.db")


async def init_db():
    async with get_client() as client:
        await client.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                invite_code TEXT UNIQUE NOT NULL,
                partner_id INTEGER
            )
            """
        )
        await client.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_by INTEGER NOT NULL,
                target_user INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                category TEXT DEFAULT 'Свободное время',
                date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
            )
            """
        )
        await client.execute(
            """
            CREATE TABLE IF NOT EXISTS checklist_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                is_completed INTEGER DEFAULT 0
            )
            """
        )


async def get_or_create_user(telegram_id: int):
    import secrets
    async with get_client() as client:
        rs = await client.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        if rs.rows:
            r = rs.rows[0]
            return {"telegram_id": r[0], "invite_code": r[1], "partner_id": r[2]}

        invite_code = secrets.token_hex(4)
        await client.execute(
            "INSERT INTO users (telegram_id, invite_code) VALUES (?, ?)",
            (telegram_id, invite_code),
        )
        return {"telegram_id": telegram_id, "invite_code": invite_code, "partner_id": None}


async def get_user_by_code(code: str):
    async with get_client() as client:
        rs = await client.execute("SELECT * FROM users WHERE invite_code = ?", (code,))
        if rs.rows:
            r = rs.rows[0]
            return {"telegram_id": r[0], "invite_code": r[1], "partner_id": r[2]}
        return None


async def link_partners(user1_id: int, user2_id: int):
    async with get_client() as client:
        await client.execute("UPDATE users SET partner_id = ? WHERE telegram_id = ?", (user2_id, user1_id))
        await client.execute("UPDATE users SET partner_id = ? WHERE telegram_id = ?", (user1_id, user2_id))


async def create_event(created_by: int, target_user: int, title: str, description: str, category: str, date: str, items: list = None):
    async with get_client() as client:
        rs = await client.execute(
            "INSERT INTO events (created_by, target_user, title, description, category, date) VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
            (created_by, target_user, title, description, category, date),
        )
        event_id = rs.rows[0][0]

        if category == "Магазин" and items:
            for item in items:
                item_title = str(item).strip()
                if item_title:
                    await client.execute(
                        "INSERT INTO checklist_items (event_id, title) VALUES (?, ?)",
                        (event_id, item_title),
                    )

        return {
            "id": event_id,
            "created_by": created_by,
            "target_user": target_user,
            "title": title,
            "description": description,
            "category": category,
            "date": date,
            "status": "pending",
            "items": []
        }


async def get_event(event_id: int):
    async with get_client() as client:
        rs = await client.execute("SELECT * FROM events WHERE id = ?", (event_id,))
        if rs.rows:
            r = rs.rows[0]
            return {
                "id": r[0], "created_by": r[1], "target_user": r[2],
                "title": r[3], "description": r[4], "category": r[5],
                "date": r[6], "status": r[7]
            }
        return None


async def list_events(user_id: int):
    async with get_client() as client:
        rs = await client.execute(
            "SELECT * FROM events WHERE created_by = ? OR target_user = ? ORDER BY date ASC",
            (user_id, user_id),
        )
        events = []
        for r in rs.rows:
            e = {
                "id": r[0], "created_by": r[1], "target_user": r[2],
                "title": r[3], "description": r[4], "category": r[5],
                "date": r[6], "status": r[7], "items": []
            }
            if e["category"] == "Магазин":
                c_rs = await client.execute("SELECT id, title, is_completed FROM checklist_items WHERE event_id = ?", (e["id"],))
                e["items"] = [{"id": ci[0], "title": ci[1], "is_completed": bool(ci[2])} for ci in c_rs.rows]
            events.append(e)
        return events


async def set_status(event_id: int, status: str):
    async with get_client() as client:
        await client.execute("UPDATE events SET status = ? WHERE id = ?", (status, event_id))


async def toggle_checklist_item(item_id: int):
    async with get_client() as client:
        await client.execute("UPDATE checklist_items SET is_completed = NOT is_completed WHERE id = ?", (item_id,))


async def add_checklist_item(event_id: int, title: str):
    async with get_client() as client:
        rs = await client.execute(
            "INSERT INTO checklist_items (event_id, title) VALUES (?, ?) RETURNING id",
            (event_id, title),
        )
        return {"id": rs.rows[0][0], "title": title, "is_completed": 0}