import os
import secrets
from libsql_client import create_client

TURSO_URL = os.getenv("TURSO_URL")
TURSO_TOKEN = os.getenv("TURSO_TOKEN")


def get_client():
    if TURSO_URL and TURSO_TOKEN:
        url = TURSO_URL
        # Гарантируем использование https:// для стабильного подключения
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
                title       TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                date        TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'accepted', 'declined'))
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


def _event_to_dict(r):
    if not r:
        return None
    return {
        "id": r[0],
        "created_by": r[1],
        "target_user": r[2],
        "title": r[3],
        "description": r[4],
        "date": r[5],
        "status": r[6],
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
    """Связывает двух пользователей в одной транзакции."""
    async with get_client() as client:
        await client.execute("UPDATE users SET partner_id = ? WHERE telegram_id = ?", (b, a))
        await client.execute("UPDATE users SET partner_id = ? WHERE telegram_id = ?", (a, b))


async def create_event(created_by: int, target_user: int, title: str, description: str, date: str):
    async with get_client() as client:
        rs = await client.execute(
            "INSERT INTO events (created_by, target_user, title, description, date) "
            "VALUES (?, ?, ?, ?, ?) RETURNING id, created_by, target_user, title, description, date, status",
            (created_by, target_user, title, description, date),
        )
        if rs.rows:
            return _event_to_dict(rs.rows[0])
        last_id = rs.last_insert_rowid
        rs_sel = await client.execute(
            "SELECT id, created_by, target_user, title, description, date, status FROM events WHERE id = ?",
            (last_id,),
        )
        return _event_to_dict(rs_sel.rows[0])


async def list_events(user_id: int):
    async with get_client() as client:
        rs = await client.execute(
            "SELECT id, created_by, target_user, title, description, date, status FROM events "
            "WHERE created_by = ? OR target_user = ? ORDER BY date, id",
            (user_id, user_id),
        )
        return [_event_to_dict(r) for r in rs.rows]


async def get_event(event_id: int):
    async with get_client() as client:
        rs = await client.execute(
            "SELECT id, created_by, target_user, title, description, date, status FROM events WHERE id = ?",
            (event_id,),
        )
        if rs.rows:
            return _event_to_dict(rs.rows[0])
        return None


async def set_status(event_id: int, status: str) -> None:
    async with get_client() as client:
        await client.execute("UPDATE events SET status = ? WHERE id = ?", (status, event_id))
