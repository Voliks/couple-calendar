import os
import secrets
import logging
import json
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
    try:
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
                                CHECK (status IN ('pending', 'accepted', 'declined')),
                    checklist   TEXT NOT NULL DEFAULT '[]'
                )
                """
            )
            # Для уже существующих БД: добавляем колонку, если её нет
            try:
                await client.execute(
                    "ALTER TABLE events ADD COLUMN checklist TEXT NOT NULL DEFAULT '[]'"
                )
            except Exception:
                pass  # колонка уже есть
            await client.execute("CREATE INDEX IF NOT EXISTS idx_events_created ON events (created_by)")
            await client.execute("CREATE INDEX IF NOT EXISTS idx_events_target ON events (target_user)")
    except Exception as e:
        logging.error("Ошибка инициализации базы данных: %s", e)


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
    checklist = []
    if len(r) > 7 and r[7]:
        try:
            checklist = json.loads(r[7]) if isinstance(r[7], str) else (r[7] or [])
        except (json.JSONDecodeError, TypeError):
            checklist = []
    if not isinstance(checklist, list):
        checklist = []
    return {
        "id": r[0],
        "created_by": r[1],
        "target_user": r[2],
        "title": r[3],
        "description": r[4],
        "date": r[5],
        "status": r[6],
        "checklist": checklist,
    }


async def get_user(telegram_id: int):
    try:
        async with get_client() as client:
            rs = await client.execute(
                "SELECT telegram_id, partner_id, invite_code FROM users WHERE telegram_id = ?",
                (telegram_id,),
            )
            if rs.rows:
                return _user_to_dict(rs.rows[0])
    except Exception as e:
        logging.error("Ошибка в get_user: %s", e)
    return None


async def get_or_create_user(telegram_id: int):
    user = await get_user(telegram_id)
    if user:
        return user
    code = secrets.token_urlsafe(6)
    try:
        async with get_client() as client:
            await client.execute(
                "INSERT OR IGNORE INTO users (telegram_id, invite_code) VALUES (?, ?)",
                (telegram_id, code),
            )
    except Exception as e:
        logging.error("Ошибка в get_or_create_user: %s", e)
    return await get_user(telegram_id)


async def get_user_by_code(code: str):
    try:
        async with get_client() as client:
            rs = await client.execute(
                "SELECT telegram_id, partner_id, invite_code FROM users WHERE invite_code = ?",
                (code,),
            )
            if rs.rows:
                return _user_to_dict(rs.rows[0])
    except Exception as e:
        logging.error("Ошибка в get_user_by_code: %s", e)
    return None


async def link_partners(a: int, b: int) -> None:
    try:
        async with get_client() as client:
            await client.execute("UPDATE users SET partner_id = ? WHERE telegram_id = ?", (b, a))
            await client.execute("UPDATE users SET partner_id = ? WHERE telegram_id = ?", (a, b))
    except Exception as e:
        logging.error("Ошибка в link_partners: %s", e)


async def create_event(created_by: int, target_user: int, title: str, description: str, date: str, checklist=None, status="pending"):
    if checklist is None:
        checklist = []
    if status not in ("pending", "accepted", "declined"):
        status = "pending"
    checklist_json = json.dumps(checklist, ensure_ascii=False)
    try:
        async with get_client() as client:
            rs = await client.execute(
                "INSERT INTO events (created_by, target_user, title, description, date, status, checklist) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id, created_by, target_user, title, description, date, status, checklist",
                (created_by, target_user, title, description, date, status, checklist_json),
            )
            if rs.rows:
                return _event_to_dict(rs.rows[0])
            last_id = rs.last_insert_rowid
            rs_sel = await client.execute(
                "SELECT id, created_by, target_user, title, description, date, status, checklist FROM events WHERE id = ?",
                (last_id,),
            )
            return _event_to_dict(rs_sel.rows[0])
    except Exception as e:
        logging.error("Ошибка в create_event: %s", e)
        raise


async def list_events(user_id: int):
    try:
        async with get_client() as client:
            rs = await client.execute(
                "SELECT id, created_by, target_user, title, description, date, status, checklist FROM events "
                "WHERE created_by = ? OR target_user = ? ORDER BY date, id",
                (user_id, user_id),
            )
            return [_event_to_dict(r) for r in rs.rows]
    except Exception as e:
        logging.error("Ошибка в list_events: %s", e)
        return []


async def get_event(event_id: int):
    try:
        async with get_client() as client:
            rs = await client.execute(
                "SELECT id, created_by, target_user, title, description, date, status, checklist FROM events WHERE id = ?",
                (event_id,),
            )
            if rs.rows:
                return _event_to_dict(rs.rows[0])
    except Exception as e:
        logging.error("Ошибка в get_event: %s", e)
    return None


async def set_status(event_id: int, status: str) -> None:
    try:
        async with get_client() as client:
            await client.execute("UPDATE events SET status = ? WHERE id = ?", (status, event_id))
    except Exception as e:
        logging.error("Ошибка в set_status: %s", e)
        raise


async def update_event(event_id: int, title: str, description: str, date: str, checklist=None):
    """При обновлении: для «Магазин» статус остаётся accepted, для остальных — сбрасывается в pending."""
    new_status = "accepted" if title == "Магазин" else "pending"
    try:
        async with get_client() as client:
            if checklist is not None:
                checklist_json = json.dumps(checklist, ensure_ascii=False)
                await client.execute(
                    "UPDATE events SET title = ?, description = ?, date = ?, checklist = ?, status = ? WHERE id = ?",
                    (title, description, date, checklist_json, new_status, event_id),
                )
            else:
                await client.execute(
                    "UPDATE events SET title = ?, description = ?, date = ?, status = ? WHERE id = ?",
                    (title, description, date, new_status, event_id),
                )
            return await get_event(event_id)
    except Exception as e:
        logging.error("Ошибка в update_event: %s", e)
        raise


async def update_checklist(event_id: int, checklist: list) -> dict:
    """Обновляет только чеклист, не сбрасывая статус."""
    try:
        checklist_json = json.dumps(checklist, ensure_ascii=False)
        async with get_client() as client:
            await client.execute(
                "UPDATE events SET checklist = ? WHERE id = ?",
                (checklist_json, event_id),
            )
            return await get_event(event_id)
    except Exception as e:
        logging.error("Ошибка в update_checklist: %s", e)
        raise


async def delete_event(event_id: int) -> None:
    try:
        async with get_client() as client:
            await client.execute("DELETE FROM events WHERE id = ?", (event_id,))
    except Exception as e:
        logging.error("Ошибка в delete_event: %s", e)
        raise
