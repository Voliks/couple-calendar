"""Слой работы с БД: готовые идеи для секса читаются напрямую из sex_ideas.json."""
from __future__ import annotations

import asyncio
import hashlib
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
    _json_ideas()  # прогрев кэша встроенных идей
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


async def _m7_archived_ideas() -> None:
    await _exec(
        """
        CREATE TABLE IF NOT EXISTS archived_ideas (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            category_title TEXT NOT NULL,
            text           TEXT NOT NULL,
            source         TEXT NOT NULL,
            archived_by    INTEGER,
            archived_at    INTEGER NOT NULL
        )
        """
    )
    await _exec(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_archived_ideas "
        "ON archived_ideas (category_title, text, source)"
    )


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
    await _exec(
        """
        CREATE TABLE IF NOT EXISTS reactions (
            event_id INTEGER NOT NULL,
            user_id  INTEGER NOT NULL,
            emoji    TEXT NOT NULL,
            PRIMARY KEY (event_id, user_id)
        )
        """
    )


async def _m12_todo_remind_before() -> None:
    await _add_column("events", "remind_before", "INTEGER NOT NULL DEFAULT 0")


async def _m13_idea_responses() -> None:
    await _exec(
        """
        CREATE TABLE IF NOT EXISTS idea_responses (
            event_id  INTEGER NOT NULL,
            idea_idx  INTEGER NOT NULL,
            status    TEXT NOT NULL,
            user_id   INTEGER NOT NULL,
            ts        INTEGER NOT NULL,
            PRIMARY KEY (event_id, idea_idx)
        )
        """
    )


async def _m14_checklist_position() -> None:
    await _add_column("checklist_items", "position", "INTEGER NOT NULL DEFAULT 0")
    await _exec("UPDATE checklist_items SET position = id WHERE position = 0")


async def _m15_personal_section() -> None:
    await _add_column("events", "date_end", "TEXT NOT NULL DEFAULT ''")
    await _add_column("events", "visibility", "TEXT NOT NULL DEFAULT 'shared'")
    await _exec(
        "CREATE INDEX IF NOT EXISTS idx_events_personal "
        "ON events (created_by, category, date)"
    )


async def _m16_indexes_and_idea_keys() -> None:
    await _exec("CREATE INDEX IF NOT EXISTS idx_events_created ON events (created_by, date)")
    await _exec("CREATE INDEX IF NOT EXISTS idx_events_target ON events (target_user, date)")
    await _exec("CREATE INDEX IF NOT EXISTS idx_checklist_event ON checklist_items (event_id)")
    # старые ключи json_N (позиция в файле) -> стабильные ключи по хэшу текста
    raw = _read_json_ideas_raw()
    rs = await _exec("SELECT user_id, idea_key FROM hidden_ideas")
    stmts = []
    for user_id, key in rs.rows:
        m = re.fullmatch(r"json_(\d+)", key)
        if not m:
            continue
        n = int(m.group(1))
        if 1 <= n <= len(raw):
            stmts.append(("INSERT OR IGNORE INTO hidden_ideas (user_id, idea_key) VALUES (?, ?)",
                          (user_id, _idea_key(*raw[n - 1]))))
        stmts.append(("DELETE FROM hidden_ideas WHERE user_id = ? AND idea_key = ?", (user_id, key)))
    await _batch(stmts)


MIGRATIONS = [
    (1, _m1_base),
    (2, _m2_features),
    (3, _m3_sex_ideas),
    (4, _m4_ideas_source),
    (5, _m5_fix_checklist),
    (6, _m6_hidden_ideas),
    (7, _m7_archived_ideas),
    (8, _m8_todo),
    (9, _m9_reactions),
    (10, _m10_two_comments),
    (11, _m11_reactions_table),
    (12, _m12_todo_remind_before),
    (13, _m13_idea_responses),
    (14, _m14_checklist_position),
    (15, _m15_personal_section),
    (16, _m16_indexes_and_idea_keys),
]


_REQUIRED_COLUMNS = {
    15: [
        ("events", "date_end", "TEXT NOT NULL DEFAULT ''"),
        ("events", "visibility", "TEXT NOT NULL DEFAULT 'shared'"),
    ],
}


async def _ensure_required_columns() -> None:
    for version, cols in _REQUIRED_COLUMNS.items():
        for table, name, ddl in cols:
            try:
                exists = name in await _columns(table)
            except DbError as e:
                log.error("Не удалось прочитать PRAGMA table_info(%s): %s", table, e)
                continue
            if exists:
                log.debug("Колонка %s.%s уже есть", table, name)
                continue
            try:
                await _exec(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                log.warning(
                    "До-добавлена колонка %s.%s (миграция v%s была применена частично)",
                    table, name, version,
                )
            except DbError as e:
                log.error("Не удалось до-добавить колонку %s.%s: %s", table, name, e)

    try:
        await _exec(
            "CREATE INDEX IF NOT EXISTS idx_events_personal "
            "ON events (created_by, category, date)"
        )
    except DbError as e:
        log.error("Не удалось создать индекс idx_events_personal: %s", e)


async def _migrate() -> None:
    await _exec("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    current = (await _exec("SELECT MAX(version) FROM schema_version")).rows[0][0] or 0
    log.info("Текущая версия схемы: %s", current)
    for version, fn in MIGRATIONS:
        if version > current:
            log.info("Миграция БД → v%s", version)
            await fn()
            await _exec("INSERT INTO schema_version (version) VALUES (?)", (version,))
    await _ensure_required_columns()


def _idea_key(category: str, text: str) -> str:
    """Стабильный ключ встроенной идеи: не зависит от порядка строк в sex_ideas.json."""
    return "json_h" + hashlib.sha1(f"{category}\n{text}".encode("utf-8")).hexdigest()[:12]


def _read_json_ideas_raw() -> list[tuple[str, str]]:
    path = Path(__file__).parent / "sex_ideas.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Не удалось прочитать sex_ideas.json")
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
    """Встроенные идеи (читаются один раз). Одинаковый текст в нескольких категориях остаётся один раз."""
    global _ideas_cache
    if _ideas_cache is None:
        raw = _read_json_ideas_raw()
        last = {t.strip().lower(): i for i, (_, t) in enumerate(raw)}
        _ideas_cache = [
            {"id": _idea_key(c, t), "category_title": c, "text": t}
            for i, (c, t) in enumerate(raw)
            if last[t.strip().lower()] == i
        ]
    return _ideas_cache


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
            (f"DELETE FROM reactions WHERE event_id IN (SELECT id FROM events WHERE {pair})", pargs),
            (f"DELETE FROM idea_responses WHERE event_id IN (SELECT id FROM events WHERE {pair})", pargs),
            (f"DELETE FROM events WHERE {pair}", pargs),
            ("UPDATE users SET partner_id = NULL, invite_expires = 0 WHERE telegram_id IN (?, ?)",
             (user_id, p)),
        ]
    )
    return p


EVENT_COLS = (
    "id, created_by, target_user, title, description, date, date_end, time, category, status, "
    "color, priority, reaction, reaction_by, decline_comment, "
    "decline_comment_creator, decline_comment_target, remind_before, visibility"
)


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
        "date_end": r[6] or r[5],
        "time": r[7] or "",
        "category": r[8],
        "status": r[9],
        "color": r[10] or "",
        "priority": r[11] or "",
        "reaction": r[12] or "",
        "reaction_by": r[13],
        "decline_comment": r[14] or "",
        "decline_comment_creator": r[15] or "",
        "decline_comment_target": r[16] or "",
        "remind_before": r[17] or 0,
        "visibility": r[18] or "shared",
        "reactions": {},
        "idea_responses": {},
        "checklist": [],
    }


async def _attach_items(events: list[dict]) -> list[dict]:
    shop = [e for e in events if e["category"] == "shop"]
    if shop:
        by_id = {e["id"]: e for e in shop}
        marks = ",".join("?" * len(by_id))
        rs = await _exec(
            f"SELECT id, event_id, text, checked FROM checklist_items "
            f"WHERE event_id IN ({marks}) ORDER BY position, id",
            tuple(by_id),
        )
        for item_id, event_id, text, checked in rs.rows:
            by_id[event_id]["checklist"].append({"id": item_id, "text": text, "checked": bool(checked)})

    if events:
        ids = [e["id"] for e in events]
        marks = ",".join("?" * len(ids))
        rs = await _exec(
            f"SELECT event_id, user_id, emoji FROM reactions WHERE event_id IN ({marks})",
            tuple(ids),
        )
        by_id = {e["id"]: e for e in events}
        for event_id, user_id, emoji in rs.rows:
            by_id[event_id]["reactions"][user_id] = emoji

        rs2 = await _exec(
            f"SELECT event_id, idea_idx, status, user_id, ts FROM idea_responses "
            f"WHERE event_id IN ({marks})",
            tuple(ids),
        )
        for event_id, idx, status, uid, ts in rs2.rows:
            by_id[event_id]["idea_responses"][idx] = {
                "status": status, "user_id": uid, "ts": ts,
            }

    return events


async def count_events_created(user_id: int) -> int:
    rs = await _exec("SELECT COUNT(*) FROM events WHERE created_by = ?", (user_id,))
    return rs.rows[0][0]


async def create_event(created_by, target_user, title, description, date, event_time, category,
                       items=None, color: str = "", remind_before: int = 0,
                       date_end: str = "", visibility: str = "shared"):
    initial_status = "accepted" if category in ("shop", "todo", "personal") else "pending"
    rs = await _exec(
        "INSERT INTO events "
        "(created_by, target_user, title, description, date, date_end, time, category, status, "
        " color, remind_before, visibility) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (created_by, target_user, title, description, date, date_end, event_time, category,
         initial_status, color, remind_before, visibility),
    )
    event_id = rs.last_id
    if items and category == "shop":
        try:
            await _batch(
                [("INSERT INTO checklist_items (event_id, text, checked, position) VALUES (?, ?, ?, ?)",
                  (event_id, it["text"], int(it["checked"]), i + 1))
                 for i, it in enumerate(items)]
            )
        except DbError:
            await _exec("DELETE FROM events WHERE id = ?", (event_id,))
            raise
    return await get_event(event_id)


async def list_events_range(user_id: int, start: str, end: str) -> list[dict]:
    """События, пересекающиеся с [start, end). Personal виден только автору/партнёру (shared)."""
    rs = await _exec(
        f"SELECT {EVENT_COLS} FROM events "
        "WHERE ("
        "  (COALESCE(NULLIF(date_end, ''), date) >= ? AND date < ?) "
        ") AND ("
        "  (category != 'personal' AND (created_by = ? OR target_user = ?)) "
        "  OR (category = 'personal' AND created_by = ?) "
        "  OR (category = 'personal' AND visibility = 'shared' AND target_user = ?) "
        ") "
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
    # Разрешаем новый статус 'done'
    if status not in ("pending", "accepted", "declined", "done"):
        raise ValueError("bad status")
    await _exec("UPDATE events SET status = ? WHERE id = ?", (status, event_id))




async def set_target_user(event_id: int, target_user: int) -> None:
    await _exec("UPDATE events SET target_user = ? WHERE id = ?", (target_user, event_id))


async def update_event(event_id, title, description, date, event_time, category, reset_status: bool,
                       color: str = "", remind_before: int = 0,
                       date_end: str = "", visibility: str | None = None,
                       reset_reminders: bool = False):
    stmts = [
        (
            "UPDATE events SET title = ?, description = ?, date = ?, date_end = ?, time = ?, "
            "category = ?, color = ?, remind_before = ?, "
            "status = CASE WHEN ? THEN 'pending' ELSE status END WHERE id = ?",
            (title, description, date, date_end, event_time, category, color, remind_before,
             int(reset_status), event_id),
        )
    ]
    if visibility is not None:
        stmts.append(("UPDATE events SET visibility = ? WHERE id = ?", (visibility, event_id)))
    if reset_status or reset_reminders:
        stmts.append(("DELETE FROM reminders WHERE event_id = ?", (event_id,)))
    if reset_status:
        stmts.append(("DELETE FROM idea_responses WHERE event_id = ?", (event_id,)))
    if category != "shop":
        stmts.append(("DELETE FROM checklist_items WHERE event_id = ?", (event_id,)))
    await _batch(stmts)
    return await get_event(event_id)


async def delete_event(event_id: int) -> None:
    await _batch(
        [
            ("DELETE FROM checklist_items WHERE event_id = ?", (event_id,)),
            ("DELETE FROM reminders WHERE event_id = ?", (event_id,)),
            ("DELETE FROM reactions WHERE event_id = ?", (event_id,)),
            ("DELETE FROM idea_responses WHERE event_id = ?", (event_id,)),
            ("DELETE FROM events WHERE id = ?", (event_id,)),
        ]
    )


async def add_item(event_id: int, text: str) -> bool:
    rs = await _exec(
        "INSERT INTO checklist_items (event_id, text, checked, position) "
        "SELECT ?, ?, 0, "
        "COALESCE((SELECT MAX(position) FROM checklist_items WHERE event_id = ?), 0) + 1 "
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
        "SELECT id FROM checklist_items WHERE event_id = ? ORDER BY position, id",
        (event_id,),
    )
    ids = [r[0] for r in rs.rows]
    if item_id not in ids:
        return False

    ids.remove(item_id)
    new_position = max(0, min(new_position, len(ids)))
    ids.insert(new_position, item_id)

    await _batch(
        [
            ("UPDATE checklist_items SET position = ? WHERE id = ?", (i + 1, cid))
            for i, cid in enumerate(ids)
        ]
    )
    return True


async def set_reaction(event_id: int, emoji: str, by_user_id: int) -> None:
    if not emoji:
        await _exec(
            "DELETE FROM reactions WHERE event_id = ? AND user_id = ?",
            (event_id, by_user_id),
        )
        return
    await _exec(
        "INSERT INTO reactions (event_id, user_id, emoji) VALUES (?, ?, ?) "
        "ON CONFLICT(event_id, user_id) DO UPDATE SET emoji = excluded.emoji",
        (event_id, by_user_id, emoji),
    )




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








async def set_decline_comment_slot(event_id: int, slot: str, comment: str) -> None:
    if slot not in ("creator", "target"):
        raise ValueError("bad slot")
    col = "decline_comment_creator" if slot == "creator" else "decline_comment_target"
    await _exec(f"UPDATE events SET {col} = ? WHERE id = ?", (comment, event_id))


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
    hidden = await _hidden_idea_keys(user_id, partner_id)
    ideas = [dict(i) for i in _json_ideas() if i["id"] not in hidden]

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


async def archive_idea(category_title: str, text: str, source: str, archived_by: int | None) -> None:
    await _exec(
        "INSERT OR IGNORE INTO archived_ideas "
        "(category_title, text, source, archived_by, archived_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (category_title, text, source, archived_by, int(time.time())),
    )


async def delete_sex_idea(idea_id: int, archived_by: int | None = None) -> bool:
    rs = await _exec(
        "SELECT category_title, text FROM sex_ideas WHERE id = ? AND source = 'sex'",
        (idea_id,),
    )
    if not rs.rows:
        return False
    _category_title, text = rs.rows[0]
    try:
        await archive_idea(CUSTOM_ARCHIVE_TITLE, text, "custom", archived_by)
    except Exception:
        log.exception("Не удалось записать идею в архив (не критично)")
    rs2 = await _exec("DELETE FROM sex_ideas WHERE id = ? AND source = 'sex'", (idea_id,))
    return rs2.affected == 1


async def hide_json_idea_for_pair(user_id: int, partner_id: int | None, idea_key: str) -> None:
    idea = next((i for i in _json_ideas() if i["id"] == idea_key), None)
    if idea is None:
        return  # неизвестный ключ — ничего не скрываем
    try:
        await archive_idea(idea["category_title"], idea["text"], "builtin", user_id)
    except Exception:
        log.exception("Не удалось архивировать JSON-идею (не критично)")

    rows = [(user_id, idea_key)]
    if partner_id and partner_id != user_id:
        rows.append((partner_id, idea_key))
    await _batch(
        [("INSERT OR IGNORE INTO hidden_ideas (user_id, idea_key) VALUES (?, ?)", r) for r in rows]
    )


async def get_archived_ideas(limit: int = 2000) -> list[dict]:
    rs = await _exec(
        "SELECT id, category_title, text, source, archived_by, archived_at "
        "FROM archived_ideas ORDER BY archived_at DESC, id DESC LIMIT ?",
        (limit,),
    )
    return [
        {
            "id": r[0],
            "category_title": r[1],
            "text": r[2],
            "source": r[3],
            "archived_by": r[4],
            "archived_at": r[5],
        }
        for r in rs.rows
    ]


async def reminder_candidates(date_from: str, date_to: str) -> list[dict]:
    """Кандидаты на напоминание. Private personal-события — только автору."""
    rs = await _exec(
        "SELECT e.id, e.title, e.date, COALESCE(NULLIF(e.date_end,''), e.date) AS date_end, "
        "       e.time, u.telegram_id, u.tz, e.category, e.remind_before, e.visibility "
        "FROM events e JOIN users u ON u.telegram_id IN (e.created_by, e.target_user) "
        "WHERE e.status = 'accepted' "
        "  AND COALESCE(NULLIF(e.date_end,''), e.date) BETWEEN ? AND ? "
        "  AND (e.visibility = 'shared' OR u.telegram_id = e.created_by) "
        "  AND NOT EXISTS (SELECT 1 FROM reminders r WHERE r.event_id = e.id AND r.user_id = u.telegram_id)",
        (date_from, date_to),
    )
    return [
        {
            "event_id": r[0], "title": r[1], "date": r[2], "date_end": r[3],
            "time": r[4] or "", "user_id": r[5], "tz": r[6] or "",
            "category": r[7] or "", "remind_before": r[8] or 0,
            "visibility": r[9] or "shared",
        }
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


async def purge_old_todos(date_before: str) -> int:
    old = (
        "SELECT id FROM events WHERE category = 'todo' "
        "AND COALESCE(NULLIF(date_end, ''), date) < ?"
    )
    a = (date_before,)
    rs = await _batch(
        [
            (f"DELETE FROM checklist_items WHERE event_id IN ({old})", a),
            (f"DELETE FROM reminders WHERE event_id IN ({old})", a),
            (f"DELETE FROM reactions WHERE event_id IN ({old})", a),
            (f"DELETE FROM idea_responses WHERE event_id IN ({old})", a),
            (
                "DELETE FROM events WHERE category = 'todo' "
                "AND COALESCE(NULLIF(date_end, ''), date) < ?",
                a,
            ),
        ]
    )
    return rs[4].affected


async def list_archived_todos(user_id: int, limit: int = 500) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=31)).strftime("%Y-%m-%d")
    rs = await _exec(
        f"SELECT {EVENT_COLS} FROM events "
        "WHERE category = 'todo' AND COALESCE(NULLIF(date_end, ''), date) < ? "
        "AND (created_by = ? OR target_user = ?) "
        "ORDER BY date DESC, id DESC LIMIT ?",
        (cutoff, user_id, user_id, limit),
    )
    return [_event(r) for r in rs.rows]


async def pair_stats(user_id: int, partner_id: int) -> dict:
    """Статистика пары по событиям, на которые нужен ответ (rest/sex).

    Дела, покупки и личное создаются сразу «принятыми» и в счёт не идут.
    """
    rs = await _exec(
        "SELECT category, status, COUNT(*) FROM events "
        "WHERE ((created_by = ? AND target_user = ?) OR (created_by = ? AND target_user = ?)) "
        "AND visibility = 'shared' AND category IN ('rest', 'sex') "
        "GROUP BY category, status",
        (user_id, partner_id, partner_id, user_id),
    )
    by_status: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for category, status, n in rs.rows:
        by_status[status] = by_status.get(status, 0) + n
        by_category[category] = by_category.get(category, 0) + n
    return {
        "total": sum(by_status.values()),
        "pending": by_status.get("pending", 0),
        "accepted": by_status.get("accepted", 0),
        "declined": by_status.get("declined", 0),
        "by_category": by_category,
    }
