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
    # Явная проверка на старте: печатаем, какие колонки реально есть в events
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
    # date_end — дата окончания диапазона (для мульти-выделения). '' = одиночное событие.
    await _add_column("events", "date_end", "TEXT NOT NULL DEFAULT ''")
    # visibility — 'shared' или 'private'. Осмысленно только для category='personal'.
    await _add_column("events", "visibility", "TEXT NOT NULL DEFAULT 'shared'")
    await _exec(
        "CREATE INDEX IF NOT EXISTS idx_events_personal "
        "ON events (created_by, category, date)"
    )


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
]


# Список «обязательных» колонок, которые должны существовать после
# соответствующей миграции. Нужен, если schema_version уже содержит версию,
# но ALTER TABLE реально не применился (частая проблема Turso при сбое/репликации).
_REQUIRED_COLUMNS = {
    15: [
        ("events", "date_end", "TEXT NOT NULL DEFAULT ''"),
        ("events", "visibility", "TEXT NOT NULL DEFAULT 'shared'"),
    ],
}


async def _ensure_required_columns() -> None:
    """Гарантирует наличие обязательных колонок. Работает независимо от
    значения schema_version — если колонки нет, она будет добавлена."""
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

    # Индекс тоже создаём принудительно — если его нет
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
    # Долечить уже отмеченные версии (на случай частичного применения)
    await _ensure_required_columns()
