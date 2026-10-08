import os
import secrets
import libsql_client

TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY,
        partner_id  INTEGER,
        invite_code TEXT NOT NULL UNIQUE
    );
    """,
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
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_created ON events (created_by);",
    "CREATE INDEX IF NOT EXISTS idx_events_target  ON events (target_user);"
]


def _get_client():
    if TURSO_DATABASE_URL:
        # Приведение URL к вебсокет/HTTP схеме для libsql-client
        url = TURSO_DATABASE_URL
        if url.startswith("libsql://"):
            url = url.replace("libsql://", "wss://")
        return libsql_client.create_client(url=url, auth_token=TURSO_AUTH_TOKEN)
    return libsql_client.create_client(url="file:calendar.db")


def _row_to_dict(rs, row):
    if not row:
        return None
    return dict(zip(rs.columns, row))


async def init_db() -> None:
    async with _get_client() as client:
        for stmt in SCHEMA:
            await client.execute(stmt)


async def get_user(telegram_id: int):
    async with _get_client() as client:
        rs = await client.execute("SELECT * FROM users WHERE telegram_id = ?", [telegram_id])
        if rs.rows:
            return _row_to_dict(rs, rs.rows[0])
    return None


async def get_or_create_user(telegram_id: int):
    user = await get_user(telegram_id)
    if user:
        return user

    async with _get_client() as client:
        await client.execute(
            "INSERT OR IGNORE INTO users (telegram_id, invite_code) VALUES (?, ?)",
            [telegram_id, secrets.token_urlsafe(6)]
        )
    return await get_user(telegram_id)


async def get_user_by_code(code: str):
    async with _get_client() as client:
        rs = await client.execute("SELECT * FROM users WHERE invite_code = ?", [code])
        if rs.rows:
            return _row_to_dict(rs, rs.rows[0])
    return None


async def link_partners(a: int, b: int) -> None:
    async with _get_client() as client:
        await client.execute("UPDATE users SET partner_id = ? WHERE telegram_id = ?", [b, a])
        await client.execute("UPDATE users SET partner_id = ? WHERE telegram_id = ?", [a, b])


async def create_event(created_by: int, target_user: int, title: str, description: str, date: str):
    async with _get_client() as client:
        rs = await client.execute(
            "INSERT INTO events (created_by, target_user, title, description, date) VALUES (?, ?, ?, ?, ?)",
            [created_by, target_user, title, description, date]
        )
        event_id = rs.last_insert_rowid
        rs_event = await client.execute("SELECT * FROM events WHERE id = ?", [event_id])
        return _row_to_dict(rs_event, rs_event.rows[0])


async def list_events(user_id: int):
    async with _get_client() as client:
        rs = await client.execute(
            "SELECT * FROM events WHERE created_by = ? OR target_user = ? ORDER BY date, id",
            [user_id, user_id]
        )
        return [_row_to_dict(rs, row) for row in rs.rows]


async def get_event(event_id: int):
    async with _get_client() as client:
        rs = await client.execute("SELECT * FROM events WHERE id = ?", [event_id])
        if rs.rows:
            return _row_to_dict(rs, rs.rows[0])
    return None


async def set_status(event_id: int, status: str) -> None:
    async with _get_client() as client:
        await client.execute("UPDATE events SET status = ? WHERE id = ?", [status, event_id])
