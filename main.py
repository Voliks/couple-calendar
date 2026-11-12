import asyncio
import contextlib
import gzip
import hashlib
import json
import logging
import mimetypes
import os
import re
import secrets
import time
from collections import deque
from datetime import date as date_cls, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from aiogram import Dispatcher, Router
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.web_app import safe_parse_webapp_init_data
# pyright: reportMissingImports=false
import aiohttp
from aiohttp import web, FormData

from common import (
    BOT_TOKEN,
    CATEGORIES,
    CATEGORY_ICONS,
    CATEGORY_TITLES,
    TIME_RE,
    WEBAPP_URL,
    bot,
    event_reply_keyboard,
    fmt_range,
    get_zone,
    humanize_minutes as _humanize_minutes,
    notify,
    open_app_keyboard,
    safe_send,
    valid_date,
)
import db
from voice import close_http as close_voice_http, router as voice_router

PORT = int(os.getenv("PORT", "8080"))
INDEX_FILE = Path(__file__).parent / "webapp" / "index.html"

WEBHOOK_PATH = "/webhook"
_webhook_from_env = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_SECRET = _webhook_from_env or secrets.token_urlsafe(32)
WEBHOOK_URL = f"{WEBAPP_URL}{WEBHOOK_PATH}"

REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "9"))
SHOW_TITLES = os.getenv("NOTIFY_SHOW_TITLES", "0") == "1"

SEX_IDEA_CATEGORIES = {
    "🔥 Страстная ночь",
    "🌹 Романтический вечер",
    "✨ Эксперименты и фантазии",
}
ALLOWED_REACTIONS = ["❤️", "🔥", "😍", "😂", "👍", "🎉", "😮", "🥰"]
ALLOWED_REMIND_BEFORE = {0, 5, 15, 30, 60, 120, 180, 360, 720, 1440, 2880}


def _origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


ALLOWED_ORIGINS = {_origin(WEBAPP_URL)} | {
    o.strip().rstrip("/")
    for o in os.getenv("ALLOWED_ORIGINS", "").split(",")
    if o.strip()
}

MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
INIT_DATA_MAX_AGE = int(os.getenv("INIT_DATA_MAX_AGE", str(6 * 3600)))
RATE_LIMIT_READ = int(os.getenv("RATE_LIMIT_READ", "90"))
RATE_LIMIT_WRITE = int(os.getenv("RATE_LIMIT_WRITE", "40"))
RATE_WINDOW = 60

dp = Dispatcher()
router = Router()
dp.include_router(router)
dp.include_router(voice_router)

bot_username: str = ""


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def invite_link(code: str) -> str:
    return f"https://t.me/{bot_username}?start=ref_{code}"


def title_part(title: str) -> str:
    return f" «{title}»" if SHOW_TITLES else ""


_rate_hits: dict[int, deque] = {}
_new_cache: dict[int, tuple[float, bool]] = {}
_NEW_CACHE_MAX = 5000
NEW_ACTIVITY_TTL = 45


def _new_cache_put(uid: int, val: bool) -> None:
    if len(_new_cache) >= _NEW_CACHE_MAX:
        now = time.monotonic()
        stale = [k for k, v in _new_cache.items() if now - v[0] > NEW_ACTIVITY_TTL * 2]
        for k in stale:
            _new_cache.pop(k, None)
        if len(_new_cache) >= _NEW_CACHE_MAX:
            _new_cache.clear()
    _new_cache[uid] = (time.monotonic(), val)


def rate_limit(uid: int, *, write: bool = False) -> None:
    now = time.monotonic()
    q = _rate_hits.setdefault(uid, deque())
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    limit = RATE_LIMIT_WRITE if write else RATE_LIMIT_READ
    if len(q) >= limit:
        raise ApiError("Слишком много запросов, подождите минуту", 429)
    q.append(now)
    if len(_rate_hits) > 2000:
        stale = [
            k for k, v in _rate_hits.items() if not v or now - v[-1] > RATE_WINDOW * 2
        ]
        for k in stale:
            del _rate_hits[k]


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    me = message.from_user
    user = await db.get_or_create_user(me.id)
    notice = ""
    args = command.args or ""
    if args.startswith("ref_"):
        inviter = await db.get_user_by_code(args[4:])
        if inviter is None or inviter["telegram_id"] == me.id:
            notice = "⚠️ Эта пригласительная ссылка недействительна.\n\n"
        elif user["partner_id"] == inviter["telegram_id"]:
            notice = "Вы уже связаны с этим партнёром 💞\n\n"
        elif user["partner_id"] or inviter["partner_id"]:
            notice = "⚠️ У одного из вас уже есть пара, связать не получилось.\n\n"
        elif inviter["invite_expires"] < time.time():
            notice = "⚠️ Срок действия ссылки истёк. Попросите партнёра прислать новую (/invite).\n\n"
        elif await db.link_partners(me.id, inviter["telegram_id"]):
            notice = "💞 Готово! Вы связаны с партнёром.\n\n"
            await safe_send(
                inviter["telegram_id"],
                f"💞 {me.full_name} принял(а) ваше приглашение. Теперь у вас общий календарь!",
            )
        else:
            notice = "⚠️ Не получилось связать вас (кто-то уже занят). Попробуйте ещё раз.\n\n"
    await message.answer(
        f"{notice}Привет, {me.first_name}! Это общий календарь на двоих: планируйте даты и встречи и отвечайте на них в пару касаний.",
        reply_markup=open_app_keyboard(),
    )


@router.message(Command("invite"))
async def cmd_invite(message: Message):
    user = await db.get_or_create_user(message.from_user.id)
    if user["partner_id"]:
        await message.answer("У вас уже есть партнёр 💞", reply_markup=open_app_keyboard())
        return
    user = await db.ensure_invite(user)
    days = db.INVITE_TTL // 86400
    await message.answer(
        f"Отправьте эту ссылку второму человеку — после перехода по ней вы будете связаны (действует {days} дн.):\n\n"
        + invite_link(user["invite_code"])
    )


@router.message(Command("unlink"))
async def cmd_unlink(message: Message):
    user = await db.get_or_create_user(message.from_user.id)
    if not user["partner_id"]:
        await message.answer("У вас нет партнёра — отвязывать нечего.")
        return
    await message.answer(
        "Отвязать партнёра? Общие данные будут удалены навсегда через 24 часа. За это время вы сможете передумать и восстановить связь.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Да, отвязать", callback_data="unlink:yes"),
                    InlineKeyboardButton(text="Отмена", callback_data="unlink:no"),
                ]
            ]
        ),
    )


@router.callback_query(lambda c: c.data == "unlink:no")
async def unlink_cancel(call: CallbackQuery):
    await call.message.edit_text("Отменено.")
    await call.answer()


@router.callback_query(lambda c: c.data == "unlink:yes")
async def unlink_confirm(call: CallbackQuery):
    partner = await db.unlink_partners(call.from_user.id)
    if partner is None:
        await call.message.edit_text("У вас нет партнёра.")
    else:
        await call.message.edit_text("Готово: связь разорвана. Данные будут удалены через 24 часа.")
        notify(partner, "💔 Партнёр отвязал вас. У вас есть 24 часа, чтобы восстановить связь и вернуть данные.")
    await call.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("ev:"))
async def event_inline_action(call: CallbackQuery):
    try:
        _, action, raw_id = call.data.split(":", 2)
        event_id = int(raw_id)
    except (ValueError, AttributeError):
        await call.answer("Некорректное действие", show_alert=False)
        return
    event = await db.get_event(event_id)
    if event is None:
        await call.answer("Событие не найдено", show_alert=True)
        return
    if call.from_user.id != event["target_user"]:
        await call.answer("Отвечать может только приглашённый", show_alert=True)
        return
    if event["category"] in ("shop", "todo", "personal"):
        await call.answer("Это не требует ответа", show_alert=True)
        return
    if action == "accept":
        status = "accepted"
    elif action == "decline":
        status = "declined"
    else:
        await call.answer("Неизвестное действие", show_alert=False)
        return
    if event["status"] == status:
        await call.answer("Уже отмечено")
        return
    await db.set_status(event_id, status)
    icon = "✅" if status == "accepted" else "❌"
    who = call.from_user.first_name
    notify(
        event["created_by"],
        f"{icon} {who} ответил(а) на событие {fmt_range(event['date'], event.get('date_end', ''))}{title_part(event['title'])}.",
    )
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await call.answer("Готово")


@router.error()
async def on_error(event: ErrorEvent):
    logging.error("Ошибка в обработчике бота", exc_info=event.exception)
    update = event.update
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if msg:
        with contextlib.suppress(Exception):
            await msg.answer("⚠️ Что-то пошло не так, попробуйте ещё раз чуть позже.")


def _add_cors(request: web.Request, response: web.StreamResponse) -> None:
    origin = request.headers.get("Origin")
    if origin and origin in ALLOWED_ORIGINS:
        h = response.headers
        h["Access-Control-Allow-Origin"] = origin
        h["Vary"] = "Origin"
        h["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        h["Access-Control-Allow-Headers"] = "Content-Type, X-Init-Data, X-TZ, If-None-Match"
        h["Access-Control-Expose-Headers"] = "ETag"
        h["Access-Control-Max-Age"] = "86400"


@web.middleware
async def gzip_middleware(request: web.Request, handler):
    response = await handler(request)
    if (
        isinstance(response, web.Response)
        and response.status == 200
        and response.body
        and len(response.body) > 1024
        and "Content-Encoding" not in response.headers
        and request.path.startswith("/api")
        and "gzip" in request.headers.get("Accept-Encoding", "").lower()
    ):
        try:
            compressed = gzip.compress(response.body, compresslevel=4)
            response.body = compressed
            response.headers["Content-Encoding"] = "gzip"
            response.headers["Vary"] = "Accept-Encoding"
            response.headers.pop("Content-Length", None)
        except Exception as e:
            logging.error("Gzip compression failed: %s", e)
    return response


@web.middleware
async def api_middleware(request: web.Request, handler):
    try:
        if request.method == "OPTIONS":
            response = web.Response(status=204)
        else:
            response = await handler(request)
    except ApiError as e:
        response = web.json_response({"error": e.message}, status=e.status)
    except web.HTTPException as e:
        response = e
    except db.DbError:
        logging.exception("Ошибка базы данных")
        response = web.json_response({"error": "Сервис временно недоступен, попробуйте позже"}, status=503)
    except Exception:
        logging.exception("Необработанная ошибка в API")
        response = web.json_response({"error": "Внутренняя ошибка сервера"}, status=500)

    if request.path.startswith(("/css/", "/js/")):
        response.headers.setdefault("Cache-Control", "no-cache")

    if not response.headers.get("Content-Security-Policy"):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' https://telegram.org; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob: https://*.supabase.co; "
            "connect-src 'self' https://*.supabase.co; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors https://web.telegram.org https://*.telegram.org;"
        )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")

    _add_cors(request, response)
    return response


def authenticate(request: web.Request, write: bool = False):
    raw = request.headers.get("X-Init-Data", "")
    try:
        data = safe_parse_webapp_init_data(BOT_TOKEN, raw)
    except ValueError:
        raise ApiError("unauthorized", 401)
    if data.user is None or time.time() - data.auth_date.timestamp() > INIT_DATA_MAX_AGE:
        raise ApiError("session expired", 401)
    rate_limit(data.user.id, write=write)
    return data.user

async def read_json(request: web.Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise ApiError("Некорректный запрос")
    if not isinstance(body, dict):
        raise ApiError("Некорректный запрос")
    return body


def _int_param(request: web.Request, name: str) -> int:
    try:
        return int(request.match_info[name])
    except (KeyError, ValueError):
        raise ApiError("Некорректный запрос")


def normalize_items(raw) -> list:
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw[: db.MAX_ITEMS_PER_EVENT]:
        if isinstance(item, dict):
            text = str(item.get("text", "")).strip()[:200]
            checked = bool(item.get("checked", False))
        elif isinstance(item, str):
            text, checked = item.strip()[:200], False
        else:
            continue
        if text:
            result.append({"text": text, "checked": checked})
    return result


def _extract_idea_lines(description: str) -> list[str]:
    out = []
    for line in (description or "").splitlines():
        s = line.strip()
        if s.startswith("• "):
            t = s[2:].strip()
            if t:
                out.append(t)
    return out


def parse_event_payload(body: dict) -> dict:
    description = str(body.get("description", "")).strip()
    date = str(body.get("date", ""))
    date_end = str(body.get("date_end") or "").strip()
    event_time = str(body.get("time") or "").strip()
    category = str(body.get("category", "rest"))
    color = str(body.get("color", "")).strip()
    visibility = str(body.get("visibility") or "shared").strip()
    try:
        remind_before = int(body.get("remind_before", 0))
    except (TypeError, ValueError):
        remind_before = 0
    if category not in CATEGORIES:
        raise ApiError("Неизвестная категория")
    if len(description) > 2000:
        raise ApiError("Слишком длинный текст")
    if not valid_date(date):
        raise ApiError("Некорректная дата")
    if date_end:
        if not valid_date(date_end):
            raise ApiError("Некорректная дата окончания")
        if date_end < date:
            raise ApiError("Дата окончания раньше даты начала")
        try:
            span = (date_cls.fromisoformat(date_end) - date_cls.fromisoformat(date)).days
        except ValueError:
            raise ApiError("Некорректная дата окончания")
        if span > 366:
            raise ApiError("Слишком длинный диапазон (максимум 366 дней)")
    if event_time and not TIME_RE.match(event_time):
        raise ApiError("Некорректное время")
    if category == "personal":
        if color not in db.TODO_COLORS:
            raise ApiError("Выберите цвет для личного события")
        if remind_before not in ALLOWED_REMIND_BEFORE:
            raise ApiError("Недопустимое время напоминания")
        if remind_before and not event_time:
            raise ApiError("Напоминание возможно только если указано время")
        if visibility not in ("shared", "private"):
            raise ApiError("Некорректная видимость")
    elif category == "todo":
        if color not in db.TODO_COLORS:
            raise ApiError("Выберите цвет для дела")
        if remind_before not in ALLOWED_REMIND_BEFORE:
            raise ApiError("Недопустимое время напоминания")
        if remind_before and not event_time:
            raise ApiError("Напоминание возможно только если указано время")
        visibility = "shared"
    else:
        remind_before = 0
        visibility = "shared"
        if category not in ("rest", "sex"):
            date_end = ""
        if category in ("rest", "sex") and date_end:
            if color and color not in db.TODO_COLORS:
                raise ApiError("Некорректный цвет")
        else:
            color = ""
    if date_end == date:
        date_end = ""
    raw_title = str(body.get("title") or "").strip()[:100]
    title = (
        raw_title
        if (category in ("todo", "personal") and raw_title)
        else CATEGORY_TITLES.get(category, "Событие")
    )
    return {
        "title": title,
        "description": description,
        "date": date,
        "date_end": date_end,
        "time": event_time,
        "category": category,
        "color": color,
        "remind_before": remind_before,
        "visibility": visibility,
        "items": normalize_items(body.get("checklist")) if category == "shop" else [],
    }


def event_to_dict(e: dict, uid: int) -> dict:
    is_creator = e["created_by"] == uid
    partner_id = e["target_user"] if is_creator else e["created_by"]
    reactions = e.get("reactions", {}) or {}
    my_reaction = reactions.get(uid, "")
    partner_reaction = reactions.get(partner_id, "")
    idea_responses = e.get("idea_responses", {}) or {}
    overall = e["status"]
    visibility = e.get("visibility", "shared")
    date_end = e.get("date_end") or e["date"]
    idea_lines = _extract_idea_lines(e["description"]) if e["category"] == "sex" else []
    ideas_out = []
    for i, text in enumerate(idea_lines):
        r = idea_responses.get(i) or {}
        own = r.get("status", "")
        if own:
            effective = own
        elif overall == "accepted":
            effective = "accepted"
        elif overall == "declined":
            effective = "declined"
        else:
            effective = ""
        ideas_out.append({
            "idx": i,
            "text": text,
            "status": own,
            "effective": effective,
            "by": r.get("user_id"),
        })
    can_respond_ideas = not is_creator and overall == "pending" and len(ideas_out) > 0
    can_edit = is_creator or e["category"] in ("todo",)
    return {
        "id": e["id"],
        "title": e["title"],
        "description": e["description"],
        "date": e["date"],
        "date_end": date_end,
        "time": e["time"],
        "category": e["category"],
        "status": e["status"],
        "color": e.get("color", ""),
        "remind_before": e.get("remind_before", 0),
        "visibility": visibility,
        "is_private": visibility == "private",
        "reaction_mine": my_reaction,
        "reaction_partner": partner_reaction,
        "decline_comment_creator": e.get("decline_comment_creator", ""),
        "decline_comment_target": e.get("decline_comment_target", ""),
        "my_comment_slot": "creator" if is_creator else "target",
        "my_decline_comment": (
            e.get("decline_comment_creator", "")
            if is_creator
            else e.get("decline_comment_target", "")
        ),
        "is_creator": is_creator,
        "can_edit": can_edit,
        "checklist": e["checklist"],
        "comments": e.get("comments", []),
        "ideas": ideas_out,
        "can_respond_ideas": can_respond_ideas,
        "alt_date": e.get("alt_date", ""),
        "alt_time": e.get("alt_time", ""),
    }


async def member_event(request: web.Request, uid: int) -> dict:
    event = await db.get_event(_int_param(request, "id"))
    if event is None:
        raise ApiError("Событие не найдено", 404)
    if uid not in (event["created_by"], event["target_user"]):
        raise ApiError("Нет доступа", 403)
    return event


def parse_month(raw) -> tuple:
    if raw is None:
        now = datetime.now(timezone.utc)
        year, month = now.year, now.month
    else:
        m = MONTH_RE.match(raw)
        if not m or not 1 <= int(m.group(2)) <= 12 or not 2000 <= int(m.group(1)) <= 2100:
            raise ApiError("Некорректный месяц")
        year, month = int(m.group(1)), int(m.group(2))
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    return f"{year:04d}-{month:02d}-01", f"{ny:04d}-{nm:02d}-01"


async def health_check(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})

# --- СТАТИКА ДЛЯ КЛИЕНТА ---
async def api_config(request: web.Request) -> web.Response:
    """Статичные данные (цвета, реакции, время напоминаний). Идеи — в /api/state."""
    user = authenticate(request)
    body = json.dumps({
        "todo_colors": db.TODO_COLORS,
        "allowed_reactions": ALLOWED_REACTIONS,
        "allowed_remind_before": sorted(ALLOWED_REMIND_BEFORE),
    }, ensure_ascii=False)
    return web.Response(text=body, content_type="application/json", headers={"Cache-Control": "private, max-age=3600"})


async def api_state(request: web.Request) -> web.Response:
    tg_user = authenticate(request)
    user = await db.get_or_create_user(tg_user.id)
    tz = request.headers.get("X-TZ", "").strip()
    if tz and tz != user["tz"] and get_zone(tz):
        await db.set_timezone(tg_user.id, tz)

    has_partner = bool(user["partner_id"])
    pk = db.pair_key(tg_user.id, user["partner_id"]) if has_partner else ""

    etag_in = request.headers.get("If-None-Match")

    # Быстрый путь: клиент прислал ETag → проверяем только rev (1 RTT)
    if has_partner and etag_in:
        rev = await db.get_pair_rev(pk)
        etag = f'"{rev}"'
        if etag_in == etag:
            return web.Response(status=304, headers={"ETag": etag, "Cache-Control": "no-store"})
    else:
        rev = None  # получим параллельно ниже

    start, end = parse_month(request.query.get("month"))
    link = ""
    if not has_partner:
        user = await db.ensure_invite(user)
        link = invite_link(user["invite_code"])

    # Локальная дата пользователя для moods
    tz_obj = get_zone(user["tz"]) or timezone.utc
    local_now = datetime.now(timezone.utc).astimezone(tz_obj)
    today_local = local_now.strftime("%Y-%m-%d")

    async def _safe_stats():
        if not has_partner:
            return None
        try:
            return await db.pair_stats(tg_user.id, user["partner_id"])
        except Exception:
            logging.exception("Не удалось получить статистику")
            return None

    async def _safe_moods_raw():
        if not has_partner:
            return []
        try:
            return await db.get_moods(pk, today_local)
        except Exception:
            logging.exception("Не удалось получить настроение")
            return []

    async def _safe_sex_ideas():
        try:
            return await db.get_sex_ideas(tg_user.id, user["partner_id"] if has_partner else None)
        except Exception:
            logging.exception("Не удалось получить идеи")
            return []

    async def _has_new_cached():
        hit = _new_cache.get(tg_user.id)
        if hit and time.monotonic() - hit[0] < NEW_ACTIVITY_TTL:
            return hit[1]
        val = await db.has_new_activity(tg_user.id)
        _new_cache_put(tg_user.id, val)
        return val

    # Собираем все задачи, включая get_pair_rev если он ещё не получен
    tasks = [
        db.list_events_range(tg_user.id, start, end),
        _safe_stats(),
        db.get_user_balance(tg_user.id),
        _safe_moods_raw(),
        _safe_sex_ideas(),
        _has_new_cached(),
    ]
    if rev is None:
        tasks.append(db.get_pair_rev(pk) if has_partner else asyncio.sleep(0, 0))

    results = await asyncio.gather(*tasks)
    events, stats, balance, moods_raw, sex_ideas, has_new = results[:6]
    if rev is None:
        rev = results[6]

    etag = f'"{rev}"'

    # Собираем moods в нужный формат
    moods = None
    if has_partner:
        by_user = {m["user_id"]: m for m in moods_raw}
        mine = by_user.get(tg_user.id) or {"emoji": "", "text": "", "updated_at": 0}
        partner = by_user.get(user["partner_id"]) or {"emoji": "", "text": "", "updated_at": 0}
        moods = {"date": today_local, "mine": mine, "partner": partner}

    body = json.dumps(
        {
            "has_partner": has_partner,
            "is_unlinking": bool(user.get("unlinked_at")),
            "invite_link": link,
            "month": start[:7],
            "events": [event_to_dict(e, tg_user.id) for e in events],
            "stats": stats,
            "my_id": tg_user.id,
            "balance": balance,
            "has_new_challenges": has_new,
            "moods": moods,
            "sex_ideas": sex_ideas,
        },
        ensure_ascii=False,
        sort_keys=True,
    )

    headers = {"ETag": etag, "Cache-Control": "no-store"}
    return web.Response(text=body, content_type="application/json", headers=headers)


async def api_create_event(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    user = await db.get_or_create_user(tg_user.id)
    data = parse_event_payload(await read_json(request))
    if data["category"] != "personal" and not user["partner_id"]:
        raise ApiError("Сначала свяжитесь с партнёром", 409)
    if await db.count_events_created(tg_user.id) >= db.MAX_EVENTS_PER_USER:
        raise ApiError("Достигнут лимит событий", 409)
    is_private_personal = data["category"] == "personal" and data["visibility"] == "private"
    if is_private_personal:
        target_user = tg_user.id
    elif data["category"] == "personal":
        target_user = user["partner_id"] or tg_user.id
    else:
        target_user = user["partner_id"]
    event = await db.create_event(
        tg_user.id, target_user, data["title"], data["description"], data["date"], data["time"], data["category"], data["items"],
        color=data["color"], remind_before=data["remind_before"], date_end=data["date_end"], visibility=data["visibility"],
    )
    if data["category"] == "shop":
        notify(user["partner_id"], f"🛒 Партнёр добавил список покупок на {fmt_range(data['date'], data['date_end'])}. Откройте календарь, чтобы посмотреть.")
    elif data["category"] == "personal":
        if data["visibility"] == "shared" and user["partner_id"]:
            notify(user["partner_id"], f"📝 Партнёр добавил общее личное дело на {fmt_range(data['date'], data['date_end'])}{(' в ' + data['time']) if data['time'] else ''}. Откройте календарь, чтобы посмотреть.")
    elif data["category"] == "todo":
        at = f" в {data['time']}" if data["time"] else ""
        extra = f" (напомню за {_humanize_minutes(data['remind_before'])})" if data["time"] and data["remind_before"] else ""
        notify(user["partner_id"], f"📌 Новое дело на {fmt_range(data['date'], data['date_end'])}{at}.{extra} Откройте календарь, чтобы посмотреть.")
    else:
        icon = CATEGORY_ICONS.get(data["category"], "➕")
        notify(user["partner_id"], f"{icon} Новое событие на {fmt_range(data['date'], data['date_end'])}{(' в ' + data['time']) if data['time'] else ''}. Ответьте прямо здесь или откройте календарь.", reply_markup=event_reply_keyboard(event["id"]))
    return web.json_response(event_to_dict(event, tg_user.id), status=201)


async def api_update_event(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    user = await db.get_or_create_user(tg_user.id)
    event = await member_event(request, tg_user.id)
    if event["category"] == "personal" and event["created_by"] != tg_user.id:
        raise ApiError("Редактировать личное событие может только автор", 403)
    if event["category"] != "todo" and event["category"] != "personal" and event["created_by"] != tg_user.id:
        raise ApiError("Редактировать событие может только его автор", 403)
    data = parse_event_payload(await read_json(request))
    if data["category"] != event["category"]:
        raise ApiError("Категорию существующего события менять нельзя — создайте новое", 409)
    other_id = event["target_user"] if event["created_by"] == tg_user.id else event["created_by"]
    old_end = event.get("date_end") or ""
    if old_end == event["date"]:
        old_end = ""
    schedule_changed = (data["date"], data["date_end"], data["time"], data["remind_before"]) != (event["date"], old_end, event["time"], event.get("remind_before") or 0)
    identity_changed = (data["date"], data["date_end"], data["time"]) != (event["date"], old_end, event["time"])
    ideas_changed = data["category"] == "sex" and _extract_idea_lines(data["description"]) != _extract_idea_lines(event["description"])
    visibility_changed = data["visibility"] != event.get("visibility", "shared")
    changed = (identity_changed or visibility_changed or data["description"] != event["description"] or data["color"] != event.get("color", "") or data["remind_before"] != (event.get("remind_before") or 0) or data["title"] != event["title"])
    if not changed:
        return web.json_response(event_to_dict(event, tg_user.id))
    reset = (identity_changed or ideas_changed) and data["category"] not in ("shop", "todo", "personal")
    updated = await db.update_event(
        event["id"], data["title"], data["description"], data["date"], data["time"], data["category"], reset_status=reset,
        color=data["color"], remind_before=data["remind_before"], date_end=data["date_end"], visibility=data["visibility"], reset_reminders=schedule_changed,
    )
    if data["category"] == "personal" and visibility_changed:
        partner_id = user["partner_id"]
        if data["visibility"] == "private":
            await db.set_target_user(event["id"], tg_user.id)
        else:
            await db.set_target_user(event["id"], partner_id or tg_user.id)
        if partner_id:
            if data["visibility"] == "shared":
                notify(partner_id, f"📝 Партнёр сделал личное дело общим: {fmt_range(data['date'], data['date_end'])}{title_part(data['title'])}.")
            else:
                notify(partner_id, f"📝 Партнёр скрыл личное дело: {fmt_range(data['date'], data['date_end'])}{title_part(data['title'])}.")
    if data["category"] == "shop":
        notify(other_id, f"✏️ Список покупок на {fmt_range(data['date'], data['date_end'])} изменён.")
    elif data["category"] == "todo":
        notify(other_id, f"📌 Дело на {fmt_range(data['date'], data['date_end'])} изменено.")
    elif data["category"] != "personal":
        suffix = " Оно снова ждёт вашего ответа." if reset else ""
        notify(other_id, f"✏️ Событие на {fmt_range(data['date'], data['date_end'])}{title_part(data['title'])} изменено.{suffix}")
    updated = await db.get_event(event["id"])
    return web.json_response(event_to_dict(updated, tg_user.id))


async def api_delete_event(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    event = await member_event(request, tg_user.id)
    if event["category"] == "personal" and event["created_by"] != tg_user.id:
        raise ApiError("Удалять личное событие может только автор", 403)
    if event["category"] not in ("todo", "personal") and event["created_by"] != tg_user.id:
        raise ApiError("Удалить событие может только автор. Вы можете отказаться от него.", 403)
    await db.delete_event(event["id"])
    return web.json_response({"status": "deleted"})

async def api_respond(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    event = await member_event(request, tg_user.id)
    body = await read_json(request)

    if event["category"] in ("shop", "todo", "personal"):
        raise ApiError("Не требует ответа")

    status = body.get("status")
    if status not in ("accepted", "declined"):
        raise ApiError("Некорректный статус.")

    if event["target_user"] != tg_user.id:
        raise ApiError("Отвечать на событие может только приглашённый", 403)

    comment = str(body.get("comment") or "").strip()[:500]
    alt_date = str(body.get("alt_date") or "").strip()
    alt_time = str(body.get("alt_time") or "").strip()
    
    if alt_date and not valid_date(alt_date):
        alt_date = ""
        alt_time = ""

    slot = "creator" if event["created_by"] == tg_user.id else "target"

    if event["status"] != status or comment != event.get(f"decline_comment_{slot}", ""):
        await db.set_status(event["id"], status)
        if status == "declined":
            await db.set_decline_comment_slot(event["id"], slot, comment)
            if alt_date:
                await db.set_alt_time(event["id"], alt_date, alt_time)
        else:
            await db.set_decline_comment_slot(event["id"], slot, "")

        what = "принял(а)" if status == "accepted" else "отклонил(а)"
        alt_text = ""
        if status == "declined" and alt_date:
            alt_text = f"\n📅 Предлагает перенести на: {fmt_range(alt_date, '')}{(' в ' + alt_time) if alt_time else ''}"

        icon = "✅" if status in ("accepted", "done") else "❌"
        extra = f"\n💬 {comment}" if status == "declined" and comment else ""

        notify(
            event["created_by"],
            f"{icon} {tg_user.first_name} {what} событие на {fmt_range(event['date'], event.get('date_end', ''))}{title_part(event['title'])}.{alt_text}{extra}",
        )
        await db.bump_pair_rev(db.pair_key(event["created_by"], event["target_user"]))
        event = await db.get_event(event["id"])

    return web.json_response(event_to_dict(event, tg_user.id))


async def api_set_idea_response(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    event = await member_event(request, tg_user.id)
    if event["category"] != "sex":
        raise ApiError("Здесь нет отдельных идей")
    if event["status"] != "pending":
        raise ApiError("Общий ответ уже дан — изменить отдельные нельзя")
    if event["target_user"] != tg_user.id:
        raise ApiError("Отвечать по идеям может только приглашённый")
    body = await read_json(request)
    try:
        idx = int(body.get("idx"))
    except (TypeError, ValueError):
        raise ApiError("Некорректный idx")
    status = str(body.get("status") or "")
    if status not in ("accepted", "declined"):
        raise ApiError("Некорректный статус")
    idea_lines = _extract_idea_lines(event["description"])
    if not (0 <= idx < len(idea_lines)):
        raise ApiError("Идея не найдена", 404)
    await db.set_idea_response(event["id"], idx, status, tg_user.id)
    updated = await db.get_event(event["id"])
    return web.json_response(event_to_dict(updated, tg_user.id))


async def api_set_reaction(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    event = await member_event(request, tg_user.id)
    body = await read_json(request)
    emoji = str(body.get("emoji") or "").strip()
    if event["category"] in ("shop", "todo", "personal"):
        raise ApiError("Здесь реакция не нужна")
    if event["status"] != "accepted":
        raise ApiError("Реакция доступна только для принятого события")
    if emoji and emoji not in ALLOWED_REACTIONS:
        raise ApiError("Недопустимая реакция")
    await db.set_reaction(event["id"], emoji, tg_user.id)
    updated = await db.get_event(event["id"])
    return web.json_response(event_to_dict(updated, tg_user.id))


async def api_set_decline_comment(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    event = await member_event(request, tg_user.id)
    if event["category"] in ("shop", "todo", "personal"):
        raise ApiError("Здесь комментарий не нужен")
    if event["status"] != "declined":
        raise ApiError("Комментарий доступен только для отклонённого события")
    body = await read_json(request)
    comment = str(body.get("comment") or "").strip()[:500]
    slot = "creator" if event["created_by"] == tg_user.id else "target"
    prev = event.get(f"decline_comment_{slot}", "") or ""
    await db.set_decline_comment_slot(event["id"], slot, comment)
    if event["created_by"] != tg_user.id and comment != prev:
        at = f" в {event['time']}" if event.get("time") else ""
        if comment:
            body_text = f"💬 {tg_user.first_name} оставил(а) комментарий к событию на {fmt_range(event['date'], event.get('date_end', ''))}{at}{title_part(event['title'])}:\n\n{comment}"
        else:
            body_text = f"💬 {tg_user.first_name} удалил(а) свой комментарий к событию на {fmt_range(event['date'], event.get('date_end', ''))}{at}{title_part(event['title'])}."
        notify(event["created_by"], body_text)
        await db.bump_pair_rev(db.pair_key(event["created_by"], event["target_user"]))
    updated = await db.get_event(event["id"])
    return web.json_response(event_to_dict(updated, tg_user.id))


async def _shop_event(request: web.Request, uid: int) -> dict:
    event = await member_event(request, uid)
    if event["category"] != "shop":
        raise ApiError("Список доступен только для категории «Магазин»")
    return event


async def api_add_item(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    event = await _shop_event(request, tg_user.id)
    text = str((await read_json(request)).get("text", "")).strip()[:200]
    if not text:
        raise ApiError("Введите текст пункта")
    if not await db.add_item(event["id"], text):
        raise ApiError(f"Не больше {db.MAX_ITEMS_PER_EVENT} пунктов в списке", 409)
    await db.bump_pair_rev(db.pair_key(event["created_by"], event["target_user"]))
    return web.json_response(event_to_dict(await db.get_event(event["id"]), tg_user.id), status=201)


async def api_set_item(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    event = await _shop_event(request, tg_user.id)
    checked = (await read_json(request)).get("checked")
    if not isinstance(checked, bool):
        raise ApiError("Некорректный запрос")
    if not await db.set_item_checked(event["id"], _int_param(request, "item_id"), checked):
        raise ApiError("Пункт не найден", 404)
    await db.bump_pair_rev(db.pair_key(event["created_by"], event["target_user"]))
    return web.json_response(event_to_dict(await db.get_event(event["id"]), tg_user.id))


async def api_delete_item(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    event = await _shop_event(request, tg_user.id)
    if not await db.delete_item(event["id"], _int_param(request, "item_id")):
        raise ApiError("Пункт не найден", 404)
    await db.bump_pair_rev(db.pair_key(event["created_by"], event["target_user"]))
    return web.json_response(event_to_dict(await db.get_event(event["id"]), tg_user.id))


async def api_move_item(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    event = await _shop_event(request, tg_user.id)
    body = await read_json(request)
    try:
        position = int(body.get("position"))
    except (TypeError, ValueError):
        raise ApiError("Некорректная позиция")
    if position < 0:
        raise ApiError("Некорректная позиция")
    if not await db.move_item(event["id"], _int_param(request, "item_id"), position):
        raise ApiError("Пункт не найден", 404)
    await db.bump_pair_rev(db.pair_key(event["created_by"], event["target_user"]))
    return web.json_response(event_to_dict(await db.get_event(event["id"]), tg_user.id))


async def api_add_sex_idea(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    user = await db.get_or_create_user(tg_user.id)
    body = await read_json(request)
    category_title = str(body.get("category_title", "")).strip()
    text = str(body.get("text", "")).strip()[:300]
    if category_title not in SEX_IDEA_CATEGORIES:
        raise ApiError("Идеи можно добавлять только в категории для секса")
    if not text:
        raise ApiError("Укажите текст идеи")
    await db.add_sex_idea(tg_user.id, category_title, text)
    if user["partner_id"]:
        await db.bump_pair_rev(db.pair_key(tg_user.id, user["partner_id"]))
    ideas = await db.get_sex_ideas(tg_user.id, user["partner_id"])
    return web.json_response({"status": "ok", "ideas": ideas})


async def api_delete_sex_idea(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    user = await db.get_or_create_user(tg_user.id)
    raw_id = request.match_info.get("id", "")
    if raw_id.startswith("json_"):
        await db.hide_json_idea_for_pair(tg_user.id, user["partner_id"], raw_id)
    else:
        try:
            idea_id = int(raw_id)
        except ValueError:
            raise ApiError("Некорректный id идеи")
        if not await db.delete_sex_idea(idea_id, archived_by=tg_user.id):
            raise ApiError("Идея не найдена", 404)
    if user["partner_id"]:
        await db.bump_pair_rev(db.pair_key(tg_user.id, user["partner_id"]))
    ideas = await db.get_sex_ideas(tg_user.id, user["partner_id"])
    return web.json_response({"status": "deleted", "ideas": ideas})


async def api_archive(request: web.Request) -> web.Response:
    authenticate(request)
    try:
        items = await db.get_archived_ideas()
    except Exception:
        logging.exception("Не удалось прочитать архив идей")
        items = []
    body = json.dumps(items, ensure_ascii=False, indent=2)
    return web.Response(text=body, content_type="application/json; charset=utf-8")


async def api_archived_todos(request: web.Request) -> web.Response:
    tg_user = authenticate(request)
    try:
        items = await db.list_archived_todos(tg_user.id)
    except Exception:
        logging.exception("Не удалось прочитать архив дел")
        items = []
    out = [
        {
            "id": e["id"],
            "title": e["title"],
            "description": e["description"],
            "date": e["date"],
            "date_end": e.get("date_end") or e["date"],
            "time": e["time"],
            "category": e["category"],
            "status": e["status"],
            "color": e.get("color", ""),
            "is_creator": e["created_by"] == tg_user.id,
            "checklist": [],
        }
        for e in items
    ]
    return web.json_response({"items": out})


async def api_unlink(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    partner = await db.unlink_partners(tg_user.id)
    if partner is None:
        raise ApiError("У вас нет партнёра", 409)
    notify(partner, "💔 Партнёр отвязал вас. У вас есть 24 часа, чтобы восстановить связь и вернуть данные.")
    return web.json_response({"status": "soft_unlinked"})


async def api_restore_unlink(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    partner = await db.restore_partners(tg_user.id)
    if partner is None:
        raise ApiError("Не удалось восстановить (время истекло или партнёр не найден)", 409)
    notify(partner, "💞 Связь восстановлена! Все ваши общие данные вернулись на место.")
    return web.json_response({"status": "restored"})


async def api_get_mood(request: web.Request) -> web.Response:
    tg_user = authenticate(request)
    user = await db.get_or_create_user(tg_user.id)
    if not user["partner_id"]:
        raise ApiError("Сначала свяжитесь с партнёром", 409)
    date = request.query.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not valid_date(date):
        raise ApiError("Некорректная дата")
    pk = db.pair_key(tg_user.id, user["partner_id"])
    moods = await db.get_moods(pk, date)
    by_user = {m["user_id"]: m for m in moods}
    mine = by_user.get(tg_user.id) or {"emoji": "", "text": "", "updated_at": 0}
    partner = by_user.get(user["partner_id"]) or {"emoji": "", "text": "", "updated_at": 0}
    return web.json_response({"date": date, "my_id": tg_user.id, "partner_id": user["partner_id"], "mine": mine, "partner": partner})


async def api_set_mood(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    user = await db.get_or_create_user(tg_user.id)
    if not user["partner_id"]:
        raise ApiError("Сначала свяжитесь с партнёром", 409)
    body = await read_json(request)
    date = str(body.get("date") or "").strip()
    if not valid_date(date):
        raise ApiError("Некорректная дата")
    emoji = str(body.get("emoji") or "").strip()
    text = str(body.get("text") or "").strip()
    if emoji and len(emoji) > 8:
        raise ApiError("Слишком длинный эмодзи")
    if len(text) > 280:
        raise ApiError("Слишком длинный текст")

    pk = db.pair_key(tg_user.id, user["partner_id"])
    before = await db.get_moods(pk, date)
    mine_had = any(m["user_id"] == tg_user.id for m in before)
    
    await db.set_mood(pk, tg_user.id, date, emoji, text)

    if (emoji or text) and not mine_had:
        part = f" {emoji}" if emoji else ""
        msg = f"💭 {tg_user.first_name} поделился(ась) настроением{part}"
        if text:
            msg += f": {text}"
        notify(user["partner_id"], msg)

    return web.json_response({"status": "ok"})

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
PHOTO_BUCKET = os.getenv("SUPABASE_BUCKET", "challenges").strip() or "challenges"
PHOTO_EXTS = ("jpg", "jpeg", "png", "webp", "gif", "heic", "heif")
CHALLENGE_MAX_POINTS = 20

BACKUP_BUCKET = os.getenv("SUPABASE_BACKUP_BUCKET", "").strip()
BACKUP_KEEP_DAYS = int(os.getenv("SUPABASE_BACKUP_KEEP_DAYS", "14"))


# --- Прямая работа с Supabase Storage через aiohttp ---

async def _supabase_request(method: str, endpoint: str, body: dict = None) -> dict:
    """Запрос к REST API Supabase Storage."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ApiError("Хранилище не настроено", 503)
    headers = {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "apikey": SUPABASE_KEY,
        "Content-Type": "application/json",
    }
    url = f"{SUPABASE_URL}/storage/v1{endpoint}"
    
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, json=body, headers=headers) as resp:
            text = await resp.text()
            if not resp.ok:
                logging.error("Supabase error %s: %s", resp.status, text)
                raise ApiError(f"Ошибка хранилища", 502)
            try:
                return json.loads(text)
            except Exception:
                return {}


def _storage_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("http"):
        return url
    return f"{SUPABASE_URL}/storage/v1{url if url.startswith('/') else '/' + url}"


def _photo_path_ok(path: str, challenge_id: int) -> bool:
    if not path or len(path) > 500:
        return False
    if ".." in path or "/" in path or "\\" in path:
        return False
    exts = "|".join(PHOTO_EXTS)
    paths = [p.strip() for p in path.split(",") if p.strip()]
    if not paths or len(paths) > 10:
        return False
    for p in paths:
        if not re.fullmatch(rf"challenge_{challenge_id}_\d+_[0-9a-f]{{8}}\.({exts})", p, re.IGNORECASE):
            return False
    return True


def _short_text(text: str, limit: int = 80) -> str:
    t = (text or "").strip().replace("\n", " ")
    if len(t) <= limit:
        return t
    return t[: limit - 1] + "…"


async def api_upload_photo_url(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    body = await read_json(request)
    try:
        challenge_id = int(body.get("challenge_id"))
    except (TypeError, ValueError):
        raise ApiError("Некорректный запрос")

    challenge = await db.get_user_challenge(challenge_id)
    if not challenge or challenge["target_id"] != tg_user.id:
        raise ApiError("Челлендж не найден", 404)
    if challenge["status"] not in ("pending", "rejected"):
        raise ApiError("Фото сейчас прикрепить нельзя", 409)

    name = str(body.get("filename") or "")
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in PHOTO_EXTS:
        ext = "jpg"
    path = f"challenge_{challenge_id}_{int(time.time())}_{secrets.token_hex(4)}.{ext}"

    res = await _supabase_request("POST", f"/object/upload/sign/{PHOTO_BUCKET}/{path}", {})
    upload_url = _storage_url(res.get("signed_url") or res.get("signedUrl") or res.get("signedURL"))
    
    if not upload_url:
        raise ApiError("Не удалось подготовить загрузку", 502)
    return web.json_response({"upload_url": upload_url, "path": path})


async def api_challenge_photo_url(request: web.Request) -> web.Response:
    tg_user = authenticate(request)
    path = request.query.get("path", "").strip()
    if not path:
        raise ApiError("Путь не указан", 400)
        
    m = re.search(r"^challenge_(\d+)_", path)
    if not m:
        raise ApiError("Некорректный формат пути", 400)
    
    challenge_id = int(m.group(1))
    if not _photo_path_ok(path, challenge_id):
        raise ApiError("Некорректный путь к фото", 400)

    challenge = await db.get_user_challenge(challenge_id)
    if not challenge or tg_user.id not in (challenge["creator_id"], challenge["target_id"]):
        raise ApiError("Фото не найдено", 404)

    raw_photo_id = challenge.get("photo_id") or ""
    paths = [p.strip() for p in raw_photo_id.split(",") if p.strip()]
    if path not in paths:
        raise ApiError("Фото не найдено", 404)

    res = await _supabase_request("POST", f"/object/sign/{PHOTO_BUCKET}/{path}", {"expiresIn": 3600})
    view_url = _storage_url(res.get("signedURL") or res.get("signedUrl"))
    
    if not view_url:
        raise ApiError("Ошибка получения ссылки на фото", 502)
    return web.json_response({"view_url": view_url})


async def api_set_challenge_photo(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    challenge_id = _int_param(request, "id")
    challenge = await db.get_user_challenge(challenge_id)
    if not challenge or challenge["target_id"] != tg_user.id:
        raise ApiError("Челлендж не найден или вы не исполнитель", 403)

    body = await read_json(request)
    path = str(body.get("path") or "").strip()
    if not _photo_path_ok(path, challenge_id):
        raise ApiError("Некорректный путь к фото", 400)

    if not await db.transition_challenge(challenge_id, ("pending", "rejected"), "submitted", photo_id=path):
        raise ApiError("Этот вызов уже отправлен на проверку", 409)

    notify(challenge["creator_id"], f"🔥 Партнёр выполнил челлендж: «{_short_text(challenge['text'])}». Зайди в календарь, чтобы проверить!")
    return web.json_response({"status": "ok"})


async def api_get_challenges_data(request: web.Request) -> web.Response:
    tg_user = authenticate(request)
    refresh = request.query.get("refresh") == "1"

    # 1. Баланс и история одним batch-запросом к Turso (вместо двух отдельных)
    rs_balance, rs_history = await db._batch([
        ("SELECT balance FROM users WHERE telegram_id = ?", (tg_user.id,)),
        ("SELECT * FROM user_challenges WHERE creator_id = ? OR target_id = ? ORDER BY created_at DESC", (tg_user.id, tg_user.id)),
    ])
    
    balance = rs_balance.rows[0][0] if rs_balance.rows else 0
    history = [
        {
            "id": r[0], "creator_id": r[1], "target_id": r[2], "category": r[3],
            "text": r[4], "reward": r[5], "status": r[6], "photo_id": r[7],
            "created_at": r[8], "deadline": r[9] if len(r) > 9 else 0,
        }
        for r in rs_history.rows
    ]

    # 2. Обновляем last_viewed и получаем каталог (если нужно)
    catalog = None
    if not refresh:
        await db.set_last_viewed_challenges(tg_user.id)
        catalog = await db.get_challenges_json()
        _new_cache.pop(tg_user.id, None)

    now = int(time.time())
    for h in history:
        h["is_creator"] = (h["creator_id"] == tg_user.id)
        h["is_archived"] = False
        
        if h["status"] in ("approved", "rejected"):
            dl = h.get("deadline") or 0
            if dl > 0 and now > dl:
                h["is_archived"] = True

    payload = {
        "balance": balance,
        "history": history,
        "my_id": tg_user.id,
    }
    if catalog is not None:
        payload["catalog"] = catalog
    return web.json_response(payload)


async def api_create_challenge(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    user = await db.get_or_create_user(tg_user.id)
    if not user["partner_id"]:
        raise ApiError("Сначала свяжитесь с партнёром", 409)

    if await db.count_active_challenges_today(tg_user.id) >= 3:
        raise ApiError("Лимит на сегодня: можно иметь не более 3 активных челленджей.", 429)

    body = await read_json(request)
    text = str(body.get("text") or "").strip()[:300]
    category = str(body.get("category") or "").strip()[:50] or "Своё"
    try:
        points = int(body.get("reward"))
        hours = int(body.get("hours", 24))
    except (TypeError, ValueError):
        raise ApiError("Некорректное количество очков или времени")
        
    if not text:
        raise ApiError("Укажите текст задания")
    if not 1 <= points <= CHALLENGE_MAX_POINTS:
        raise ApiError(f"Количество очков должно быть от 1 до {CHALLENGE_MAX_POINTS}")
    if not 1 <= hours <= 720:
        raise ApiError("Время на выполнение должно быть от 1 часа до 30 дней (720 часов)")

    deadline = int(time.time()) + hours * 3600

    challenge = await db.create_user_challenge(tg_user.id, user["partner_id"], category, text, points, deadline)
    notify(user["partner_id"], f"⚔️ Тебе бросили вызов: «{_short_text(text)}» ({points} б.). На выполнение {hours} ч.")
    return web.json_response({"status": "created", "challenge": challenge}, status=201)


async def api_challenge_action(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    challenge_id = _int_param(request, "id")
    challenge = await db.get_user_challenge(challenge_id)
    if not challenge:
        raise ApiError("Челлендж не найден", 404)
        
    action = (await read_json(request)).get("action")
    is_creator = tg_user.id == challenge["creator_id"]

    if action == "approve" and is_creator:
        if not await db.transition_challenge(challenge_id, ("pending", "submitted"), "approved"):
            raise ApiError("Этот вызов нельзя засчитать (возможно, он уже завершён)", 409)
        await db.add_balance(challenge["target_id"], challenge["reward"])
        notify(challenge["target_id"], f"✅ Твой челлендж засчитан! Тебе начислено {challenge['reward']} баллов.")
    elif action == "reject" and is_creator:
        if not await db.transition_challenge(challenge_id, ("pending", "submitted"), "rejected"):
            raise ApiError("Этот вызов нельзя отклонить (возможно, он уже завершён)", 409)
        notify(challenge["target_id"], f"❌ Челлендж отклонён: «{_short_text(challenge['text'])}». Можно прислать другое фото.")
    else:
        raise ApiError("Недопустимое действие", 400)
        
    return web.json_response({"status": "ok"})


async def api_get_challenge_messages(request: web.Request) -> web.Response:
    tg_user = authenticate(request)
    challenge_id = _int_param(request, "id")
    challenge = await db.get_user_challenge(challenge_id)
    if not challenge or tg_user.id not in (challenge["creator_id"], challenge["target_id"]):
        raise ApiError("Челлендж не найден", 404)
        
    user = await db.get_or_create_user(tg_user.id)
    partner_id = challenge["target_id"] if tg_user.id == challenge["creator_id"] else challenge["creator_id"]
    if user["partner_id"] != partner_id:
        raise ApiError("Нет доступа к переписке бывшего партнёра", 403)
        
    try:
        after = max(0, int(request.query.get("after") or 0))
    except ValueError:
        after = 0
        
    messages = await db.get_challenge_messages(challenge_id, after)
    return web.json_response({"messages": messages})


async def api_post_challenge_message(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    challenge_id = _int_param(request, "id")
    challenge = await db.get_user_challenge(challenge_id)
    if not challenge or tg_user.id not in (challenge["creator_id"], challenge["target_id"]):
        raise ApiError("Челлендж не найден", 404)
        
    user = await db.get_or_create_user(tg_user.id)
    partner_id = challenge["target_id"] if tg_user.id == challenge["creator_id"] else challenge["creator_id"]
    if user["partner_id"] != partner_id:
        raise ApiError("Нельзя писать бывшему партнёру", 403)

    body = await read_json(request)
    text = str(body.get("text") or "").strip()[:500]
    if not text:
        raise ApiError("Введите текст", 400)
        
    await db.add_challenge_message(challenge_id, tg_user.id, text)
    await db.bump_pair_rev(db.pair_key(challenge["creator_id"], challenge["target_id"]))
    notify(partner_id, f"💬 Новое сообщение по челленджу «{_short_text(challenge['text'])}»:\n\n{_short_text(text, 200)}")
    
    return web.json_response({"status": "ok"})


async def api_get_shop_items(request: web.Request) -> web.Response:
    tg_user = authenticate(request)
    # Каталог магазина читается из JSON-файла (кэшируется в памяти), а баланс — из БД.
    # Один запрос к БД.
    items = db.get_shop_items_json()
    balance = await db.get_user_balance(tg_user.id)
    return web.json_response({"items": items, "balance": balance})


async def api_buy_shop_item(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    user = await db.get_or_create_user(tg_user.id)
    if not user["partner_id"]:
        raise ApiError("Сначала свяжитесь с партнёром", 409)
        
    body = await read_json(request)
    try:
        item_id = int(body.get("item_id"))
    except (TypeError, ValueError):
        raise ApiError("Некорректный ID товара")
        
    date = str(body.get("date") or "").strip()
    if not valid_date(date):
        raise ApiError("Некорректная дата")

    items = db.get_shop_items_json()
    item = next((i for i in items if i.get("id") == item_id), None)
    if not item:
        raise ApiError("Товар не найден", 404)
        
    title = item.get("title")
    cost = item.get("cost")
    emoji = item.get("emoji", "🎁")

    if not await db.spend_balance(tg_user.id, cost):
        raise ApiError("Недостаточно баллов для покупки", 403)

    try:
        event = await db.create_event(
            created_by=tg_user.id,
            target_user=user["partner_id"],
            title=f"{emoji} {title}",
            description="Куплено в магазине желаний",
            date=date,
            event_time="",
            category="rest",
        )
    except Exception:
        await db.add_balance(tg_user.id, cost)
        logging.exception("Ошибка при создании события покупки, средства возвращены")
        raise ApiError("Ошибка при создании события. Баллы возвращены.", 500)
    
    notify(user["partner_id"], f"🎁 Партнёр купил желание «{title}» на {fmt_range(date, '')}. Загляните в календарь!", reply_markup=event_reply_keyboard(event["id"]))
    return web.json_response({"status": "ok", "event": event_to_dict(event, tg_user.id)})


async def api_get_event_comments(request: web.Request) -> web.Response:
    tg_user = authenticate(request)
    event_id = _int_param(request, "id")
    event = await member_event(request, tg_user.id)
    if event["category"] not in ("rest", "sex"):
        return web.json_response({"comments": []})
    comments = await db.get_event_comments(event_id)
    return web.json_response({"comments": comments})


async def api_post_event_comment(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    event_id = _int_param(request, "id")
    event = await member_event(request, tg_user.id)
    if event["category"] not in ("rest", "sex"):
        raise ApiError("Комментарии доступны только для Отдыха и Секса", 403)

    body = await read_json(request)
    text = str(body.get("text") or "").strip()[:500]
    if not text:
        raise ApiError("Введите текст", 400)

    await db.add_event_comment(event_id, tg_user.id, text)
    await db.bump_pair_rev(db.pair_key(event["created_by"], event["target_user"]))
    
    partner_id = event["target_user"] if tg_user.id == event["created_by"] else event["created_by"]
    notify(partner_id, f"💬 Новый комментарий к событию «{event['title']}»:\n\n{text}")
    return web.json_response({"status": "ok"})


async def api_delete_challenge(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    challenge_id = _int_param(request, "id")
    challenge = await db.get_user_challenge(challenge_id)
    if not challenge:
        raise ApiError("Челлендж не найден", 404)
    if challenge["creator_id"] != tg_user.id:
        raise ApiError("Удалить может только тот, кто бросил вызов", 403)
    if challenge["status"] != "pending":
        raise ApiError("Нельзя удалить челлендж, который уже выполняется или завершен", 403)

    await db.delete_challenge_messages(challenge_id)
    await db.delete_user_challenge(challenge_id, tg_user.id)
    return web.json_response({"status": "deleted"})


async def api_archive_challenge_manually(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    challenge_id = _int_param(request, "id")
    challenge = await db.get_user_challenge(challenge_id)
    if not challenge:
        raise ApiError("Челлендж не найден", 404)
    if tg_user.id not in (challenge["creator_id"], challenge["target_id"]):
        raise ApiError("Нет доступа", 403)

    if not await db.transition_challenge(challenge_id, ("approved", "rejected"), "archived"):
        c_check = await db.get_user_challenge(challenge_id)
        if not c_check or c_check["status"] != "archived":
            raise ApiError("Архивировать можно только завершённые вызовы", 409)

    return web.json_response({"status": "archived"})


WEB_DIR = INDEX_FILE.parent.resolve()
_static_cache: dict[Path, tuple[int, bytes, bytes]] = {}
_index_cache: tuple = ()
ASSET_RE = re.compile(r'(src|href)="(js|css)/([\w.\-]+)(?:\?v=[\w.\-]*)?"')


def _text_charset(ctype: str):
    return "utf-8" if ctype.startswith("text/") or ctype.endswith("javascript") else None


def _respond(request: web.Request, etag: str, raw: bytes, gz: bytes, ctype: str, cache: str) -> web.Response:
    headers = {"ETag": etag, "Cache-Control": cache, "Vary": "Accept-Encoding"}
    if request.headers.get("If-None-Match") == etag:
        return web.Response(status=304, headers=headers)
    body = raw
    if "gzip" in request.headers.get("Accept-Encoding", "").lower():
        body = gz
        headers["Content-Encoding"] = "gzip"
    return web.Response(body=body, content_type=ctype, charset=_text_charset(ctype), headers=headers)


_asset_versions_cache: tuple[tuple, dict[str, str]] | None = None


def _asset_versions() -> dict[str, str]:
    """Кэшируем хэши файлов по их mtime_ns, чтобы не читать диск при каждом запросе."""
    global _asset_versions_cache
    
    files = []
    for d in ("js", "css"):
        for p in (WEB_DIR / d).glob("*"):
            if p.is_file():
                files.append((str(p), p.stat().st_mtime_ns))
    
    sig = tuple(sorted(files))
    if _asset_versions_cache and _asset_versions_cache[0] == sig:
        return _asset_versions_cache[1]
    
    res = {}
    for path_str, _ in files:
        p = Path(path_str)
        content = p.read_bytes()
        res[p.name] = hashlib.sha1(content).hexdigest()[:8]
    
    _asset_versions_cache = (sig, res)
    return res


def _build_index() -> tuple:
    """Кэширует итоговый HTML по mtime index.html и версиям ассетов."""
    global _index_cache
    versions = _asset_versions()
    sig = (INDEX_FILE.stat().st_mtime_ns, tuple(sorted(versions.items())))
    if _index_cache and _index_cache[0] == sig:
        return _index_cache
        
    short = {name: ver for name, ver in versions.items()}
    html = INDEX_FILE.read_text(encoding="utf-8")
    html = ASSET_RE.sub(lambda m: f'{m[1]}="{m[2]}/{m[3]}?v={short.get(m[3], "0")}"', html)
    
    meta_assets = '<meta name="asset-versions" content="' + json.dumps(short).replace('"', '&quot;') + '">'
    html = html.replace("</head>", meta_assets + "</head>", 1)
    
    raw = html.encode("utf-8")
    etag = '"' + hashlib.sha1(raw).hexdigest()[:20] + '"'
    _index_cache = (sig, etag, raw, gzip.compress(raw, 6))
    return _index_cache


async def index(request: web.Request) -> web.Response:
    _, etag, raw, gz = _build_index()
    return _respond(request, etag, raw, gz, "text/html", "no-cache")


async def static_file(request: web.Request) -> web.Response:
    kind = request.match_info["kind"]
    path = (WEB_DIR / kind / request.match_info["name"]).resolve()
    if path.parent != (WEB_DIR / kind) or not path.is_file():
        raise web.HTTPNotFound()
    mtime = path.stat().st_mtime_ns
    hit = _static_cache.get(path)
    if hit is None or hit[0] != mtime:
        raw = path.read_bytes()
        hit = _static_cache[path] = (mtime, raw, gzip.compress(raw, 6))
    ctype = (mimetypes.guess_type(path.name)[0] or "application/octet-stream").split(";")[0]
    cache = "public, max-age=31536000, immutable" if request.query.get("v") else "no-cache"
    return _respond(request, f'"{mtime:x}"', hit[1], hit[2], ctype, cache)


async def send_due_reminders() -> None:
    now_utc = datetime.now(timezone.utc)
    lo = (now_utc - timedelta(days=3)).strftime("%Y-%m-%d")
    hi = (now_utc + timedelta(days=3)).strftime("%Y-%m-%d")
    for c in await db.reminder_candidates(lo, hi):
        tz = get_zone(c["tz"]) or timezone.utc
        local_now = now_utc.astimezone(tz)
        if c["category"] in ("todo", "personal") and c["time"] and c.get("remind_before"):
            try:
                hh, mm = c["time"].split(":")
                event_dt_local = datetime.fromisoformat(f"{c['date']}T{hh}:{mm}:00").replace(tzinfo=tz)
            except Exception:
                continue
            remind_at = event_dt_local - timedelta(minutes=c["remind_before"])
            if not (remind_at <= local_now < event_dt_local):
                continue
            if not await db.mark_reminded(c["event_id"], c["user_id"]):
                continue
            left = max(1, int((event_dt_local - local_now).total_seconds() // 60))
            prefix = "📝" if c["category"] == "personal" else "📌"
            await safe_send(c["user_id"], f"{prefix} Напоминание: {c['title']} в {c['time']} (через {_humanize_minutes(left)}).")
            continue
        if local_now.strftime("%Y-%m-%d") != c["date"] or local_now.hour < REMINDER_HOUR:
            continue
        if not await db.mark_reminded(c["event_id"], c["user_id"]):
            continue
        at = f" в {c['time']}" if c["time"] else ""
        rng = fmt_range(c["date"], c.get("date_end", ""))
        if c["category"] == "todo":
            await safe_send(c["user_id"], f"📌 Сегодня дело{at}{title_part(c['title'])}. Откройте календарь, чтобы посмотреть.")
        elif c["category"] == "personal":
            await safe_send(c["user_id"], f"📝 Сегодня личное дело{at}{title_part(c['title'])}. Откройте календарь, чтобы посмотреть.")
        else:
            await safe_send(c["user_id"], f"⏰ Сегодня у вас запланировано событие{at}{title_part(c['title'])} (на {rng}). Откройте календарь, чтобы посмотреть детали.")


async def cleanup_old_reminders() -> None:
    now = datetime.now(timezone.utc)
    cutoff_reminders = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    cutoff_todos = (now - timedelta(days=182)).strftime("%Y-%m-%d")
    try:
        n = await db.purge_old_reminders(cutoff_reminders)
        if n: logging.info("Очищено старых напоминаний: %s", n)
    except Exception: logging.exception("Не удалось очистить старые напоминания")
    try:
        n2 = await db.purge_old_todos(cutoff_todos)
        if n2: logging.info("Удалено старых дел (6+ мес): %s", n2)
    except Exception: logging.exception("Не удалось очистить старые дела")


_last_cleanup_day: str = ""


async def reminder_loop() -> None:
    global _last_cleanup_day
    while True:
        try:
            now = int(time.time())
            rs = await db._exec("SELECT telegram_id FROM users WHERE unlinked_at > 0 AND unlinked_at < ?", (now - 24 * 3600,))
            for row in rs.rows:
                await db.finalize_unlink(row[0])
                logging.info("Удалены данные пары по истечении 24 часов (user %s)", row[0])

            await send_due_reminders()
            
            expired = await db.process_expired_challenges()
            for c in expired:
                notify(c["target_id"], f"⌛ Челлендж «{_short_text(c['text'])}» не выполнен в срок. С твоего баланса списано {c['penalty']} баллов.")
                notify(c["creator_id"], f"⌛ Челлендж «{_short_text(c['text'])}» просрочен. Партнёр потерял {c['penalty']} баллов.")
            
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if today != _last_cleanup_day:
                _last_cleanup_day = today
                await cleanup_old_reminders()
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Ошибка рассылки напоминаний")
        await asyncio.sleep(60)


async def main() -> None:
    global bot_username
    logging.basicConfig(level=logging.INFO)
    if not _webhook_from_env:
        logging.warning("WEBHOOK_SECRET не задан в env — сгенерирован временный. Задайте WEBHOOK_SECRET.")
    await db.init_db()
    bot_username = (await bot.get_me()).username
    with contextlib.suppress(Exception):
        await bot.set_my_commands([
            BotCommand(command="start", description="Открыть календарь"),
            BotCommand(command="invite", description="Ссылка-приглашение для партнёра"),
            BotCommand(command="unlink", description="Отвязать партнёра"),
        ])

    async def on_startup(app: web.Application):
        await bot.set_webhook(WEBHOOK_URL, secret_token=WEBHOOK_SECRET, drop_pending_updates=False)
        logging.info("Webhook установлен: %s", WEBHOOK_URL)

    app = web.Application(middlewares=[gzip_middleware, api_middleware], client_max_size=1024 * 1024)
    
    app.router.add_get("/", index)
    app.router.add_get("/health", health_check)
    app.router.add_get(r"/{kind:(?:js|css)}/{name}", static_file)
    
    app.router.add_get("/api/config", api_config)
    app.router.add_get("/api/state", api_state)
    
    app.router.add_get("/api/archive.json", api_archive)
    app.router.add_get("/api/todos/archive", api_archived_todos)
    
    app.router.add_post("/api/events", api_create_event)
    app.router.add_put("/api/events/{id}", api_update_event)
    app.router.add_delete("/api/events/{id}", api_delete_event)
    app.router.add_post("/api/events/{id}/respond", api_respond)
    app.router.add_post("/api/events/{id}/idea", api_set_idea_response)
    app.router.add_post("/api/events/{id}/reaction", api_set_reaction)
    app.router.add_post("/api/events/{id}/comment", api_set_decline_comment)
    
    app.router.add_post("/api/events/{id}/items", api_add_item)
    app.router.add_put("/api/events/{id}/items/{item_id}", api_set_item)
    app.router.add_delete("/api/events/{id}/items/{item_id}", api_delete_item)
    app.router.add_post("/api/events/{id}/items/{item_id}/move", api_move_item)
    
    app.router.add_post("/api/sex-ideas", api_add_sex_idea)
    app.router.add_delete("/api/sex-ideas/{id}", api_delete_sex_idea)
    
    app.router.add_post("/api/unlink", api_unlink)
    app.router.add_post("/api/unlink/restore", api_restore_unlink)
    
    app.router.add_get("/api/challenges", api_get_challenges_data)
    app.router.add_post("/api/challenges", api_create_challenge)
    app.router.add_post("/api/challenges/{id}/action", api_challenge_action)
    app.router.add_post("/api/challenges/upload-url", api_upload_photo_url)
    app.router.add_get("/api/challenges/photo-url", api_challenge_photo_url)
    app.router.add_post("/api/challenges/{id}/photo", api_set_challenge_photo)
    app.router.add_delete("/api/challenges/{id}", api_delete_challenge)
    app.router.add_get("/api/challenges/{id}/messages", api_get_challenge_messages)
    app.router.add_post("/api/challenges/{id}/messages", api_post_challenge_message)
    app.router.add_post("/api/challenges/{id}/archive", api_archive_challenge_manually)
    
    app.router.add_get("/api/shop", api_get_shop_items)
    app.router.add_post("/api/shop/buy", api_buy_shop_item)
    
    app.router.add_get("/api/events/{id}/comments", api_get_event_comments)
    app.router.add_post("/api/events/{id}/comments", api_post_event_comment)

    app.router.add_get("/api/mood", api_get_mood)
    app.router.add_post("/api/mood", api_set_mood)
    
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logging.info("Web app: http://0.0.0.0:%s  (public: %s)", PORT, WEBAPP_URL)
    
    reminders = asyncio.create_task(reminder_loop())
    try:
        await asyncio.Event().wait()
    finally:
        reminders.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reminders
        with contextlib.suppress(Exception):
            await bot.delete_webhook()
        await runner.cleanup()
        await close_voice_http()
        await bot.session.close()
        await db.close_db()


if __name__ == "__main__":
    asyncio.run(main())