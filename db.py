import os
import aiosqlite

DB_PATH = os.getenv("DB_PATH", "calendar.db")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                invite_code TEXT UNIQUE NOT NULL,
                partner_id INTEGER REFERENCES users(telegram_id)
            )
            """
        )
        await db.execute(
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
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS checklist_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                is_completed INTEGER DEFAULT 0
            )
            """
        )
        # Миграция: проверяем наличие колонки category в старых БД
        cursor = await db.execute("PRAGMA table_info(events)")
        columns = [row[1] for row in await cursor.fetchall()]
        if "category" not in columns:
            await db.execute("ALTER TABLE events ADD COLUMN category TEXT DEFAULT 'Свободное время'")

        await db.commit()


async def get_or_create_user(telegram_id: int):
    import secrets
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)) as cursor:
            user = await cursor.fetchone()
            if user:
                return dict(user)

        invite_code = secrets.token_hex(4)
        await db.execute(
            "INSERT INTO users (telegram_id, invite_code) VALUES (?, ?)",
            (telegram_id, invite_code),
        )
        await db.commit()
        return {"telegram_id": telegram_id, "invite_code": invite_code, "partner_id": None}


async def get_user_by_code(code: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE invite_code = ?", (code,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def link_partners(user1_id: int, user2_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET partner_id = ? WHERE telegram_id = ?", (user2_id, user1_id))
        await db.execute("UPDATE users SET partner_id = ? WHERE telegram_id = ?", (user1_id, user2_id))
        await db.commit()


async def create_event(created_by: int, target_user: int, title: str, description: str, category: str, date: str, items: list = None):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "INSERT INTO events (created_by, target_user, title, description, category, date) VALUES (?, ?, ?, ?, ?, ?)",
            (created_by, target_user, title, description, category, date),
        )
        event_id = cursor.lastrowid
        
        if category == "Магазин" and items:
            for item in items:
                item_title = str(item).strip()
                if item_title:
                    await db.execute(
                        "INSERT INTO checklist_items (event_id, title) VALUES (?, ?)",
                        (event_id, item_title),
                    )

        await db.commit()
        async with db.execute("SELECT * FROM events WHERE id = ?", (event_id,)) as cur:
            row = await cur.fetchone()
            return dict(row)


async def get_event(event_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM events WHERE id = ?", (event_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def list_events(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM events WHERE created_by = ? OR target_user = ? ORDER BY date ASC",
            (user_id, user_id),
        ) as cursor:
            events = [dict(r) for r in await cursor.fetchall()]

        for e in events:
            if e["category"] == "Магазин":
                async with db.execute(
                    "SELECT id, title, is_completed FROM checklist_items WHERE event_id = ?",
                    (e["id"],),
                ) as c_cursor:
                    e["items"] = [dict(item) for item in await c_cursor.fetchall()]
            else:
                e["items"] = []

        return events


async def set_status(event_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE events SET status = ? WHERE id = ?", (status, event_id))
        await db.commit()


async def toggle_checklist_item(item_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE checklist_items SET is_completed = NOT is_completed WHERE id = ?", (item_id,))
        await db.commit()


async def add_checklist_item(event_id: int, title: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO checklist_items (event_id, title) VALUES (?, ?)",
            (event_id, title),
        )
        item_id = cursor.lastrowid
        await db.commit()
        return {"id": item_id, "title": title, "is_completed": 0}