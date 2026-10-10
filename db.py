"""Слой работы с БД: готовые идеи для секса читаются напрямую из sex_ideas.json."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

log = logging.getLogger(__name__)

TURSO_URL = os.getenv("TURSO_URL", "").strip()
TURSO_TOKEN = os.getenv("TURSO_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "calendar.db")

INVITE_TTL = 7 * 24 * 3600
MAX_ITEMS_PER_EVENT = 50
MAX_EVENTS_PER_USER = 10000

# --- Архив удалённых идей (только для ручного просмотра файла) ---
ARCHIVE_KEY = "_архив"
CUSTOM_ARCHIVE_TITLE = "Свои идеи"
JSON_PATH = Path(__file__).parent / "sex_ideas.json"


class DbError(Exception):
    pass


@dataclass
class Result:
    rows: list = field(default_factory=list)
    affected: int = 0
    last_id: int | None = None


def _encode(value: Any) -> dict:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    return {"type": "text", "value": str(value)}


def _decode(value: dict) -> Any:
    kind = value.get("type")
    if kind == "null":
        return None
    if kind == "integer":
        return int(value["value"])
    if kind == "float":
        return float(value["value"])
    return value.get("value")


def _stmt(sql: str, args: Sequence[Any] = ()) -> dict:
    return {"sql": sql, "args": [_encode(a) for a in args]}


def _parse_result(res: dict) -> Result:
    rows = [tuple(_decode(v) for v in row) for row in res.get("rows", [])]
    last = res.get("last_insert_rowid")
    return Result(
        rows=rows,
        affected=int(res.get("affected_row_count") or 0),
        last_id=int(last) if last is not None else None,
    )


def _check_item(item: dict) -> dict:
    if item.get("type") == "error":
        raise DbError(item.get("error", {}).get("message", "ошибка Turso"))
    return item["response"]["result"]


class _TursoBackend:
    def __init__(self, url: str, token: str):
        url = url.strip().rstrip("/")
        url = "https://" + (url.split("://", 1)[1] if "://" in url else url)
        self._endpoint = url + "/v2/pipeline"
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self._session = None

    async def start(self) -> None:
        import aiohttp
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def _pipeline(self, requests: list, retries: int = 0) -> list:
        import aiohttp
        payload = {"requests": requests + [{"type": "close"}]}
        for attempt in range(retries + 1):
            try:
                async with self._session.post(
                    self._endpoint, json=payload, headers=self._headers
                ) as resp:
                    status, body = resp.status, await resp.text()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                err = DbError(f"Нет связи с Turso: {e!r}")
            else:
                if status == 200:
                    try:
                        return json.loads(body)["results"]
                    except (ValueError, KeyError) as e:
                        raise DbError("Некорректный ответ Turso") from e
                err = DbError(f"Turso вернул HTTP {status}: {body[:300]}")
                if status < 500:
                    raise err
            if attempt < retries:
                await asyncio.sleep(0.4 * (attempt + 1))
                continue
            raise err
        raise DbError("unreachable")

    async def execute(self, sql: str, args: Sequence[Any] = ()) -> Result:
        retries = 2 if sql.lstrip()[:6].upper() in ("SELECT", "PRAGMA") else 0
        results = await self._pipeline(
            [{"type": "execute", "stmt": _stmt(sql, args)}], retries
        )
        return _parse_result(_check_item(results[0]))

    async def batch(self, stmts: list[tuple[str, Sequence[Any]]]) -> list[Result]:
        if not stmts:
            return []
        n = len(stmts)
        steps = [{"stmt": _stmt("BEGIN")}]
        for i, (sql, args) in enumerate(stmts):
            steps.append({"stmt": _stmt(sql, args), "condition": {"type": "ok", "step": i}})
        steps.append({"stmt": _stmt("COMMIT"), "condition": {"type": "ok", "step": n}})
        steps.append(
            {
                "stmt": _stmt("ROLLBACK"),
                "condition": {"type": "not", "cond": {"type": "ok", "step": n + 1}},
            }
        )
        results = await self._pipeline([{"type": "batch", "batch": {"steps": steps}}])
        res = _check_item(results[0])
        step_results, step_errors = res["step_results"], res["step_errors"]
        if step_results[n + 1] is None:
            failed = next(((i, e) for i, e in enumerate(step_errors) if e), None)
            if failed is not None:
                log.error("Batch упал на шаге %s: %s", failed[0], failed[1].get("message"))
            msg = next((e["message"] for e in step_errors if e), "транзакция отменена")
            raise DbError(msg)
        return [_parse_result(step_results[i + 1]) for i in range(n)]


class _SqliteBackend:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        self._conn.close()

    def _run(self, sql: str, args: Sequence[Any]) -> Result:
        cur = self._conn.execute(sql, tuple(args))
        rows = cur.fetchall()
        return Result(rows=rows, affected=max(cur.rowcount, 0), last_id=cur.lastrowid)

    def _execute_sync(self, sql: str, args: Sequence[Any]) -> Result:
        with self._lock:
            try:
                return self._run(sql, args)
            except sqlite3.Error as e:
                raise DbError(str(e)) from e

    def _batch_sync(self, stmts) -> list[Result]:
        with self._lock:
            try:
                self._conn.execute("BEGIN")
                out = [self._run(sql, args) for sql, args in stmts]
                self._conn.execute("COMMIT")
                return out
            except sqlite3.Error as e:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise DbError(str(e)) from e

    async def execute(self, sql: str, args: Sequence[Any] = ()) -> Result:
        return await asyncio.to_thread(self._execute_sync, sql, args)

    async def batch(self, stmts) -> list[Result]:
        if not stmts:
            return []
        return await asyncio.to_thread(self._batch_sync, stmts)


_backend: _TursoBackend | _SqliteBackend | None = None


def _b():
    if _backend is None:
        raise DbError("База данных не инициализирована")
    return _backend


async def _exec(sql: str, args: Sequence[Any] = ()) -> Result:
    return await _b().execute(sql, args)


async def _batch(stmts) -> list[Result]:
    return await _b().batch(stmts)


async def init_db() -> None:
    global _backend
    if bool(TURSO_URL) != bool(TURSO_TOKEN):
        raise RuntimeError("Нужно задать обе переменные: TURSO_URL и TURSO_TOKEN (или ни одной)")
    if TURSO_URL:
        _backend = _TursoBackend(TURSO_URL, TURSO_TOKEN)
        log.info("БД: Turso (HTTP)")
    else:
        _backend = _SqliteBackend(DB_PATH)
        log.warning("БД: локальный файл %s", DB_PATH)
    await _backend.start()
    await _migrate()


async def close_db() -> None:
    global _backend
    if _backend is not None:
        await _backend.close()
        _backend = None


async def _columns(table: str) -> set[str]:
    rows = (await _exec(f"PRAGMA table_info({table})")).rows
    return {r[1] for r in rows}


async def _add_column(table: str, name: str, ddl: str) -> bool:
    if name in await _columns(table):
        return False
    await _exec(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    return True


async def _m1_base() -> None:
    await _exec(
        """
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            partner_id  INTEGER,
            invite_code TEXT NOT NULL UNIQUE
        )
        """
    )
    await _exec(
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
    await _add_column("events", "checklist", "TEXT NOT NULL DEFAULT '[]'")


async def _m2_features() -> None:
    await _add_column("users", "invite_expires", "INTEGER NOT NULL DEFAULT 0")
    await _add_column("users", "tz", "TEXT NOT NULL DEFAULT ''")
    await _add_column("events", "time", "TEXT NOT NULL DEFAULT ''")
    await _add_column("events", "category", "TEXT NOT NULL DEFAULT 'rest'")
    await _exec(
        """
        CREATE TABLE IF NOT EXISTS checklist_items (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            text     TEXT NOT NULL,
            checked  INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    await _exec(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            event_id INTEGER NOT NULL,
            user_id  INTEGER NOT NULL,
            PRIMARY KEY (event_id, user_id)
        )
        """
    )


async def _m3_sex_ideas() -> None:
    await _exec(
        """
        CREATE TABLE IF NOT EXISTS sex_ideas (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        INTEGER NOT NULL,
            category_title TEXT NOT NULL,
            text           TEXT NOT NULL
        )
        """
    )


async def _m4_ideas_source() -> None:
    await _add_column("sex_ideas", "source", "TEXT NOT NULL DEFAULT 'sex'")
    await _exec(
        "DELETE FROM sex_ideas WHERE category_title NOT IN "
        "('🔥 Страстная ночь', '🌹 Романтический вечер', '✨ Эксперименты и фантазии')"
    )


async def _m5_fix_checklist() -> None:
    await _add_column("checklist_items", "checked", "INTEGER NOT NULL DEFAULT 0")
    await _exec(
        """
        CREATE TABLE IF NOT EXISTS checklist_items (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            text     TEXT NOT NULL,
            checked  INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    await _add_column("events", "time", "TEXT NOT NULL DEFAULT ''")
    await _add_column("events", "category", "TEXT NOT NULL DEFAULT 'rest'")
    await _exec(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            event_id INTEGER NOT NULL,
            user_id  INTEGER NOT NULL,
            PRIMARY KEY (event_id, user_id)
        )
        """
    )


async def _m6_hidden_ideas() -> None:
    await _exec(
        """
        CREATE TABLE IF NOT EXISTS hidden_ideas (
            user_id   INTEGER NOT NULL,
            idea_key  TEXT NOT NULL,
            PRIMARY KEY (user_id, idea_key)
        )
        """
    )


MIGRATIONS = [
    (1, _m1_base),
    (2, _m2_features),
    (3, _m3_sex_ideas),
    (4, _m4_ideas_source),
    (5, _m5_fix_checklist),
    (6, _m6_hidden_ideas),
]


async def _migrate() -> None:
    await _exec("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    current = (await _exec("SELECT MAX(version) FROM schema_version")).rows[0][0] or 0
    for version, fn in MIGRATIONS:
        if version > current:
            log.info("Миграция БД → v%s", version)
            await fn()
            await _exec("INSERT INTO schema_version (version) VALUES (?)", (version,))


# ==================== sex_ideas.json: чтение/архив ====================

def _load_json_ideas_sync() -> dict:
    if not JSON_PATH.exists():
        return {}
    try:
        data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        log.exception("Не удалось прочитать sex_ideas.json")
        return {}


def _save_json_ideas_sync(data: dict) -> None:
    """Атомарно перезаписывает sex_ideas.json через временный файл."""
    tmp = JSON_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, JSON_PATH)


def _archive_idea_sync(category_title: str, text: str) -> None:
    """Дописывает идею в раздел _архив (без дублей). Синхронно."""
    data = _load_json_ideas_sync()
    archive = data.setdefault(ARCHIVE_KEY, {})
    if not isinstance(archive, dict):
        archive = {}
        data[ARCHIVE_KEY] = archive
    bucket = archive.setdefault(category_title, [])
    if not isinstance(bucket, list):
        bucket = []
        archive[category_title] = bucket
    if text not in bucket:
        bucket.append(text)
    _save_json_ideas_sync(data)


async def archive_idea(category_title: str, text: str) -> None:
    """Публичная асинхронная обёртка — из обработчиков API."""
    await asyncio.to_thread(_archive_idea_sync, category_title, text)


def read_json_archive_sync() -> dict:
    """Возвращает только _архив из sex_ideas.json (для /api/archive.json)."""
    data = _load_json_ideas_sync()
    archive = data.get(ARCHIVE_KEY, {})
    return archive if isinstance(archive, dict) else {}


# ==================== users ====================

USER_COLS = "telegram_id, partner_id, invite_code, invite_expires, tz"


def _user(r) -> dict | None:
    if not r:
        return None
    return {
        "telegram_id": r[0],
        "partner_id": r[1],
        "invite_code": r[2],
        "invite_expires": r[3] or 0,
        "tz": r[4] or "",
    }


def _new_code() -> str:
    return secrets.token_urlsafe(9)


async def get_user(telegram_id: int) -> dict | None:
    rs = await _exec(f"SELECT {USER_COLS} FROM users WHERE telegram_id = ?", (telegram_id,))
    return _user(rs.rows[0]) if rs.rows else None


async def get_or_create_user(telegram_id: int) -> dict:
    user = await get_user(telegram_id)
    if user:
        return user
    for _ in range(5):
        await _exec(
            "INSERT OR IGNORE INTO users (telegram_id, invite_code, invite_expires) VALUES (?, ?, ?)",
            (telegram_id, _new_code(), int(time.time()) + INVITE_TTL),
        )
        user = await get_user(telegram_id)
        if user:
            return user
    raise DbError("Не удалось создать пользователя")


async def get_user_by_code(code: str) -> dict | None:
    rs = await _exec(f"SELECT {USER_COLS} FROM users WHERE invite_code = ?", (code,))
    return _user(rs.rows[0]) if rs.rows else None


async def ensure_invite(user: dict) -> dict:
    if user["partner_id"] is not None or user["invite_expires"] > time.time():
        return user
    for _ in range(5):
        try:
            await _exec(
                "UPDATE users SET invite_code = ?, invite_expires = ? WHERE telegram_id = ?",
                (_new_code(), int(time.time()) + INVITE_TTL, user["telegram_id"]),
            )
        except DbError:
            continue
        return await get_user(user["telegram_id"])
    raise DbError("Не удалось обновить приглашение")


async def set_timezone(telegram_id: int, tz: str) -> None:
    await _exec("UPDATE users SET tz = ? WHERE telegram_id = ?", (tz, telegram_id))


async def link_partners(a: int, b: int) -> bool:
    rs = await _exec(
        "UPDATE users SET partner_id = CASE telegram_id WHEN ? THEN ? ELSE ? END "
        "WHERE telegram_id IN (?, ?) AND "
        "(SELECT COUNT(*) FROM users WHERE telegram_id IN (?, ?) AND partner_id IS NULL) = 2",
        (a, b, a, a, b, a, b),
    )
    return rs.affected == 2


async def unlink_partners(user_id: int) -> int | None:
    user = await get_user(user_id)
    if not user or user["partner_id"] is None:
        return None
    p = user["partner_id"]
    pair = "(created_by = ? AND target_user = ?) OR (created_by = ? AND target_user = ?)"
    pargs = (user_id, p, p, user_id)
    await _batch(
        [
            (f"DELETE FROM checklist_items WHERE event_id IN (SELECT id FROM events WHERE {pair})", pargs),
            (f"DELETE FROM reminders WHERE event_id IN (SELECT id FROM events WHERE {pair})", pargs),
            (f"DELETE FROM events WHERE {pair}", pargs),
            ("UPDATE users SET partner_id = NULL, invite_expires = 0 WHERE telegram_id IN (?, ?)",
             (user_id, p)),
        ]
    )
    return p


# ==================== events ====================

EVENT_COLS = "id, created_by, target_user, title, description, date, time, category, status"


def _event(r) -> dict | None:
    if not r:
        return None
    return {
        "id": r[0],
        "created_by": r[1],
        "target_user": r[2],
        "title": r[3],
        "description": r[4],
        "date": r[5],
        "time": r[6] or "",
        "category": r[7],
        "status": r[8],
        "checklist": [],
    }


async def _attach_items(events: list[dict]) -> list[dict]:
    shop = [e for e in events if e["category"] == "shop"]
    if not shop:
        return events
    by_id = {e["id"]: e for e in shop}
    marks = ",".join("?" * len(by_id))
    rs = await _exec(
        f"SELECT id, event_id, text, checked FROM checklist_items "
        f"WHERE event_id IN ({marks}) ORDER BY id",
        tuple(by_id),
    )
    for item_id, event_id, text, checked in rs.rows:
        by_id[event_id]["checklist"].append({"id": item_id, "text": text, "checked": bool(checked)})
    return events


async def count_events_created(user_id: int) -> int:
    rs = await _exec("SELECT COUNT(*) FROM events WHERE created_by = ?", (user_id,))
    return rs.rows[0][0]


async def create_event(created_by, target_user, title, description, date, event_time, category, items=None):
    initial_status = "accepted" if category == "shop" else "pending"
    rs = await _exec(
        "INSERT INTO events (created_by, target_user, title, description, date, time, category, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (created_by, target_user, title, description, date, event_time, category, initial_status),
    )
    event_id = rs.last_id
    if items and category == "shop":
        try:
            await _batch(
                [("INSERT INTO checklist_items (event_id, text, checked) VALUES (?, ?, ?)",
                  (event_id, it["text"], int(it["checked"]))) for it in items]
            )
        except DbError:
            await _exec("DELETE FROM events WHERE id = ?", (event_id,))
            raise
    return await get_event(event_id)


async def list_events_range(user_id: int, start: str, end: str) -> list[dict]:
    rs = await _exec(
        f"SELECT {EVENT_COLS} FROM events "
        "WHERE (created_by = ? OR target_user = ?) AND date >= ? AND date < ? "
        "ORDER BY date, time, id",
        (user_id, user_id, start, end),
    )
    return await _attach_items([_event(r) for r in rs.rows])


async def get_event(event_id: int) -> dict | None:
    rs = await _exec(f"SELECT {EVENT_COLS} FROM events WHERE id = ?", (event_id,))
    if not rs.rows:
        return None
    return (await _attach_items([_event(rs.rows[0])]))[0]


async def set_status(event_id: int, status: str) -> None:
    await _exec("UPDATE events SET status = ? WHERE id = ?", (status, event_id))


async def update_event(event_id, title, description, date, event_time, category, reset_status: bool):
    stmts = [
        (
            "UPDATE events SET title = ?, description = ?, date = ?, time = ?, category = ?, "
            "status = CASE WHEN ? THEN 'pending' ELSE status END WHERE id = ?",
            (title, description, date, event_time, category, int(reset_status), event_id),
        )
    ]
    if reset_status:
        stmts.append(("DELETE FROM reminders WHERE event_id = ?", (event_id,)))
    if category != "shop":
        stmts.append(("DELETE FROM checklist_items WHERE event_id = ?", (event_id,)))
    await _batch(stmts)
    return await get_event(event_id)


async def delete_event(event_id: int) -> None:
    await _batch(
        [
            ("DELETE FROM checklist_items WHERE event_id = ?", (event_id,)),
            ("DELETE FROM reminders WHERE event_id = ?", (event_id,)),
            ("DELETE FROM events WHERE id = ?", (event_id,)),
        ]
    )


async def add_item(event_id: int, text: str) -> bool:
    rs = await _exec(
        "INSERT INTO checklist_items (event_id, text, checked) "
        "SELECT ?, ?, 0 WHERE (SELECT COUNT(*) FROM checklist_items WHERE event_id = ?) < ?",
        (event_id, text, event_id, MAX_ITEMS_PER_EVENT),
    )
    return rs.affected == 1


async def set_item_checked(event_id: int, item_id: int, checked: bool) -> bool:
    rs = await _exec(
        "UPDATE checklist_items SET checked = ? WHERE id = ? AND event_id = ?",
        (int(checked), item_id, event_id),
    )
    return rs.affected == 1


async def delete_item(event_id: int, item_id: int) -> bool:
    rs = await _exec("DELETE FROM checklist_items WHERE id = ? AND event_id = ?", (item_id, event_id))
    return rs.affected == 1


# ==================== sex_ideas ====================

async def _hidden_idea_keys(user_id: int, partner_id: int | None) -> set[str]:
    if partner_id:
        rs = await _exec(
            "SELECT DISTINCT idea_key FROM hidden_ideas WHERE user_id IN (?, ?)",
            (user_id, partner_id),
        )
    else:
        rs = await _exec(
            "SELECT idea_key FROM hidden_ideas WHERE user_id = ?", (user_id,)
        )
    return {r[0] for r in rs.rows}


async def get_sex_ideas(user_id: int, partner_id: int | None) -> list[dict]:
    ideas = []
    hidden = await _hidden_idea_keys(user_id, partner_id)

    # 1. sex_ideas.json, кроме служебного раздела _архив
    data = await asyncio.to_thread(_load_json_ideas_sync)
    idx = 1
    for category_title, texts in data.items():
        if category_title == ARCHIVE_KEY:
            continue
        if not isinstance(texts, list):
            continue
        for txt in texts:
            key = f"json_{idx}"
            idx += 1
            if key in hidden:
                continue
            ideas.append({"id": key, "category_title": category_title, "text": txt})

    # 2. Кастомные идеи (и свои, и партнёра)
    if partner_id:
        rs = await _exec(
            "SELECT id, category_title, text FROM sex_ideas "
            "WHERE source = 'sex' AND user_id IN (?, ?) ORDER BY id DESC",
            (user_id, partner_id)
        )
    else:
        rs = await _exec(
            "SELECT id, category_title, text FROM sex_ideas "
            "WHERE source = 'sex' AND user_id = ? ORDER BY id DESC",
            (user_id,)
        )
    for r in rs.rows:
        ideas.append({"id": r[0], "category_title": r[1], "text": r[2]})

    return ideas


async def add_sex_idea(user_id: int, category_title: str, text: str) -> int | None:
    rs = await _exec(
        "INSERT INTO sex_ideas (user_id, category_title, text, source) VALUES (?, ?, ?, 'sex')",
        (user_id, category_title, text)
    )
    return rs.last_id


async def delete_sex_idea(idea_id: int) -> bool:
    """Удаляет кастомную идею из БД и архивирует её текст в sex_ideas.json."""
    rs = await _exec(
        "SELECT category_title, text FROM sex_ideas WHERE id = ? AND source = 'sex'",
        (idea_id,),
    )
    if not rs.rows:
        return False
    _category_title, text = rs.rows[0]
    try:
        await archive_idea(CUSTOM_ARCHIVE_TITLE, text)
    except Exception:
        log.exception("Не удалось записать идею в архив (не критично)")
    rs2 = await _exec("DELETE FROM sex_ideas WHERE id = ? AND source = 'sex'", (idea_id,))
    return rs2.affected == 1


async def hide_json_idea_for_pair(user_id: int, partner_id: int | None, idea_key: str) -> None:
    """Прячет JSON-идею у обоих партнёров и архивирует её в sex_ideas.json."""
    # Находим исходный текст по ключу json_N, чтобы положить его в _архив
    try:
        data = _load_json_ideas_sync()
        idx = 1
        found_title = None
        found_text = None
        for category_title, texts in data.items():
            if category_title == ARCHIVE_KEY or not isinstance(texts, list):
                continue
            for txt in texts:
                key = f"json_{idx}"
                idx += 1
                if key == idea_key:
                    found_title, found_text = category_title, txt
                    break
            if found_text is not None:
                break
        if found_text is not None:
            await archive_idea(found_title, found_text)
    except Exception:
        log.exception("Не удалось архивировать JSON-идею (не критично)")

    rows = [(user_id, idea_key)]
    if partner_id and partner_id != user_id:
        rows.append((partner_id, idea_key))
    await _batch(
        [("INSERT OR IGNORE INTO hidden_ideas (user_id, idea_key) VALUES (?, ?)", r) for r in rows]
    )


# ==================== напоминания ====================

async def reminder_candidates(date_from: str, date_to: str) -> list[dict]:
    rs = await _exec(
        "SELECT e.id, e.title, e.date, e.time, u.telegram_id, u.tz "
        "FROM events e JOIN users u ON u.telegram_id IN (e.created_by, e.target_user) "
        "WHERE e.status = 'accepted' AND e.date BETWEEN ? AND ? "
        "AND NOT EXISTS (SELECT 1 FROM reminders r WHERE r.event_id = e.id AND r.user_id = u.telegram_id)",
        (date_from, date_to),
    )
    return [
        {"event_id": r[0], "title": r[1], "date": r[2], "time": r[3] or "", "user_id": r[4], "tz": r[5] or ""}
        for r in rs.rows
    ]


async def mark_reminded(event_id: int, user_id: int) -> bool:
    rs = await _exec(
        "INSERT OR IGNORE INTO reminders (event_id, user_id) VALUES (?, ?)", (event_id, user_id)
    )
    return rs.affected == 1


async def purge_old_reminders(date_before: str) -> int:
    rs = await _exec(
        "DELETE FROM reminders WHERE event_id IN "
        "(SELECT id FROM events WHERE date < ?)",
        (date_before,),
    )
    return rs.affected


# ==================== статистика ====================

async def pair_stats(user_id: int, partner_id: int) -> dict:
    pair = "(created_by = ? AND target_user = ?) OR (created_by = ? AND target_user = ?)"
    pargs = (user_id, partner_id, partner_id, user_id)

    rs_total = await _exec(f"SELECT COUNT(*) FROM events WHERE {pair}", pargs)
    total = rs_total.rows[0][0] if rs_total.rows else 0

    rs_status = await _exec(
        f"SELECT status, COUNT(*) FROM events WHERE {pair} GROUP BY status",
        pargs,
    )
    by_status = {row[0]: row[1] for row in rs_status.rows}

    rs_cat = await _exec(
        f"SELECT category, COUNT(*) FROM events WHERE {pair} GROUP BY category",
        pargs,
    )
    by_category = {row[0]: row[1] for row in rs_cat.rows}

    return {
        "total": total,
        "pending": by_status.get("pending", 0),
        "accepted": by_status.get("accepted", 0),
        "declined": by_status.get("declined", 0),
        "by_category": by_category,
    }
