"""Слой работы с БД."""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import importlib
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

try:
    aiohttp: Any = importlib.import_module("aiohttp")
except ModuleNotFoundError:
    aiohttp = None

log = logging.getLogger(__name__)

TURSO_URL = os.getenv("TURSO_URL", "").strip()
TURSO_TOKEN = os.getenv("TURSO_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "calendar.db")

INVITE_TTL = 7 * 24 * 3600
MAX_ITEMS_PER_EVENT = 50
MAX_EVENTS_PER_USER = 10000

ARCHIVE_KEY = "_архив"
CUSTOM_ARCHIVE_TITLE = "Свои идеи"

TODO_COLORS = [
    "#ef4444", "#f97316", "#eab308", "#22c55e",
    "#14b8a6", "#3b82f6", "#8b5cf6", "#ec4899",
]


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
        self._session: Any = None

    async def start(self) -> None:
        if aiohttp is None:
            raise RuntimeError("The aiohttp package is required when using Turso")
        timeout = aiohttp.ClientTimeout(total=20, connect=10)
        self._session = aiohttp.ClientSession(
            headers=self._headers,
            timeout=timeout,
            connector=aiohttp.TCPConnector(limit=50, ttl_dns_cache=300),
        )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _pipeline(self, requests: list, retries: int = 0) -> list:
        if self._session is None or self._session.closed:
            raise DbError("HTTP-сессия Turso не инициализирована")

        payload = {"requests": requests + [{"type": "close"}]}
        for attempt in range(retries + 1):
            try:
                async with self._session.post(self._endpoint, json=payload) as resp:
                    status = resp.status
                    body = await resp.text()
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
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")

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
    _json_ideas()
    await _migrate()
    try:
        cols = await _columns("events")
        log.info("events колонки после миграций: %s", sorted(cols))
    except Exception:
        log.exception("Не удалось прочитать PRAGMA table_info(events)")


async def close_db() -> None:
    global _backend
    if _backend is not None:
        await _backend.close()
        _backend = None

_ALLOWED_TABLES = {
    "users", "events", "checklist_items", "reminders", "sex_ideas",
    "hidden_ideas", "archived_ideas", "reactions", "idea_responses",
    "user_challenges", "challenge_messages", "shop_items",
    "event_comments", "moods", "pair_rev", "schema_version",
}

_ALLOWED_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_identifier(value: str) -> str:
    if value not in _ALLOWED_TABLES and not _ALLOWED_IDENTIFIER.fullmatch(value):
        raise DbError(f"Недопустимый идентификатор: {value}")
    return value


async def _columns(table: str) -> set[str]:
    table = _validate_identifier(table)
    rows = (await _exec(f"PRAGMA table_info({table})")).rows
    return {r[1] for r in rows}


async def _add_column(table: str, name: str, ddl: str) -> bool:
    table = _validate_identifier(table)
    name = _validate_identifier(name)
    if name in await _columns(table):
        return False
    await _exec(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    return True


async def _m1_base() -> None:
    await _exec("CREATE TABLE IF NOT EXISTS users (telegram_id INTEGER PRIMARY KEY, partner_id INTEGER, invite_code TEXT NOT NULL UNIQUE)")
    await _exec("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, created_by INTEGER NOT NULL, target_user INTEGER NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', date TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'declined')), checklist TEXT NOT NULL DEFAULT '[]')")
    await _add_column("events", "checklist", "TEXT NOT NULL DEFAULT '[]'")


async def _m2_features() -> None:
    await _add_column("users", "invite_expires", "INTEGER NOT NULL DEFAULT 0")
    await _add_column("users", "tz", "TEXT NOT NULL DEFAULT ''")
    await _add_column("events", "time", "TEXT NOT NULL DEFAULT ''")
    await _add_column("events", "category", "TEXT NOT NULL DEFAULT 'rest'")
    await _exec("CREATE TABLE IF NOT EXISTS checklist_items (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL, text TEXT NOT NULL, checked INTEGER NOT NULL DEFAULT 0)")
    await _exec("CREATE TABLE IF NOT EXISTS reminders (event_id INTEGER NOT NULL, user_id INTEGER NOT NULL, PRIMARY KEY (event_id, user_id))")


async def _m3_sex_ideas() -> None:
    await _exec("CREATE TABLE IF NOT EXISTS sex_ideas (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, category_title TEXT NOT NULL, text TEXT NOT NULL)")


async def _m4_ideas_source() -> None:
    await _add_column("sex_ideas", "source", "TEXT NOT NULL DEFAULT 'sex'")
    await _exec("DELETE FROM sex_ideas WHERE category_title NOT IN ('🔥 Страстная ночь', '🌹 Романтический вечер', '✨ Эксперименты и фантазии')")


async def _m5_fix_checklist() -> None:
    await _add_column("checklist_items", "checked", "INTEGER NOT NULL DEFAULT 0")
    await _exec("CREATE TABLE IF NOT EXISTS checklist_items (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL, text TEXT NOT NULL, checked INTEGER NOT NULL DEFAULT 0)")
    await _add_column("events", "time", "TEXT NOT NULL DEFAULT ''")
    await _add_column("events", "category", "TEXT NOT NULL DEFAULT 'rest'")
    await _exec("CREATE TABLE IF NOT EXISTS reminders (event_id INTEGER NOT NULL, user_id INTEGER NOT NULL, PRIMARY KEY (event_id, user_id))")


async def _m6_hidden_ideas() -> None:
    await _exec("CREATE TABLE IF NOT EXISTS hidden_ideas (user_id INTEGER NOT NULL, idea_key TEXT NOT NULL, PRIMARY KEY (user_id, idea_key))")


async def _m7_archived_ideas() -> None:
    await _exec("CREATE TABLE IF NOT EXISTS archived_ideas (id INTEGER PRIMARY KEY AUTOINCREMENT, category_title TEXT NOT NULL, text TEXT NOT NULL, source TEXT NOT NULL, archived_by INTEGER, archived_at INTEGER NOT NULL)")
    await _exec("CREATE UNIQUE INDEX IF NOT EXISTS uq_archived_ideas ON archived_ideas (category_title, text, source)")


async def _m8_todo() -> None:
    await _add_column("events", "color", "TEXT NOT NULL DEFAULT ''")
    await _add_column("events", "priority", "TEXT NOT NULL DEFAULT ''")


async def _m9_reactions() -> None:
    await _add_column("events", "reaction", "TEXT NOT NULL DEFAULT ''")
    await _add_column("events", "reaction_by", "INTEGER")
    await _add_column("events", "decline_comment", "TEXT NOT NULL DEFAULT ''")


async def _m10_two_comments() -> None:
    await _add_column("events", "decline_comment_creator", "TEXT NOT NULL DEFAULT ''")
    await _add_column("events", "decline_comment_target", "TEXT NOT NULL DEFAULT ''")


async def _m11_reactions_table() -> None:
    await _exec("CREATE TABLE IF NOT EXISTS reactions (event_id INTEGER NOT NULL, user_id INTEGER NOT NULL, emoji TEXT NOT NULL, PRIMARY KEY (event_id, user_id))")


async def _m12_todo_remind_before() -> None:
    await _add_column("events", "remind_before", "INTEGER NOT NULL DEFAULT 0")


async def _m13_idea_responses() -> None:
    await _exec("CREATE TABLE IF NOT EXISTS idea_responses (event_id INTEGER NOT NULL, idea_idx INTEGER NOT NULL, status TEXT NOT NULL, user_id INTEGER NOT NULL, ts INTEGER NOT NULL, PRIMARY KEY (event_id, idea_idx))")


async def _m14_checklist_position() -> None:
    await _add_column("checklist_items", "position", "INTEGER NOT NULL DEFAULT 0")
    await _exec("UPDATE checklist_items SET position = id WHERE position = 0")


async def _m15_personal_section() -> None:
    await _add_column("events", "date_end", "TEXT NOT NULL DEFAULT ''")
    await _add_column("events", "visibility", "TEXT NOT NULL DEFAULT 'shared'")
    await _exec("CREATE INDEX IF NOT EXISTS idx_events_personal ON events (created_by, category, date)")


async def _m16_indexes_and_idea_keys() -> None:
    await _exec("CREATE INDEX IF NOT EXISTS idx_events_created ON events (created_by, date)")
    await _exec("CREATE INDEX IF NOT EXISTS idx_events_target ON events (target_user, date)")
    await _exec("CREATE INDEX IF NOT EXISTS idx_checklist_event ON checklist_items (event_id)")
    raw = _read_json_ideas_raw()
    rs = await _exec("SELECT user_id, idea_key FROM hidden_ideas")
    stmts = []
    for user_id, key in rs.rows:
        m = re.fullmatch(r"json_(\d+)", key)
        if not m:
            continue
        n = int(m.group(1))
        if 1 <= n <= len(raw):
            stmts.append(("INSERT OR IGNORE INTO hidden_ideas (user_id, idea_key) VALUES (?, ?)", (user_id, _idea_key(*raw[n - 1]))))
        stmts.append(("DELETE FROM hidden_ideas WHERE user_id = ? AND idea_key = ?", (user_id, key)))
    await _batch(stmts)


async def _m16_add_done_status() -> None:
    try:
        rs = await _exec("SELECT sql FROM sqlite_master WHERE type='table' AND name='events'")
        ddl = (rs.rows[0][0] or "") if rs.rows else ""
        if "'done'" in ddl or '"done"' in ddl:
            return
    except Exception:
        pass
    await _exec("DROP TABLE IF EXISTS events_new")
    await _exec("""
        CREATE TABLE events_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_by INTEGER NOT NULL, target_user INTEGER NOT NULL,
            title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', date TEXT NOT NULL,
            date_end TEXT NOT NULL DEFAULT '', time TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT 'rest',
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'declined', 'done')),
            checklist TEXT NOT NULL DEFAULT '[]', color TEXT NOT NULL DEFAULT '', priority TEXT NOT NULL DEFAULT '',
            reaction TEXT NOT NULL DEFAULT '', reaction_by INTEGER, decline_comment TEXT NOT NULL DEFAULT '',
            decline_comment_creator TEXT NOT NULL DEFAULT '', decline_comment_target TEXT NOT NULL DEFAULT '',
            remind_before INTEGER NOT NULL DEFAULT 0, visibility TEXT NOT NULL DEFAULT 'shared'
        )
    """)
    await _batch([
        ("INSERT INTO events_new (id, created_by, target_user, title, description, date, date_end, time, category, status, checklist, color, priority, reaction, reaction_by, decline_comment, decline_comment_creator, decline_comment_target, remind_before, visibility) SELECT id, created_by, target_user, title, description, date, date_end, time, category, status, checklist, color, priority, reaction, reaction_by, decline_comment, decline_comment_creator, decline_comment_target, remind_before, visibility FROM events", ()),
        ("DROP TABLE events", ()),
        ("ALTER TABLE events_new RENAME TO events", ()),
    ])


async def _m17_challenges() -> None:
    await _add_column("users", "balance", "INTEGER NOT NULL DEFAULT 0")
    await _exec("CREATE TABLE IF NOT EXISTS user_challenges (id INTEGER PRIMARY KEY AUTOINCREMENT, creator_id INTEGER NOT NULL, target_id INTEGER NOT NULL, category TEXT NOT NULL, text TEXT NOT NULL, reward INTEGER NOT NULL DEFAULT 10, status TEXT NOT NULL DEFAULT 'pending', photo_id TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL)")


async def _m18_challenge_messages() -> None:
    await _exec("CREATE TABLE IF NOT EXISTS challenge_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, challenge_id INTEGER NOT NULL, sender_id INTEGER NOT NULL, text TEXT NOT NULL, created_at INTEGER NOT NULL)")


async def _m19_challenge_deadline() -> None:
    await _add_column("user_challenges", "deadline", "INTEGER NOT NULL DEFAULT 0")


async def _m20_shop_items() -> None:
    await _exec("CREATE TABLE IF NOT EXISTS shop_items (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, cost INTEGER NOT NULL, emoji TEXT DEFAULT '🎁')")
    rs = await _exec("SELECT COUNT(*) FROM shop_items")
    if rs.rows[0][0] == 0:
        await _exec("INSERT INTO shop_items (title, cost, emoji) VALUES (?, ?, ?)", ("Массаж", 50, "💆"))
        await _exec("INSERT INTO shop_items (title, cost, emoji) VALUES (?, ?, ?)", ("Ужин в ресторане", 150, "🍷"))
        await _exec("INSERT INTO shop_items (title, cost, emoji) VALUES (?, ?, ?)", ("Поход в кино", 80, "🍿"))


async def _m21_event_comments() -> None:
    await _exec("CREATE TABLE IF NOT EXISTS event_comments (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL, user_id INTEGER NOT NULL, text TEXT NOT NULL, created_at INTEGER NOT NULL)")


async def _m22_last_viewed() -> None:
    await _add_column("users", "last_viewed_challenges", "INTEGER NOT NULL DEFAULT 0")


async def _m23_performance_indexes() -> None:
    await _exec("CREATE INDEX IF NOT EXISTS idx_events_created ON events (created_by, date)")
    await _exec("CREATE INDEX IF NOT EXISTS idx_events_target ON events (target_user, date)")
    await _exec("CREATE INDEX IF NOT EXISTS idx_challenges_target_created ON user_challenges (target_id, created_at)")
    await _exec("CREATE INDEX IF NOT EXISTS idx_challenges_creator_created ON user_challenges (creator_id, created_at)")
    await _exec("CREATE INDEX IF NOT EXISTS idx_challenge_messages_challenge_created ON challenge_messages (challenge_id, created_at)")
    await _exec("CREATE INDEX IF NOT EXISTS idx_event_comments_event_created ON event_comments (event_id, created_at)")


async def _m24_moods() -> None:
    await _exec("CREATE TABLE IF NOT EXISTS moods (pair_key TEXT NOT NULL, user_id INTEGER NOT NULL, date TEXT NOT NULL, emoji TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL, PRIMARY KEY (pair_key, user_id, date))")
    await _exec("CREATE INDEX IF NOT EXISTS idx_moods_pair_date ON moods (pair_key, date)")


async def _m25_alt_time() -> None:
    await _add_column("events", "alt_date", "TEXT NOT NULL DEFAULT ''")
    await _add_column("events", "alt_time", "TEXT NOT NULL DEFAULT ''")


async def _m26_fix_schema() -> None:
    await _exec("DROP TABLE IF EXISTS events_new")
    try:
        cols = await _columns("events")
    except DbError:
        return
    expected = {"date_end": "TEXT NOT NULL DEFAULT ''", "visibility": "TEXT NOT NULL DEFAULT 'shared'", "alt_date": "TEXT NOT NULL DEFAULT ''", "alt_time": "TEXT NOT NULL DEFAULT ''"}
    for name, ddl in expected.items():
        if name not in cols:
            try:
                await _exec(f"ALTER TABLE events ADD COLUMN {name} {ddl}")
            except DbError:
                pass


async def _m28_soft_unlink() -> None:
    await _add_column("users", "unlinked_at", "INTEGER NOT NULL DEFAULT 0")


async def _m29_pair_rev() -> None:
    await _exec("CREATE TABLE IF NOT EXISTS pair_rev (pair_key TEXT PRIMARY KEY, rev INTEGER NOT NULL DEFAULT 1)")


async def _m30_reliable_unlink() -> None:
    await _add_column("users", "previous_partner_id", "INTEGER")

    rs = await _exec(
        """
        SELECT telegram_id, unlinked_at
        FROM users
        WHERE unlinked_at > 0 AND partner_id IS NULL
        """
    )

    stmts = []

    for user_id, unlinked_at in rs.rows:
        stmts.append((
            """
            UPDATE users
            SET previous_partner_id = (
                SELECT u2.telegram_id
                FROM users u2
                WHERE u2.unlinked_at = ?
                  AND u2.telegram_id != ?
                LIMIT 1
            )
            WHERE telegram_id = ?
            """,
            (unlinked_at, user_id, user_id),
        ))

    if stmts:
        await _batch(stmts)


async def _m31_archived_at() -> None:
    await _add_column("events", "archived_at", "INTEGER NOT NULL DEFAULT 0")


async def _m32_comment_photos() -> None:
    await _add_column("event_comments", "photo_id", "TEXT NOT NULL DEFAULT ''")
    await _add_column("challenge_messages", "photo_id", "TEXT NOT NULL DEFAULT ''")


MIGRATIONS = [
    (1, _m1_base), (2, _m2_features), (3, _m3_sex_ideas), (4, _m4_ideas_source), (5, _m5_fix_checklist),
    (6, _m6_hidden_ideas), (7, _m7_archived_ideas), (8, _m8_todo), (9, _m9_reactions), (10, _m10_two_comments),
    (11, _m11_reactions_table), (12, _m12_todo_remind_before), (13, _m13_idea_responses), (14, _m14_checklist_position),
    (15, _m15_personal_section), (16, _m16_indexes_and_idea_keys), (17, _m17_challenges), (18, _m18_challenge_messages),
    (19, _m19_challenge_deadline), (20, _m20_shop_items), (21, _m21_event_comments), (22, _m22_last_viewed),
    (23, _m23_performance_indexes), (24, _m24_moods), (25, _m25_alt_time), (26, _m26_fix_schema),
    (27, _m16_add_done_status), (28, _m28_soft_unlink), (29, _m29_pair_rev),
    (30, _m30_reliable_unlink), (31, _m31_archived_at), (32, _m32_comment_photos),
]

_REQUIRED_COLUMNS = {15: [("events", "date_end", "TEXT NOT NULL DEFAULT ''"), ("events", "visibility", "TEXT NOT NULL DEFAULT 'shared'")]}


async def _ensure_required_columns() -> None:
    for version, cols in _REQUIRED_COLUMNS.items():
        for table, name, ddl in cols:
            try:
                exists = name in await _columns(table)
            except DbError:
                continue
            if exists:
                continue
            try:
                await _exec(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            except DbError:
                pass
    try:
        await _exec("CREATE INDEX IF NOT EXISTS idx_events_personal ON events (created_by, category, date)")
    except DbError:
        pass


async def _migrate() -> None:
    await _exec("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    current = (await _exec("SELECT MAX(version) FROM schema_version")).rows[0][0] or 0
    log.info("Текущая версия схемы: %s", current)
    try:
        cols = await _columns("events")
        if "alt_date" not in cols and current >= 26:
            log.warning("ВНИМАНИЕ: база на версии %s, но колонки alt_date нет! Откатываю до 25.", current)
            await _exec("DELETE FROM schema_version WHERE version >= 26")
            current = 25
    except Exception:
        pass

    for version, fn in MIGRATIONS:
        if version > current:
            log.info("Миграция БД → v%s", version)
            await fn()
            await _exec("INSERT INTO schema_version (version) VALUES (?)", (version,))
    await _ensure_required_columns()


def _idea_key(category: str, text: str) -> str:
    return "json_h" + hashlib.sha1(f"{category}\n{text}".encode("utf-8")).hexdigest()[:12]


def _read_json_ideas_raw() -> list[tuple[str, str]]:
    path = Path(__file__).parent / "sex_ideas.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    out = []
    for category, texts in data.items():
        if category == ARCHIVE_KEY or not isinstance(texts, list):
            continue
        out.extend((category, t) for t in texts if isinstance(t, str))
    return out


_ideas_cache: list[dict] | None = None


def _json_ideas() -> list[dict]:
    global _ideas_cache
    if _ideas_cache is None:
        raw = _read_json_ideas_raw()
        last = {t.strip().lower(): i for i, (_, t) in enumerate(raw)}
        _ideas_cache = [{"id": _idea_key(c, t), "category_title": c, "text": t} for i, (c, t) in enumerate(raw) if last[t.strip().lower()] == i]
    return _ideas_cache

# --- Кэш пользователя (LRU с TTL) ---
_user_cache: dict[int, tuple[float, dict]] = {}
_USER_CACHE_TTL = 30
_USER_CACHE_MAX = 5000


def _invalidate_user_cache(*uids: int) -> None:
    for uid in uids:
        _user_cache.pop(uid, None)


def _user_cache_put(uid: int, value: dict) -> None:
    if len(_user_cache) >= _USER_CACHE_MAX:
        now = time.monotonic()
        stale = [k for k, v in _user_cache.items() if now - v[0] > _USER_CACHE_TTL]
        for k in stale:
            _user_cache.pop(k, None)
        if len(_user_cache) >= _USER_CACHE_MAX:
            _user_cache.clear()
    _user_cache[uid] = (time.monotonic(), value)


USER_COLS = "telegram_id, partner_id, invite_code, invite_expires, tz, unlinked_at, previous_partner_id"


def _user(r) -> dict | None:
    if not r:
        return None
    return {
        "telegram_id": r[0],
        "partner_id": r[1],
        "invite_code": r[2],
        "invite_expires": r[3] or 0,
        "tz": r[4] or "",
        "unlinked_at": r[5] or 0,
        "previous_partner_id": r[6],
    }


def _new_code() -> str:
    return secrets.token_urlsafe(9)


def pair_key(a: int, b: int) -> str:
    x, y = sorted((int(a), int(b)))
    return f"{x}:{y}"


async def get_user(telegram_id: int) -> dict | None:
    now = time.monotonic()
    hit = _user_cache.get(telegram_id)
    if hit and now - hit[0] < _USER_CACHE_TTL:
        return hit[1]
    rs = await _exec(f"SELECT {USER_COLS} FROM users WHERE telegram_id = ?", (telegram_id,))
    user = _user(rs.rows[0]) if rs.rows else None
    if user:
        _user_cache_put(telegram_id, user)
    return user


async def get_or_create_user(telegram_id: int) -> dict:
    invite_expires = int(time.time()) + INVITE_TTL

    for _ in range(5):
        await _exec(
            """
            INSERT OR IGNORE INTO users
                (telegram_id, invite_code, invite_expires)
            VALUES (?, ?, ?)
            """,
            (telegram_id, _new_code(), invite_expires),
        )

        user = await get_user(telegram_id)

        if user:
            _invalidate_user_cache(telegram_id)
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
        _invalidate_user_cache(user["telegram_id"])
        return await get_user(user["telegram_id"])
    raise DbError("Не удалось обновить приглашение")


async def set_timezone(telegram_id: int, tz: str) -> None:
    await _exec("UPDATE users SET tz = ? WHERE telegram_id = ?", (tz, telegram_id))
    _invalidate_user_cache(telegram_id)


async def link_partners(a: int, b: int) -> bool:
    rs = await _exec(
        "UPDATE users SET partner_id = CASE telegram_id WHEN ? THEN ? ELSE ? END "
        "WHERE telegram_id IN (?, ?) AND "
        "(SELECT COUNT(*) FROM users WHERE telegram_id IN (?, ?) AND partner_id IS NULL) = 2",
        (a, b, a, a, b, a, b),
    )
    if rs.affected == 2:
        _invalidate_user_cache(a, b)
    return rs.affected == 2


async def unlink_partners(user_id: int) -> int | None:
    user = await get_user(user_id)
    if not user or user["partner_id"] is None:
        return None

    partner_id = user["partner_id"]
    now = int(time.time())

    await _exec(
        """
        UPDATE users
        SET partner_id = NULL,
            invite_expires = 0,
            unlinked_at = ?,
            previous_partner_id = CASE
                WHEN telegram_id = ? THEN ?
                ELSE ?
            END
        WHERE telegram_id IN (?, ?)
        """,
        (now, user_id, partner_id, user_id, user_id, partner_id),
    )

    _invalidate_user_cache(user_id, partner_id)
    return partner_id


async def restore_partners(user_id: int) -> int | None:
    user = await get_user(user_id)
    if not user or not user.get("unlinked_at"):
        return None

    partner_id = user.get("previous_partner_id")
    if not partner_id:
        return None

    partner = await get_user(partner_id)

    if (
        not partner
        or not partner.get("unlinked_at")
        or partner.get("previous_partner_id") != user_id
    ):
        return None

    await _exec(
        """
        UPDATE users
        SET partner_id = CASE
                WHEN telegram_id = ? THEN ?
                ELSE ?
            END,
            unlinked_at = 0,
            previous_partner_id = NULL
        WHERE telegram_id IN (?, ?)
        """,
        (user_id, partner_id, user_id, user_id, partner_id),
    )

    _invalidate_user_cache(user_id, partner_id)
    return partner_id


async def finalize_unlink(user_id: int) -> None:
    user = await get_user(user_id)
    if not user or not user.get("unlinked_at"):
        return

    p = user.get("previous_partner_id")

    pair = "(created_by = ? AND target_user = ?) OR (created_by = ? AND target_user = ?)"
    pargs = (user_id, p, p, user_id) if p else (user_id, user_id, user_id, user_id)
    pk = pair_key(user_id, p) if p else ""

    stmts = [
        (f"DELETE FROM checklist_items WHERE event_id IN (SELECT id FROM events WHERE {pair})", pargs),
        (f"DELETE FROM reminders WHERE event_id IN (SELECT id FROM events WHERE {pair})", pargs),
        (f"DELETE FROM reactions WHERE event_id IN (SELECT id FROM events WHERE {pair})", pargs),
        (f"DELETE FROM idea_responses WHERE event_id IN (SELECT id FROM events WHERE {pair})", pargs),
        (f"DELETE FROM event_comments WHERE event_id IN (SELECT id FROM events WHERE {pair})", pargs),
        (f"DELETE FROM events WHERE {pair}", pargs),
    ]
    if p:
        stmts.extend([
            ("DELETE FROM challenge_messages WHERE challenge_id IN (SELECT id FROM user_challenges WHERE creator_id IN (?, ?) OR target_id IN (?, ?))", (user_id, p, user_id, p)),
            ("DELETE FROM user_challenges WHERE creator_id IN (?, ?) OR target_id IN (?, ?)", (user_id, p, user_id, p)),
            ("DELETE FROM moods WHERE pair_key = ?", (pk,)),
        ])
    await _batch(stmts)
    if p:
        await _exec(
            """
            UPDATE users
            SET unlinked_at = 0, previous_partner_id = NULL
            WHERE telegram_id IN (?, ?)
            """,
            (user_id, p),
        )
    else:
        await _exec(
            """
            UPDATE users
            SET unlinked_at = 0, previous_partner_id = NULL
            WHERE telegram_id = ?
            """,
            (user_id,),
        )
    _invalidate_user_cache(user_id)
    if p:
        _invalidate_user_cache(p)


# Держатель «последнего rev» на время одного HTTP-запроса. Middleware кладёт сюда dict,
# любой bump_pair_rev записывает в него новый rev, и middleware отдаёт его заголовком X-Rev.
# Клиент по нему понимает, что изменение его собственное, и не перекачивает весь /state.
rev_holder: contextvars.ContextVar = contextvars.ContextVar("rev_holder", default=None)


def _note_rev(rev: int) -> None:
    holder = rev_holder.get()
    if holder is not None and rev:
        holder["rev"] = rev


async def bump_pair_rev(pair_key_value: str) -> int:
    """Увеличивает rev пары и возвращает новое значение (bump + чтение одним запросом)."""
    if not pair_key_value:
        return 0
    res = await _batch([
        ("INSERT INTO pair_rev (pair_key, rev) VALUES (?, 1) "
         "ON CONFLICT(pair_key) DO UPDATE SET rev = rev + 1", (pair_key_value,)),
        ("SELECT rev FROM pair_rev WHERE pair_key = ?", (pair_key_value,)),
    ])
    rev = res[1].rows[0][0] if res[1].rows else 0
    _note_rev(rev)
    return rev


async def get_pair_rev(pair_key_value: str) -> int:
    if not pair_key_value:
        return 0
    rs = await _exec("SELECT rev FROM pair_rev WHERE pair_key = ?", (pair_key_value,))
    return rs.rows[0][0] if rs.rows else 0


# --- События ---

EVENT_COLS = (
    "id, created_by, target_user, title, description, date, date_end, time, category, status, "
    "color, priority, reaction, reaction_by, decline_comment, "
    "decline_comment_creator, decline_comment_target, remind_before, visibility, "
    "alt_date, alt_time, archived_at"
)


def _event(r) -> dict | None:
    if not r:
        return None
    return {
        "id": r[0], "created_by": r[1], "target_user": r[2], "title": r[3], "description": r[4],
        "date": r[5], "date_end": r[6] or r[5], "time": r[7] or "", "category": r[8], "status": r[9],
        "color": r[10] or "", "priority": r[11] or "", "reaction": r[12] or "", "reaction_by": r[13],
        "decline_comment": r[14] or "", "decline_comment_creator": r[15] or "",
        "decline_comment_target": r[16] or "", "remind_before": r[17] or 0,
        "visibility": r[18] or "shared", "alt_date": r[19] or "", "alt_time": r[20] or "",
        "archived_at": r[21] or 0,
        "reactions": {}, "idea_responses": {}, "checklist": [], "comments": [],
    }


async def _attach_items(events: list[dict]) -> list[dict]:
    if not events:
        return events
    ids = [e["id"] for e in events]
    marks = ",".join("?" for _ in ids)
    by_id = {e["id"]: e for e in events}

    shop_ids = [e["id"] for e in events if e["category"] == "shop"]
    # Теперь комментарии грузим для всех, кроме магазина
    chat_ids = [e["id"] for e in events if e["category"] != "shop"]

    stmts: list[tuple[str, tuple]] = []
    pos: dict[str, int] = {}

    if shop_ids:
        sm = ",".join("?" for _ in shop_ids)
        pos["checklist"] = len(stmts)
        stmts.append((
            f"SELECT id, event_id, text, checked FROM checklist_items "
            f"WHERE event_id IN ({sm}) ORDER BY position, id",
            tuple(shop_ids),
        ))

    pos["reactions"] = len(stmts)
    stmts.append((
        f"SELECT event_id, user_id, emoji FROM reactions WHERE event_id IN ({marks})",
        tuple(ids),
    ))

    pos["ideas"] = len(stmts)
    stmts.append((
        f"SELECT event_id, idea_idx, status, user_id, ts FROM idea_responses "
        f"WHERE event_id IN ({marks})",
        tuple(ids),
    ))

    if chat_ids:
        cm = ",".join("?" for _ in chat_ids)
        pos["comments"] = len(stmts)
        stmts.append((
            f"SELECT event_id, id, user_id, text, photo_id, created_at FROM event_comments "
            f"WHERE event_id IN ({cm}) ORDER BY created_at, id",
            tuple(chat_ids),
        ))

    try:
        results = await _batch(stmts)
    except DbError:
        log.exception("Не удалось загрузить вложения событий (graceful degradation)")
        return events

    if "comments" in pos:
        for event_id, cid, uid, text, photo_id, created_at in results[pos["comments"]].rows:
            by_id[event_id]["comments"].append(
                {"id": cid, "sender_id": uid, "text": text, "photo_id": photo_id, "created_at": created_at}
            )
    if "checklist" in pos:
        for item_id, event_id, text, checked in results[pos["checklist"]].rows:
            by_id[event_id]["checklist"].append(
                {"id": item_id, "text": text, "checked": bool(checked)}
            )
    for event_id, user_id, emoji in results[pos["reactions"]].rows:
        by_id[event_id]["reactions"][user_id] = emoji
    for event_id, idx, status, uid, ts in results[pos["ideas"]].rows:
        by_id[event_id]["idea_responses"][idx] = {"status": status, "user_id": uid, "ts": ts}
    return events


async def count_events_created(user_id: int) -> int:
    rs = await _exec("SELECT COUNT(*) FROM events WHERE created_by = ?", (user_id,))
    return rs.rows[0][0]


async def create_event(created_by, target_user, title, description, date, event_time, category,
                       items=None, color: str = "", remind_before: int = 0,
                       date_end: str = "", visibility: str = "shared"):
    initial_status = "accepted" if category in ("shop", "todo", "personal", "sex") else "pending"
    rs = await _exec(
        "INSERT INTO events (created_by, target_user, title, description, date, date_end, time, category, status, color, remind_before, visibility) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (created_by, target_user, title, description, date, date_end, event_time, category,
         initial_status, color, remind_before, visibility),
    )
    event_id = rs.last_id
    if items and category == "shop":
        try:
            await _batch([
                ("INSERT INTO checklist_items (event_id, text, checked, position) VALUES (?, ?, ?, ?)",
                 (event_id, it["text"], int(it["checked"]), i + 1))
                for i, it in enumerate(items)
            ])
        except DbError:
            await _exec("DELETE FROM events WHERE id = ?", (event_id,))
            raise
    await bump_pair_rev(pair_key(created_by, target_user))
    return await get_event(event_id)


async def list_events_range(user_id: int, start: str, end: str) -> list[dict]:
    rs = await _exec(
        f"SELECT {EVENT_COLS} FROM events "
        "WHERE archived_at = 0 "
        "AND (COALESCE(NULLIF(date_end, ''), date) >= ? AND date < ?) "
        "AND ((category != 'personal' AND (created_by = ? OR target_user = ?)) "
        "  OR (category = 'personal' AND created_by = ?) "
        "  OR (category = 'personal' AND visibility = 'shared' AND target_user = ?)) "
        "ORDER BY date, time, id",
        (start, end, user_id, user_id, user_id, user_id),
    )
    return await _attach_items([_event(r) for r in rs.rows])


async def get_event(event_id: int) -> dict | None:
    rs = await _exec(f"SELECT {EVENT_COLS} FROM events WHERE id = ?", (event_id,))
    if not rs.rows:
        return None
    return (await _attach_items([_event(rs.rows[0])]))[0]


async def set_status(event_id: int, status: str) -> None:
    if status not in ("pending", "accepted", "declined", "done"):
        raise ValueError("bad status")
    await _exec("UPDATE events SET status = ?, alt_date = '', alt_time = '' WHERE id = ?", (status, event_id))


async def mark_event_done(event_id: int, user_id: int) -> bool:
    rs = await _exec(
        """
        UPDATE events
        SET status = 'done',
            alt_date = '',
            alt_time = ''
        WHERE id = ?
          AND (created_by = ? OR target_user = ?)
          AND status = 'accepted'
        """,
        (event_id, user_id, user_id),
    )

    return rs.affected == 1


async def set_target_user(event_id: int, target_user: int) -> None:
    await _exec("UPDATE events SET target_user = ? WHERE id = ?", (target_user, event_id))


async def update_event(event_id, title, description, date, event_time, category, reset_status: bool,
                       color: str = "", remind_before: int = 0, date_end: str = "",
                       visibility: str | None = None, reset_reminders: bool = False):
    stmts = [(
        "UPDATE events SET title = ?, description = ?, date = ?, date_end = ?, time = ?, "
        "category = ?, color = ?, remind_before = ?, "
        "status = CASE WHEN ? THEN 'pending' ELSE status END WHERE id = ?",
        (title, description, date, date_end, event_time, category, color, remind_before,
         int(reset_status), event_id),
    )]
    if visibility is not None:
        stmts.append(("UPDATE events SET visibility = ? WHERE id = ?", (visibility, event_id)))
    if reset_status or reset_reminders:
        stmts.append(("DELETE FROM reminders WHERE event_id = ?", (event_id,)))
    if reset_status:
        stmts.append(("DELETE FROM idea_responses WHERE event_id = ?", (event_id,)))
    if category != "shop":
        stmts.append(("DELETE FROM checklist_items WHERE event_id = ?", (event_id,)))
    await _batch(stmts)
    ev = await get_event(event_id)
    if ev:
        await bump_pair_rev(pair_key(ev["created_by"], ev["target_user"]))
    return ev


async def delete_event(event_id: int) -> None:
    ev = await get_event(event_id)
    await _batch([
        ("DELETE FROM checklist_items WHERE event_id = ?", (event_id,)),
        ("DELETE FROM reminders WHERE event_id = ?", (event_id,)),
        ("DELETE FROM reactions WHERE event_id = ?", (event_id,)),
        ("DELETE FROM idea_responses WHERE event_id = ?", (event_id,)),
        ("DELETE FROM events WHERE id = ?", (event_id,)),
    ])
    if ev:
        await bump_pair_rev(pair_key(ev["created_by"], ev["target_user"]))


async def add_items(event_id: int, texts: list[str]) -> int:
    if not texts:
        return 0
    rs = await _exec(
        "SELECT COUNT(*), COALESCE(MAX(position), 0) FROM checklist_items WHERE event_id = ?",
        (event_id,),
    )
    count, last_pos = rs.rows[0]
    texts = texts[: max(0, MAX_ITEMS_PER_EVENT - count)]
    if not texts:
        return 0
    await _batch([
        ("INSERT INTO checklist_items (event_id, text, checked, position) VALUES (?, ?, 0, ?)",
         (event_id, t, last_pos + i + 1))
        for i, t in enumerate(texts)
    ])
    return len(texts)


async def add_item(event_id: int, text: str) -> bool:
    rs = await _exec(
        "INSERT INTO checklist_items (event_id, text, checked, position) "
        "SELECT ?, ?, 0, COALESCE((SELECT MAX(position) FROM checklist_items WHERE event_id = ?), 0) + 1 "
        "WHERE (SELECT COUNT(*) FROM checklist_items WHERE event_id = ?) < ?",
        (event_id, text, event_id, event_id, MAX_ITEMS_PER_EVENT),
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


async def move_item(event_id: int, item_id: int, new_position: int) -> bool:
    rs = await _exec(
        "SELECT id FROM checklist_items WHERE event_id = ? ORDER BY position, id", (event_id,)
    )
    ids = [r[0] for r in rs.rows]
    if item_id not in ids:
        return False
    ids.remove(item_id)
    new_position = max(0, min(new_position, len(ids)))
    ids.insert(new_position, item_id)
    await _batch([("UPDATE checklist_items SET position = ? WHERE id = ?", (i + 1, cid)) for i, cid in enumerate(ids)])
    return True


async def set_reaction(event_id: int, emoji: str, by_user_id: int) -> None:
    if not emoji:
        await _exec("DELETE FROM reactions WHERE event_id = ? AND user_id = ?", (event_id, by_user_id))
        return
    await _exec(
        "INSERT INTO reactions (event_id, user_id, emoji) VALUES (?, ?, ?) "
        "ON CONFLICT(event_id, user_id) DO UPDATE SET emoji = excluded.emoji",
        (event_id, by_user_id, emoji),
    )
    ev = await get_event(event_id)
    if ev:
        await bump_pair_rev(pair_key(ev["created_by"], ev["target_user"]))


async def set_idea_response(event_id: int, idea_idx: int, status: str, user_id: int) -> None:
    if status not in ("accepted", "declined"):
        raise ValueError("bad status")
    await _exec(
        "INSERT INTO idea_responses (event_id, idea_idx, status, user_id, ts) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(event_id, idea_idx) DO UPDATE SET "
        "status = excluded.status, user_id = excluded.user_id, ts = excluded.ts",
        (event_id, idea_idx, status, user_id, int(time.time())),
    )
    ev = await get_event(event_id)
    if ev:
        await bump_pair_rev(pair_key(ev["created_by"], ev["target_user"]))


async def set_decline_comment_slot(event_id: int, slot: str, comment: str) -> None:
    if slot not in ("creator", "target"):
        raise ValueError("bad slot")
    col = "decline_comment_creator" if slot == "creator" else "decline_comment_target"
    await _exec(f"UPDATE events SET {col} = ? WHERE id = ?", (comment, event_id))


async def set_alt_time(event_id: int, date: str, time: str) -> None:
    await _exec("UPDATE events SET alt_date = ?, alt_time = ? WHERE id = ?", (date, time, event_id))


# --- Новые функции (вынесено из main.py) ---

async def has_new_activity(user_id: int) -> bool:
    rs = await _exec(
        "WITH v AS ("
        " SELECT COALESCE(last_viewed_challenges, 0) AS ts "
        " FROM users WHERE telegram_id = ?"
        ") "
        "SELECT EXISTS ("
        " SELECT 1 FROM user_challenges c, v "
        " WHERE v.ts > 0 AND c.target_id = ? AND c.created_at > v.ts "
        " UNION ALL "
        " SELECT 1 FROM challenge_messages m "
        " JOIN user_challenges c ON c.id = m.challenge_id, v "
        " WHERE v.ts > 0 AND (c.creator_id = ? OR c.target_id = ?) "
        " AND m.sender_id != ? AND m.created_at > v.ts "
        " UNION ALL "
        " SELECT 1 FROM event_comments e "
        " JOIN events ev ON ev.id = e.event_id, v "
        " WHERE v.ts > 0 AND (ev.created_by = ? OR ev.target_user = ?) "
        " AND e.user_id != ? AND e.created_at > v.ts"
        ")",
        (user_id, user_id, user_id, user_id, user_id, user_id, user_id, user_id),
    )
    return bool(rs.rows and rs.rows[0][0])


async def set_last_viewed_challenges(user_id: int) -> None:
    await _exec(
        "UPDATE users SET last_viewed_challenges = ? WHERE telegram_id = ?",
        (int(time.time()), user_id),
    )


async def get_event_meta(event_id: int) -> dict | None:
    """Лёгкая проверка доступа: одна строка без вложений (чеклисты, реакции, комментарии)."""
    rs = await _exec(
        "SELECT created_by, target_user, category, title FROM events WHERE id = ?",
        (event_id,),
    )
    if not rs.rows:
        return None
    r = rs.rows[0]
    return {"id": event_id, "created_by": r[0], "target_user": r[1], "category": r[2], "title": r[3]}


_BUMP_SQL = (
    "INSERT INTO pair_rev (pair_key, rev) VALUES (?, 1) "
    "ON CONFLICT(pair_key) DO UPDATE SET rev = rev + 1"
)


async def get_event_comments(event_id: int) -> list[dict]:
    rs = await _exec(
        "SELECT id, user_id, text, photo_id, created_at FROM event_comments "
        "WHERE event_id = ? ORDER BY created_at ASC, id ASC",
        (event_id,),
    )
    return [{"id": r[0], "sender_id": r[1], "text": r[2], "photo_id": r[3], "created_at": r[4]} for r in rs.rows]


async def add_event_comment(event_id: int, user_id: int, text: str, photo_id: str,
                            pair_key_value: str) -> tuple[dict, int]:
    """INSERT + bump rev + чтение rev: одна транзакция, один сетевой запрос к Turso."""
    now = int(time.time())
    res = await _batch([
        ("INSERT INTO event_comments (event_id, user_id, text, photo_id, created_at) VALUES (?, ?, ?, ?, ?)",
         (event_id, user_id, text, photo_id, now)),
        (_BUMP_SQL, (pair_key_value,)),
        ("SELECT rev FROM pair_rev WHERE pair_key = ?", (pair_key_value,)),
    ])
    rev = res[2].rows[0][0] if res[2].rows else 0
    _note_rev(rev)
    comment = {"id": res[0].last_id, "sender_id": user_id, "text": text, "photo_id": photo_id, "created_at": now}
    return comment, rev


async def get_challenge_messages(challenge_id: int, after: int = 0) -> list[dict]:
    rs = await _exec(
        "SELECT id, sender_id, text, photo_id, created_at FROM challenge_messages "
        "WHERE challenge_id = ? AND id > ? ORDER BY id ASC",
        (challenge_id, after),
    )
    return [{"id": r[0], "sender_id": r[1], "text": r[2], "photo_id": r[3], "created_at": r[4]} for r in rs.rows]


async def add_challenge_message(challenge_id: int, sender_id: int, text: str, photo_id: str,
                                pair_key_value: str) -> dict:
    """INSERT + bump rev + чтение rev: одна транзакция, один запрос к Turso."""
    now = int(time.time())
    res = await _batch([
        ("INSERT INTO challenge_messages (challenge_id, sender_id, text, photo_id, created_at) VALUES (?, ?, ?, ?, ?)",
         (challenge_id, sender_id, text, photo_id, now)),
        ("INSERT INTO pair_rev (pair_key, rev) VALUES (?, 1) "
         "ON CONFLICT(pair_key) DO UPDATE SET rev = rev + 1", (pair_key_value,)),
        ("SELECT rev FROM pair_rev WHERE pair_key = ?", (pair_key_value,)),
    ])
    _note_rev(res[2].rows[0][0] if res[2].rows else 0)
    return {"id": res[0].last_id, "sender_id": sender_id, "text": text, "photo_id": photo_id, "created_at": now}


async def delete_challenge_messages(challenge_id: int) -> None:
    await _exec("DELETE FROM challenge_messages WHERE challenge_id = ?", (challenge_id,))


async def count_active_challenges_today(creator_id: int) -> int:
    now = int(time.time())
    start_of_day = now - (now % 86400)
    rs = await _exec(
        "SELECT COUNT(*) FROM user_challenges "
        "WHERE creator_id = ? AND created_at >= ? AND status IN ('pending', 'submitted')",
        (creator_id, start_of_day),
    )
    return rs.rows[0][0]

async def _hidden_idea_keys(user_id: int, partner_id: int | None) -> set[str]:
    if partner_id:
        rs = await _exec("SELECT DISTINCT idea_key FROM hidden_ideas WHERE user_id IN (?, ?)", (user_id, partner_id))
    else:
        rs = await _exec("SELECT idea_key FROM hidden_ideas WHERE user_id = ?", (user_id,))
    return {r[0] for r in rs.rows}


async def get_sex_ideas(user_id: int, partner_id: int | None) -> list[dict]:
    hidden = await _hidden_idea_keys(user_id, partner_id)
    ideas = [dict(i) for i in _json_ideas() if i["id"] not in hidden]
    if partner_id:
        rs = await _exec("SELECT id, category_title, text FROM sex_ideas WHERE source = 'sex' AND user_id IN (?, ?) ORDER BY id DESC", (user_id, partner_id))
    else:
        rs = await _exec("SELECT id, category_title, text FROM sex_ideas WHERE source = 'sex' AND user_id = ? ORDER BY id DESC", (user_id,))
    for r in rs.rows:
        ideas.append({"id": r[0], "category_title": r[1], "text": r[2]})
    return ideas


async def add_sex_idea(user_id: int, category_title: str, text: str) -> int | None:
    rs = await _exec("INSERT INTO sex_ideas (user_id, category_title, text, source) VALUES (?, ?, ?, 'sex')", (user_id, category_title, text))
    return rs.last_id


async def archive_idea(category_title: str, text: str, source: str, archived_by: int | None) -> None:
    await _exec("INSERT OR IGNORE INTO archived_ideas (category_title, text, source, archived_by, archived_at) VALUES (?, ?, ?, ?, ?)", (category_title, text, source, archived_by, int(time.time())))


async def delete_sex_idea(idea_id: int, archived_by: int | None = None) -> bool:
    rs = await _exec("SELECT category_title, text FROM sex_ideas WHERE id = ? AND source = 'sex'", (idea_id,))
    if not rs.rows:
        return False
    _category_title, text = rs.rows[0]
    try:
        await archive_idea(CUSTOM_ARCHIVE_TITLE, text, "custom", archived_by)
    except Exception:
        pass
    rs2 = await _exec("DELETE FROM sex_ideas WHERE id = ? AND source = 'sex'", (idea_id,))
    return rs2.affected == 1


async def hide_json_idea_for_pair(user_id: int, partner_id: int | None, idea_key: str) -> None:
    idea = next((i for i in _json_ideas() if i["id"] == idea_key), None)
    if idea is None:
        return
    try:
        await archive_idea(idea["category_title"], idea["text"], "builtin", user_id)
    except Exception:
        pass
    rows = [(user_id, idea_key)]
    if partner_id and partner_id != user_id:
        rows.append((partner_id, idea_key))
    await _batch([("INSERT OR IGNORE INTO hidden_ideas (user_id, idea_key) VALUES (?, ?)", r) for r in rows])


async def get_archived_ideas(limit: int = 2000) -> list[dict]:
    rs = await _exec("SELECT id, category_title, text, source, archived_by, archived_at FROM archived_ideas ORDER BY archived_at DESC, id DESC LIMIT ?", (limit,))
    return [{"id": r[0], "category_title": r[1], "text": r[2], "source": r[3], "archived_by": r[4], "archived_at": r[5]} for r in rs.rows]


async def reminder_candidates(date_from: str, date_to: str) -> list[dict]:
    rs = await _exec(
        "SELECT e.id, e.title, e.date, COALESCE(NULLIF(e.date_end,''), e.date) AS date_end, "
        "e.time, u.telegram_id, u.tz, e.category, e.remind_before, e.visibility "
        "FROM events e JOIN users u ON u.telegram_id IN (e.created_by, e.target_user) "
        "WHERE e.archived_at = 0 "
        "AND e.status = 'accepted' AND COALESCE(NULLIF(e.date_end,''), e.date) BETWEEN ? AND ? "
        "AND (e.visibility = 'shared' OR u.telegram_id = e.created_by) "
        "AND NOT EXISTS (SELECT 1 FROM reminders r WHERE r.event_id = e.id AND r.user_id = u.telegram_id)",
        (date_from, date_to),
    )
    return [
        {"event_id": r[0], "title": r[1], "date": r[2], "date_end": r[3],
         "time": r[4] or "", "user_id": r[5], "tz": r[6] or "",
         "category": r[7] or "", "remind_before": r[8] or 0, "visibility": r[9] or "shared"}
        for r in rs.rows
    ]


async def mark_reminded(event_id: int, user_id: int) -> bool:
    rs = await _exec("INSERT OR IGNORE INTO reminders (event_id, user_id) VALUES (?, ?)", (event_id, user_id))
    return rs.affected == 1


async def purge_old_reminders(date_before: str) -> int:
    rs = await _exec("DELETE FROM reminders WHERE event_id IN (SELECT id FROM events WHERE date < ?)", (date_before,))
    return rs.affected


async def archive_old_events(date_before: str) -> int:
    now = int(time.time())
    rs = await _exec(
        "UPDATE events SET archived_at = ? "
        "WHERE archived_at = 0 AND COALESCE(NULLIF(date_end, ''), date) < ?",
        (now, date_before)
    )
    return rs.affected


async def purge_archived_events(cutoff_timestamp: int) -> int:
    old = "SELECT id FROM events WHERE archived_at > 0 AND archived_at < ?"
    a = (cutoff_timestamp,)
    rs = await _batch([
        (f"DELETE FROM checklist_items WHERE event_id IN ({old})", a),
        (f"DELETE FROM reminders WHERE event_id IN ({old})", a),
        (f"DELETE FROM reactions WHERE event_id IN ({old})", a),
        (f"DELETE FROM idea_responses WHERE event_id IN ({old})", a),
        (f"DELETE FROM event_comments WHERE event_id IN ({old})", a),
        ("DELETE FROM events WHERE archived_at > 0 AND archived_at < ?", a),
    ])
    return rs[-1].affected


async def list_archived_todos(user_id: int, limit: int = 500) -> list[dict]:
    rs = await _exec(
        f"SELECT {EVENT_COLS} FROM events "
        "WHERE archived_at > 0 "
        "AND (created_by = ? OR target_user = ?) ORDER BY archived_at DESC, id DESC LIMIT ?",
        (user_id, user_id, limit),
    )
    return [_event(r) for r in rs.rows]


async def pair_stats(user_id: int, partner_id: int) -> dict:
    rs = await _exec(
        "SELECT category, status, COUNT(*) FROM events "
        "WHERE archived_at = 0 "
        "AND ((created_by = ? AND target_user = ?) OR (created_by = ? AND target_user = ?)) "
        "AND visibility = 'shared' AND category IN ('rest', 'sex') GROUP BY category, status",
        (user_id, partner_id, partner_id, user_id),
    )
    by_status, by_category = {}, {}
    for category, status, n in rs.rows:
        by_status[status] = by_status.get(status, 0) + n
        by_category[category] = by_category.get(category, 0) + n
    return {
        "total": sum(by_status.values()), "pending": by_status.get("pending", 0),
        "accepted": by_status.get("accepted", 0), "declined": by_status.get("declined", 0),
        "by_category": by_category,
    }


async def get_moods(pair_key_value: str, date: str) -> list[dict]:
    rs = await _exec("SELECT user_id, emoji, text, updated_at FROM moods WHERE pair_key = ? AND date = ? ORDER BY user_id", (pair_key_value, date))
    return [{"user_id": r[0], "emoji": r[1] or "", "text": r[2] or "", "updated_at": r[3]} for r in rs.rows]


async def set_mood(pair_key_value: str, user_id: int, date: str, emoji: str, text: str) -> dict:
    emoji = (emoji or "")[:8]
    text = (text or "")[:280]
    if not emoji and not text:
        await _exec("DELETE FROM moods WHERE pair_key = ? AND user_id = ? AND date = ?", (pair_key_value, user_id, date))
        await bump_pair_rev(pair_key_value)
        return {"user_id": user_id, "emoji": "", "text": "", "updated_at": 0}
    now = int(time.time())
    await _exec(
        "INSERT INTO moods (pair_key, user_id, date, emoji, text, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(pair_key, user_id, date) DO UPDATE SET emoji = excluded.emoji, text = excluded.text, updated_at = excluded.updated_at",
        (pair_key_value, user_id, date, emoji, text, now),
    )
    await bump_pair_rev(pair_key_value)
    return {"user_id": user_id, "emoji": emoji, "text": text, "updated_at": now}


async def get_user_balance(user_id: int) -> int:
    rs = await _exec("SELECT balance FROM users WHERE telegram_id = ?", (user_id,))
    return rs.rows[0][0] if rs.rows else 0


async def add_balance(user_id: int, amount: int) -> None:
    await _exec("UPDATE users SET balance = balance + ? WHERE telegram_id = ?", (amount, user_id))


async def spend_balance(user_id: int, cost: int) -> bool:
    rs = await _exec("UPDATE users SET balance = balance - ? WHERE telegram_id = ? AND balance >= ?", (cost, user_id, cost))
    return rs.affected == 1


async def create_user_challenge(creator_id: int, target_id: int, category: str, text: str, reward: int, deadline: int = 0) -> dict:
    rs = await _exec(
        "INSERT INTO user_challenges (creator_id, target_id, category, text, reward, deadline, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (creator_id, target_id, category, text, reward, deadline, int(time.time())),
    )
    await bump_pair_rev(pair_key(creator_id, target_id))
    return await get_user_challenge(rs.last_id)


async def get_user_challenge(challenge_id: int) -> dict | None:
    rs = await _exec("SELECT * FROM user_challenges WHERE id = ?", (challenge_id,))
    if not rs.rows:
        return None
    r = rs.rows[0]
    return {
        "id": r[0], "creator_id": r[1], "target_id": r[2], "category": r[3],
        "text": r[4], "reward": r[5], "status": r[6], "photo_id": r[7],
        "created_at": r[8], "deadline": r[9] if len(r) > 9 else 0,
    }


async def get_all_user_challenges(user_id: int) -> list[dict]:
    rs = await _exec("SELECT * FROM user_challenges WHERE creator_id = ? OR target_id = ? ORDER BY created_at DESC", (user_id, user_id))
    return [
        {"id": r[0], "creator_id": r[1], "target_id": r[2], "category": r[3],
         "text": r[4], "reward": r[5], "status": r[6], "photo_id": r[7],
         "created_at": r[8], "deadline": r[9] if len(r) > 9 else 0}
        for r in rs.rows
    ]


async def update_user_challenge(challenge_id: int, status: str, photo_id: str | None = None) -> None:
    if photo_id is not None:
        await _exec("UPDATE user_challenges SET status = ?, photo_id = ? WHERE id = ?", (status, photo_id, challenge_id))
    else:
        await _exec("UPDATE user_challenges SET status = ? WHERE id = ?", (status, challenge_id))


_challenges_json_cache: tuple[int, dict] | None = None


async def get_challenges_json() -> dict:
    global _challenges_json_cache
    path = Path(__file__).parent / "challenges.json"
    try:
        mtime_ns = path.stat().st_mtime_ns
        if _challenges_json_cache is not None and _challenges_json_cache[0] == mtime_ns:
            return _challenges_json_cache[1]
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
        _challenges_json_cache = (mtime_ns, data)
        return data
    except Exception:
        return {}


_shop_cache: tuple[int, list[dict]] | None = None


def get_shop_items_json() -> list[dict]:
    global _shop_cache
    path = Path(__file__).parent / "shop_items.json"
    try:
        mtime_ns = path.stat().st_mtime_ns
    except FileNotFoundError:
        return []
    if _shop_cache is not None and _shop_cache[0] == mtime_ns:
        return _shop_cache[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data = data if isinstance(data, list) else []
    except Exception:
        return []
    _shop_cache = (mtime_ns, data)
    return data


async def update_challenge_photo(challenge_id: int, photo_path: str) -> None:
    await _exec("UPDATE user_challenges SET photo_id = ? WHERE id = ?", (photo_path, challenge_id))


async def get_challenge_by_photo(photo_path: str) -> dict | None:
    rs = await _exec("SELECT id FROM user_challenges WHERE photo_id = ? LIMIT 1", (photo_path,))
    return await get_user_challenge(rs.rows[0][0]) if rs.rows else None


async def transition_challenge(challenge_id: int, from_statuses: Sequence[str], new_status: str, photo_id: str | None = None) -> bool:
    marks = ",".join("?" for _ in from_statuses)
    if photo_id is None:
        sql = f"UPDATE user_challenges SET status = ? WHERE id = ? AND status IN ({marks})"
        args = (new_status, challenge_id, *from_statuses)
    else:
        sql = f"UPDATE user_challenges SET status = ?, photo_id = ? WHERE id = ? AND status IN ({marks})"
        args = (new_status, photo_id, challenge_id, *from_statuses)
    rs = await _exec(sql, args)
    if rs.affected == 1:
        c = await get_user_challenge(challenge_id)
        if c:
            await bump_pair_rev(pair_key(c["creator_id"], c["target_id"]))
    return rs.affected == 1


async def delete_user_challenge(challenge_id: int, user_id: int) -> bool:
    rs = await _exec("DELETE FROM user_challenges WHERE id = ? AND creator_id = ? AND status = 'pending'", (challenge_id, user_id))
    if rs.affected == 1:
        c = await get_user_challenge(challenge_id)
        if c:
            await bump_pair_rev(pair_key(c["creator_id"], c["target_id"]))
    return rs.affected == 1


async def process_expired_challenges() -> list[dict]:
    now = int(time.time())
    rs = await _exec("SELECT id, creator_id, target_id, text, reward FROM user_challenges WHERE status = 'pending' AND deadline > 0 AND deadline < ?", (now,))
    expired = []
    for cid, creator_id, target_id, text, reward in rs.rows:
        penalty = reward // 2
        rs_status = await _exec("UPDATE user_challenges SET status = 'rejected' WHERE id = ? AND status = 'pending'", (cid,))
        if rs_status.affected == 0:
            continue
        actual_penalty = 0
        if penalty > 0:
            bal_rs = await _exec("SELECT balance FROM users WHERE telegram_id = ?", (target_id,))
            current_balance = bal_rs.rows[0][0] if bal_rs.rows else 0
            rs_bal = await _exec("UPDATE users SET balance = CASE WHEN balance >= ? THEN balance - ? ELSE 0 END WHERE telegram_id = ?", (penalty, penalty, target_id))
            if rs_bal.affected > 0 and current_balance > 0:
                actual_penalty = min(penalty, current_balance)
        await bump_pair_rev(pair_key(creator_id, target_id))
        expired.append({"id": cid, "creator_id": creator_id, "target_id": target_id, "text": text, "reward": reward, "penalty": actual_penalty})
    return expired

async def delete_event_comment(comment_id: int, event_id: int, user_id: int,
                               pair_key_value: str) -> tuple[bool, int]:
    """DELETE + (если удалили) bump rev + чтение rev: одним запросом."""
    res = await _batch([
        ("DELETE FROM event_comments WHERE id = ? AND event_id = ? AND user_id = ?",
         (comment_id, event_id, user_id)),
        ("INSERT INTO pair_rev (pair_key, rev) SELECT ?, 1 WHERE changes() > 0 "
         "ON CONFLICT(pair_key) DO UPDATE SET rev = rev + 1",
         (pair_key_value,)),
        ("SELECT rev FROM pair_rev WHERE pair_key = ?", (pair_key_value,)),
    ])
    rev = res[2].rows[0][0] if res[2].rows else 0
    if res[0].affected == 1:
        _note_rev(rev)
    return res[0].affected == 1, rev


async def delete_challenge_message(message_id: int, challenge_id: int, user_id: int,
                                   pair_key_value: str) -> bool:
    """DELETE + (если удалили) bump rev: один запрос. Сообщение должно принадлежать этому челленджу."""
    res = await _batch([
        ("DELETE FROM challenge_messages WHERE id = ? AND challenge_id = ? AND sender_id = ?",
         (message_id, challenge_id, user_id)),
        ("INSERT INTO pair_rev (pair_key, rev) SELECT ?, 1 WHERE changes() > 0 "
         "ON CONFLICT(pair_key) DO UPDATE SET rev = rev + 1", (pair_key_value,)),
        ("SELECT rev FROM pair_rev WHERE pair_key = ?", (pair_key_value,)),
    ])
    if res[0].affected == 1:
        _note_rev(res[2].rows[0][0] if res[2].rows else 0)
    return res[0].affected == 1

async def dump_all_tables() -> dict:
    tables = ["users", "events", "checklist_items", "reminders", "sex_ideas", "hidden_ideas", "archived_ideas", "reactions", "idea_responses", "user_challenges", "challenge_messages", "shop_items", "event_comments", "moods"]
    out: dict = {"exported_at": int(time.time()), "tables": {}}
    for table in tables:
        try:
            rs = await _exec(f"SELECT * FROM {table}")
        except DbError:
            continue
        cols_rs = await _exec(f"PRAGMA table_info({table})")
        columns = [r[1] for r in cols_rs.rows]
        out["tables"][table] = {"columns": columns, "rows": rs.rows}
    return out