import os
import secrets
import libsql_experimental as libsql

TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")

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
    date        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'accepted', 'declined'))
);

CREATE INDEX IF NOT EXISTS idx_events_created ON events (created_by);
CREATE INDEX IF NOT EXISTS idx_events_target  ON events (target_user);
"""


def _get_conn():
    if not TURSO_DATABASE_URL:
        return libsql.connect("calendar.db")
    return libsql.connect(
        database=TURSO_DATABASE_URL,
        auth_token=TURSO_AUTH_TOKEN
    )


def _row_to_dict(cursor, row):
    if row is None:
        return None
    cols = [col[0] for col in cursor.description]
    return dict(zip(cols, row))


def _rows_to_dicts(cursor, rows):
    cols = [col[0] for col in cursor.description]
    return [dict(zip(cols, row)) for row in rows]


async def init_db() -> None:
    conn = _get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


async def get_user(telegram_id: int):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    res = _row_to_dict(cur, row)
    conn.close()
    return res


async def get_or_create_user(telegram_id: int):
    user = await get_user(telegram_id)
    if user:
        return user

    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO users (telegram_id, invite_code) VALUES (?, ?)",
        (telegram_id, secrets.token_urlsafe(6)),
    )
    conn.commit()
    conn.close()
    return await get_user(telegram_id)


async def get_user_by_code(code: str):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE invite_code = ?", (code,))
    row = cur.fetchone()
    res = _row_to_dict(cur, row)
    conn.close()
    return res


async def link_partners(a: int, b: int) -> None:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET partner_id = ? WHERE telegram_id = ?", (b, a))
    cur.execute("UPDATE users SET partner_id = ? WHERE telegram_id = ?", (a, b))
    conn.commit()
    conn.close()


async def create_event(created_by: int, target_user: int, title: str, description: str, date: str):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO events (created_by, target_user, title, description, date) VALUES (?, ?, ?, ?, ?)",
        (created_by, target_user, title, description, date),
    )
    conn.commit()
    event_id = cur.lastrowid
    
    cur.execute("SELECT * FROM events WHERE id = ?", (event_id,))
    row = cur.fetchone()
    res = _row_to_dict(cur, row)
    conn.close()
    return res


async def list_events(user_id: int):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM events WHERE created_by = ? OR target_user = ? ORDER BY date, id",
        (user_id, user_id),
    )
    rows = cur.fetchall()
    res = _rows_to_dicts(cur, rows)
    conn.close()
    return res


async def get_event(event_id: int):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM events WHERE id = ?", (event_id,))
    row = cur.fetchone()
    res = _row_to_dict(cur, row)
    conn.close()
    return res


async def set_status(event_id: int, status: str) -> None:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE events SET status = ? WHERE id = ?", (status, event_id))
    conn.commit()
    conn.close()
