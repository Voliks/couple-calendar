import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import time
from collections import deque
from datetime import date as date_cls, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from supabase import create_client
from aiogram import Dispatcher, Router  # type: ignore[reportMissingImports]
from aiogram.filters import Command, CommandObject, CommandStart  # type: ignore[reportMissingImports]
from aiogram.types import (  # type: ignore[reportMissingImports]
    BotCommand,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.web_app import safe_parse_webapp_init_data  # type: ignore[reportMissingImports]
from aiohttp import web  # type: ignore[reportMissingImports]

# common импортируем первым: он читает .env до импорта db
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
from voice import router as voice_router

PORT = int(os.getenv("PORT", "8080"))
INDEX_FILE = Path(__file__).parent / "webapp" / "index.html"

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
INIT_DATA_MAX_AGE = 24 * 3600
RATE_LIMIT = 40
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


def rate_limit(uid: int) -> None:
    now = time.monotonic()
    q = _rate_hits.setdefault(uid, deque())
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        raise ApiError("Слишком много запросов, подождите минуту", 429)
    q.append(now)
    if len(_rate_hits) > 1000:  # чистим тех, кто давно не писал
        for k in [
            k for k, v in _rate_hits.items() if not v or now - v[-1] > RATE_WINDOW
        ]:
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
        await message.answer(
            "У вас уже есть партнёр 💞", reply_markup=open_app_keyboard()
        )
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
        "Отвязать партнёра? Все общие события будут удалены безвозвратно. Личные (приватные) события останутся у вас.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Да, отвязать", callback_data="unlink:yes"
                    ),
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
        await call.message.edit_text("Готово: связь разорвана, общие события удалены.")
        notify(partner, "💔 Партнёр отвязал вас. Общие события удалены.")
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
    msg = update.message or (
        update.callback_query.message if update.callback_query else None
    )
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
        h["Access-Control-Allow-Headers"] = (
            "Content-Type, X-Init-Data, X-TZ, If-None-Match"
        )
        h["Access-Control-Expose-Headers"] = "ETag"
        h["Access-Control-Max-Age"] = "86400"


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
        response = web.json_response(
            {"error": "Сервис временно недоступен, попробуйте позже"}, status=503
        )
    except Exception:
        logging.exception("Необработанная ошибка в API")
        response = web.json_response({"error": "Внутренняя ошибка сервера"}, status=500)
    if request.path.startswith(("/css/", "/js/")):
        response.headers["Cache-Control"] = (
            "no-cache"  # проверять обновления при каждом открытии
        )
    _add_cors(request, response)
    return response


def authenticate(request: web.Request, write: bool = False):
    raw = request.headers.get("X-Init-Data", "")
    try:
        data = safe_parse_webapp_init_data(BOT_TOKEN, raw)
    except ValueError:
        raise ApiError("unauthorized", 401)
    if (
        data.user is None
        or time.time() - data.auth_date.timestamp() > INIT_DATA_MAX_AGE
    ):
        raise ApiError("session expired", 401)
    if write:
        rate_limit(data.user.id)
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
            span = (
                date_cls.fromisoformat(date_end) - date_cls.fromisoformat(date)
            ).days
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
        if category != "rest":
            date_end = ""
        if category == "rest" and date_end:
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
        ideas_out.append(
            {
                "idx": i,
                "text": text,
                "status": own,
                "effective": effective,
                "by": r.get("user_id"),
            }
        )
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
        "ideas": ideas_out,
        "can_respond_ideas": can_respond_ideas,
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
        if (
            not m
            or not 1 <= int(m.group(2)) <= 12
            or not 2000 <= int(m.group(1)) <= 2100
        ):
            raise ApiError("Некорректный месяц")
        year, month = int(m.group(1)), int(m.group(2))
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    return f"{year:04d}-{month:02d}-01", f"{ny:04d}-{nm:02d}-01"


async def health_check(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def api_state(request: web.Request) -> web.Response:
    tg_user = authenticate(request)
    user = await db.get_or_create_user(tg_user.id)
    tz = request.headers.get("X-TZ", "").strip()
    if tz and tz != user["tz"] and get_zone(tz):
        await db.set_timezone(tg_user.id, tz)
    start, end = parse_month(request.query.get("month"))
    has_partner = bool(user["partner_id"])
    if has_partner:
        events = await db.list_events_range(tg_user.id, start, end)
        link = ""
    else:
        user = await db.ensure_invite(user)
        events = await db.list_events_range(tg_user.id, start, end)
        link = invite_link(user["invite_code"])
    try:
        sex_ideas = await db.get_sex_ideas(tg_user.id, user["partner_id"])
    except Exception:
        logging.exception("Не удалось получить идеи")
        sex_ideas = []
    stats = None
    if has_partner:
        try:
            stats = await db.pair_stats(tg_user.id, user["partner_id"])
        except Exception:
            logging.exception("Не удалось получить статистику")
    body = json.dumps(
        {
            "has_partner": has_partner,
            "invite_link": link,
            "month": start[:7],
            "events": [event_to_dict(e, tg_user.id) for e in events],
            "sex_ideas": sex_ideas,
            "stats": stats,
            "todo_colors": db.TODO_COLORS,
            "allowed_reactions": ALLOWED_REACTIONS,
            "allowed_remind_before": sorted(ALLOWED_REMIND_BEFORE),
            "my_id": tg_user.id,
            "balance": await db.get_user_balance(tg_user.id), # <--- ДОБАВИТЬ ВОТ ЭТО
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    etag = '"' + hashlib.sha1(body.encode()).hexdigest() + '"'
    headers = {"ETag": etag, "Cache-Control": "no-store"}
    if request.headers.get("If-None-Match") == etag:
        return web.Response(status=304, headers=headers)
    return web.Response(text=body, content_type="application/json", headers=headers)


async def api_create_event(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    user = await db.get_or_create_user(tg_user.id)
    data = parse_event_payload(await read_json(request))
    if data["category"] != "personal" and not user["partner_id"]:
        raise ApiError("Сначала свяжитесь с партнёром", 409)
    if await db.count_events_created(tg_user.id) >= db.MAX_EVENTS_PER_USER:
        raise ApiError("Достигнут лимит событий", 409)
    is_private_personal = (
        data["category"] == "personal" and data["visibility"] == "private"
    )
    if is_private_personal:
        target_user = tg_user.id
    elif data["category"] == "personal":
        target_user = user["partner_id"] or tg_user.id
    else:
        target_user = user["partner_id"]
    event = await db.create_event(
        tg_user.id,
        target_user,
        data["title"],
        data["description"],
        data["date"],
        data["time"],
        data["category"],
        data["items"],
        color=data["color"],
        remind_before=data["remind_before"],
        date_end=data["date_end"],
        visibility=data["visibility"],
    )
    if data["category"] == "shop":
        notify(
            user["partner_id"],
            f"🛒 Партнёр добавил список покупок на {fmt_range(data['date'], data['date_end'])}. Откройте календарь, чтобы посмотреть.",
        )
    elif data["category"] == "personal":
        if data["visibility"] == "shared" and user["partner_id"]:
            notify(
                user["partner_id"],
                f"📝 Партнёр добавил общее личное дело на {fmt_range(data['date'], data['date_end'])}{(' в ' + data['time']) if data['time'] else ''}. Откройте календарь, чтобы посмотреть.",
            )
    elif data["category"] == "todo":
        at = f" в {data['time']}" if data["time"] else ""
        extra = (
            f" (напомню за {_humanize_minutes(data['remind_before'])})"
            if data["time"] and data["remind_before"]
            else ""
        )
        notify(
            user["partner_id"],
            f"📌 Новое дело на {fmt_range(data['date'], data['date_end'])}{at}.{extra} Откройте календарь, чтобы посмотреть.",
        )
    else:
        icon = CATEGORY_ICONS.get(data["category"], "➕")
        notify(
            user["partner_id"],
            f"{icon} Новое событие на {fmt_range(data['date'], data['date_end'])}{(' в ' + data['time']) if data['time'] else ''}. Ответьте прямо здесь или откройте календарь.",
            reply_markup=event_reply_keyboard(event["id"]),
        )
    return web.json_response(event_to_dict(event, tg_user.id), status=201)


async def api_update_event(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    user = await db.get_or_create_user(tg_user.id)
    event = await member_event(request, tg_user.id)
    if event["category"] == "personal" and event["created_by"] != tg_user.id:
        raise ApiError("Редактировать личное событие может только автор", 403)
    if (
        event["category"] != "todo"
        and event["category"] != "personal"
        and event["created_by"] != tg_user.id
    ):
        raise ApiError("Редактировать событие может только его автор", 403)
    data = parse_event_payload(await read_json(request))
    if data["category"] != event["category"]:
        raise ApiError(
            "Категорию существующего события менять нельзя — создайте новое", 409
        )
    other_id = (
        event["target_user"]
        if event["created_by"] == tg_user.id
        else event["created_by"]
    )
    old_end = event.get("date_end") or ""
    if old_end == event["date"]:
        old_end = ""
    schedule_changed = (
        data["date"],
        data["date_end"],
        data["time"],
        data["remind_before"],
    ) != (event["date"], old_end, event["time"], event.get("remind_before") or 0)
    identity_changed = (data["date"], data["date_end"], data["time"]) != (
        event["date"],
        old_end,
        event["time"],
    )
    ideas_changed = data["category"] == "sex" and _extract_idea_lines(
        data["description"]
    ) != _extract_idea_lines(event["description"])
    visibility_changed = data["visibility"] != event.get("visibility", "shared")
    changed = (
        identity_changed
        or visibility_changed
        or data["description"] != event["description"]
        or data["color"] != event.get("color", "")
        or data["remind_before"] != (event.get("remind_before") or 0)
        or data["title"] != event["title"]
    )
    if not changed:
        return web.json_response(event_to_dict(event, tg_user.id))
    reset = (identity_changed or ideas_changed) and data["category"] not in (
        "shop",
        "todo",
        "personal",
    )
    updated = await db.update_event(
        event["id"],
        data["title"],
        data["description"],
        data["date"],
        data["time"],
        data["category"],
        reset_status=reset,
        color=data["color"],
        remind_before=data["remind_before"],
        date_end=data["date_end"],
        visibility=data["visibility"],
        reset_reminders=schedule_changed,
    )
    if data["category"] == "personal" and visibility_changed:
        partner_id = user["partner_id"]
        if data["visibility"] == "private":
            await db.set_target_user(event["id"], tg_user.id)
        else:
            await db.set_target_user(event["id"], partner_id or tg_user.id)
        if partner_id:
            if data["visibility"] == "shared":
                notify(
                    partner_id,
                    f"📝 Партнёр сделал личное дело общим: {fmt_range(data['date'], data['date_end'])}{title_part(data['title'])}.",
                )
            else:
                notify(
                    partner_id,
                    f"📝 Партнёр скрыл личное дело: {fmt_range(data['date'], data['date_end'])}{title_part(data['title'])}.",
                )
    if data["category"] == "shop":
        notify(
            other_id,
            f"✏️ Список покупок на {fmt_range(data['date'], data['date_end'])} изменён.",
        )
    elif data["category"] == "todo":
        notify(
            other_id,
            f"📌 Дело на {fmt_range(data['date'], data['date_end'])} изменено.",
        )
    elif data["category"] != "personal":
        suffix = " Оно снова ждёт вашего ответа." if reset else ""
        notify(
            other_id,
            f"✏️ Событие на {fmt_range(data['date'], data['date_end'])}{title_part(data['title'])} изменено.{suffix}",
        )
    updated = await db.get_event(event["id"])
    return web.json_response(event_to_dict(updated, tg_user.id))


async def api_delete_event(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    event = await member_event(request, tg_user.id)
    if event["category"] == "personal" and event["created_by"] != tg_user.id:
        raise ApiError("Удалять личное событие может только автор", 403)
    if (
        event["category"] not in ("todo", "personal")
        and event["created_by"] != tg_user.id
    ):
        raise ApiError(
            "Удалить событие может только автор. Вы можете отказаться от него.", 403
        )
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
    slot = "creator" if event["created_by"] == tg_user.id else "target"

    if event["status"] != status or comment != event.get(f"decline_comment_{slot}", ""):
        await db.set_status(event["id"], status)
        if status == "declined":
            await db.set_decline_comment_slot(event["id"], slot, comment)
        else:
            await db.set_decline_comment_slot(event["id"], slot, "")

        what = "принял(а)" if status == "accepted" else "отклонил(а)"
        

        icon = "✅" if status in ("accepted", "done") else "❌"
        extra = f"\n💬 {comment}" if status == "declined" and comment else ""

        notify(
            event["created_by"],
            f"{icon} {tg_user.first_name} {what} событие на "
            f"{fmt_range(event['date'], event.get('date_end', ''))}{title_part(event['title'])}.{extra}",
        )
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
    return web.json_response(
        event_to_dict(await db.get_event(event["id"]), tg_user.id), status=201
    )


async def api_set_item(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    event = await _shop_event(request, tg_user.id)
    checked = (await read_json(request)).get("checked")
    if not isinstance(checked, bool):
        raise ApiError("Некорректный запрос")
    if not await db.set_item_checked(
        event["id"], _int_param(request, "item_id"), checked
    ):
        raise ApiError("Пункт не найден", 404)
    return web.json_response(event_to_dict(await db.get_event(event["id"]), tg_user.id))


async def api_delete_item(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    event = await _shop_event(request, tg_user.id)
    if not await db.delete_item(event["id"], _int_param(request, "item_id")):
        raise ApiError("Пункт не найден", 404)
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
    notify(partner, "💔 Партнёр отвязал вас. Общие события удалены.")
    return web.json_response({"status": "unlinked"})
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
PHOTO_BUCKET = os.getenv("SUPABASE_BUCKET", "challenges").strip() or "challenges"
PHOTO_EXTS = ("jpg", "jpeg", "png", "webp", "gif", "heic", "heif")
CHALLENGE_MAX_POINTS = 20  # в challenges.json есть задания до 20 баллов
_supabase = None
_challenge_messages: dict[int, list[dict]] = {}


def get_supabase():
    global _supabase
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ApiError(
            "Хранилище фото не настроено (нужны SUPABASE_URL и SUPABASE_SERVICE_KEY)", 503
        )
    if _supabase is None:
        import httpx
        from supabase import create_client, ClientOptions
        try:
            options = ClientOptions(
                httpx_client=httpx.Client(
                    http2=False,
                    timeout=httpx.Timeout(30.0, connect=10.0),
                )
            )
            _supabase = create_client(SUPABASE_URL, SUPABASE_KEY, options=options)
            logging.info("Supabase: HTTP/1.1 client ACTIVE")
        except TypeError:
            logging.warning("supabase-py слишком старый, httpx_client не поддерживается")
            _supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase


def _storage_url(url: str) -> str:
    """Разные версии supabase-py отдают то полную, то относительную ссылку."""
    if not url:
        return ""
    if url.startswith("http"):
        return url
    return f"{SUPABASE_URL}/storage/v1{url if url.startswith('/') else '/' + url}"


def _photo_path_ok(path: str, challenge_id: int) -> bool:
    exts = "|".join(PHOTO_EXTS)
    return re.fullmatch(rf"challenge_{challenge_id}_\d+_[0-9a-f]{{8}}\.({exts})", path) is not None


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

    # Имя файла от клиента в путь не берём (кириллица/пробелы/«..» ломают ключ в Supabase) — только расширение
    name = str(body.get("filename") or "")
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in PHOTO_EXTS:
        ext = "jpg"
    path = f"challenge_{challenge_id}_{int(time.time())}_{secrets.token_hex(4)}.{ext}"

    sb = get_supabase()
    try:
        # supabase-py синхронный — выносим в поток, чтобы не вешать бота и API
        res = await asyncio.to_thread(sb.storage.from_(PHOTO_BUCKET).create_signed_upload_url, path)
        upload_url = _storage_url(res.get("signed_url") or res.get("signedUrl") or res.get("signedURL"))
    except Exception:
        logging.exception("Supabase: не удалось создать ссылку для загрузки")
        raise ApiError(f"Не удалось подготовить загрузку. Проверьте, что в Supabase есть бакет «{PHOTO_BUCKET}».", 502)
    if not upload_url:
        raise ApiError("Supabase не вернул ссылку для загрузки", 502)
    return web.json_response({"upload_url": upload_url, "path": path})


async def api_challenge_photo_url(request: web.Request) -> web.Response:
    tg_user = authenticate(request)
    path = request.query.get("path", "").strip()
    if not path:
        raise ApiError("Путь не указан", 400)
    # смотреть фото могут только двое участников этого челленджа
    challenge = await db.get_challenge_by_photo(path)
    if not challenge or tg_user.id not in (challenge["creator_id"], challenge["target_id"]):
        raise ApiError("Фото не найдено", 404)

    sb = get_supabase()
    try:
        res = await asyncio.to_thread(sb.storage.from_(PHOTO_BUCKET).create_signed_url, path, 3600)
        view_url = _storage_url(res.get("signedURL") or res.get("signedUrl"))
    except Exception:
        logging.exception("Supabase: не удалось создать ссылку на просмотр")
        raise ApiError("Ошибка получения ссылки на фото", 502)
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

    notify(challenge["creator_id"], f"🔥 Партнёр выполнил челлендж: «{challenge['text']}». Зайди в календарь, чтобы проверить!")
    return web.json_response({"status": "ok"})


async def api_get_challenges_data(request: web.Request) -> web.Response:
    tg_user = authenticate(request)
    balance = await db.get_user_balance(tg_user.id)
    history = await db.get_all_user_challenges(tg_user.id)
    catalog = await db.get_challenges_json()
    for h in history:
        h["is_creator"] = (h["creator_id"] == tg_user.id)
    return web.json_response({
        "balance": balance,
        "history": history,
        "catalog": catalog,
        "my_id": tg_user.id,
    })


async def api_create_challenge(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    user = await db.get_or_create_user(tg_user.id)
    if not user["partner_id"]:
        raise ApiError("Сначала свяжитесь с партнёром", 409)

    # Проверяем, сколько активных челленджей создано сегодня
    # (Берем начало текущего дня по UTC)
    now = int(time.time())
    start_of_day = now - (now % 86400)
    rs = await db._exec(
        "SELECT COUNT(*) FROM user_challenges "
        "WHERE creator_id = ? AND created_at >= ? AND status IN ('pending', 'submitted')",
        (tg_user.id, start_of_day)
    )
    active_today = rs.rows[0][0]
    if active_today >= 3:
        raise ApiError("Лимит на сегодня: можно создать не более 3 активных челленджей.", 429)

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

    challenge = await db.create_user_challenge(
        tg_user.id, user["partner_id"], category, text, points, deadline
    )
    notify(user["partner_id"], f"⚔️ Тебе бросили вызов: «{text}» ({points} б.). На выполнение {hours} ч.")
    return web.json_response({"status": "created", "challenge": challenge}, status=201)


async def api_challenge_action(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    challenge_id = _int_param(request, "id")
    challenge = await db.get_user_challenge(challenge_id)
    if not challenge or tg_user.id not in (challenge["creator_id"], challenge["target_id"]):
        raise ApiError("Челлендж не найден", 404)

    action = (await read_json(request)).get("action")
    is_creator = tg_user.id == challenge["creator_id"]

    # переход статуса атомарный: повторное нажатие «Засчитать» не начислит баллы дважды
    if action == "approve" and is_creator:
        if not await db.transition_challenge(challenge_id, ("submitted",), "approved"):
            raise ApiError("Этот вызов нельзя засчитать в текущем статусе", 409)
        await db.add_balance(challenge["target_id"], challenge["reward"])
        notify(challenge["target_id"], f"✅ Твой челлендж засчитан! Тебе начислено {challenge['reward']} баллов.")
    elif action == "reject" and is_creator:
        if not await db.transition_challenge(challenge_id, ("submitted",), "rejected"):
            raise ApiError("Этот вызов нельзя отклонить в текущем статусе", 409)
        notify(challenge["target_id"], f"❌ Челлендж отклонён: «{challenge['text']}». Можно прислать другое фото.")
    else:
        raise ApiError("Недопустимое действие", 400)
    return web.json_response({"status": "ok"})


async def api_get_challenge_messages(request: web.Request) -> web.Response:
    tg_user = authenticate(request)
    challenge_id = _int_param(request, "id")
    challenge = await db.get_user_challenge(challenge_id)
    if not challenge or tg_user.id not in (challenge["creator_id"], challenge["target_id"]):
        raise ApiError("Челлендж не найден", 404)
        
    rs = await db._exec(
        "SELECT id, sender_id, text, created_at FROM challenge_messages WHERE challenge_id = ? ORDER BY created_at ASC",
        (challenge_id,)
    )
    messages = [{"id": r[0], "sender_id": r[1], "text": r[2], "created_at": r[3]} for r in rs.rows]
    return web.json_response({"messages": messages})

async def api_post_challenge_message(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    challenge_id = _int_param(request, "id")
    challenge = await db.get_user_challenge(challenge_id)
    
    if not challenge or tg_user.id not in (challenge["creator_id"], challenge["target_id"]):
        raise ApiError("Челлендж не найден", 404)
        
    body = await read_json(request)
    text = str(body.get("text") or "").strip()[:500]
    if not text:
        raise ApiError("Введите текст", 400)
        
    await db._exec(
        "INSERT INTO challenge_messages (challenge_id, sender_id, text, created_at) VALUES (?, ?, ?, ?)",
        (challenge_id, tg_user.id, text, int(time.time()))
    )
    
    # Отправляем пуш партнёру
    partner_id = challenge["target_id"] if tg_user.id == challenge["creator_id"] else challenge["creator_id"]
    notify(partner_id, f"💬 Новое сообщение по челленджу «{challenge['text']}»:\n\n{text}")
    
    return web.json_response({"status": "ok"})

async def api_get_shop_items(request: web.Request) -> web.Response:
    authenticate(request)
    rs = await db._exec("SELECT id, title, cost, emoji FROM shop_items ORDER BY cost ASC")
    items = [{"id": r[0], "title": r[1], "cost": r[2], "emoji": r[3]} for r in rs.rows]
    return web.json_response({"items": items})

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

    # Получаем товар
    rs = await db._exec("SELECT title, cost, emoji FROM shop_items WHERE id = ?", (item_id,))
    if not rs.rows:
        raise ApiError("Товар не найден", 404)
    title, cost, emoji = rs.rows[0]

    # Проверяем баланс
    balance = await db.get_user_balance(tg_user.id)
    if balance < cost:
        raise ApiError("Недостаточно баллов для покупки", 403)

    # Списываем баллы
    await db.add_balance(tg_user.id, -cost)

    # Создаем событие в календаре (Отдых)
    event = await db.create_event(
        created_by=tg_user.id,
        target_user=user["partner_id"],
        title=f"{emoji} {title}",
        description="Куплено в магазине желаний",
        date=date,
        event_time="",
        category="rest",
    )
    
    # Уведомляем партнёра
    notify(
        user["partner_id"], 
        f"🎁 Партнёр купил желание «{title}» на {fmt_range(date, '')}. Загляните в календарь!",
        reply_markup=event_reply_keyboard(event["id"])
    )

    return web.json_response({"status": "ok", "event": event_to_dict(event, tg_user.id)})

async def index(_: web.Request) -> web.FileResponse:
    return web.FileResponse(INDEX_FILE, headers={"Cache-Control": "no-cache"})


async def send_due_reminders() -> None:
    now_utc = datetime.now(timezone.utc)
    lo = (now_utc - timedelta(days=3)).strftime("%Y-%m-%d")
    hi = (now_utc + timedelta(days=3)).strftime("%Y-%m-%d")
    for c in await db.reminder_candidates(lo, hi):
        tz = get_zone(c["tz"]) or timezone.utc
        local_now = now_utc.astimezone(tz)
        if (
            c["category"] in ("todo", "personal")
            and c["time"]
            and c.get("remind_before")
        ):
            try:
                hh, mm = c["time"].split(":")
                event_dt_local = datetime.fromisoformat(
                    f"{c['date']}T{hh}:{mm}:00"
                ).replace(tzinfo=tz)
            except Exception:
                continue
            remind_at = event_dt_local - timedelta(minutes=c["remind_before"])
            # шлём, как только время подошло и пока событие не началось — пропущенные при простое тоже уйдут
            if not (remind_at <= local_now < event_dt_local):
                continue
            if not await db.mark_reminded(c["event_id"], c["user_id"]):
                continue
            left = max(1, int((event_dt_local - local_now).total_seconds() // 60))
            prefix = "📝" if c["category"] == "personal" else "📌"
            await safe_send(
                c["user_id"],
                f"{prefix} Напоминание: {c['title']} в {c['time']} (через {_humanize_minutes(left)}).",
            )
            continue
        if (
            local_now.strftime("%Y-%m-%d") != c["date"]
            or local_now.hour < REMINDER_HOUR
        ):
            continue
        if not await db.mark_reminded(c["event_id"], c["user_id"]):
            continue
        at = f" в {c['time']}" if c["time"] else ""
        rng = fmt_range(c["date"], c.get("date_end", ""))
        if c["category"] == "todo":
            await safe_send(
                c["user_id"],
                f"📌 Сегодня дело{at}{title_part(c['title'])}. Откройте календарь, чтобы посмотреть.",
            )
        elif c["category"] == "personal":
            await safe_send(
                c["user_id"],
                f"📝 Сегодня личное дело{at}{title_part(c['title'])}. Откройте календарь, чтобы посмотреть.",
            )
        else:
            await safe_send(
                c["user_id"],
                f"⏰ Сегодня у вас запланировано событие{at}{title_part(c['title'])} (на {rng}). Откройте календарь, чтобы посмотреть детали.",
            )


async def cleanup_old_reminders() -> None:
    now = datetime.now(timezone.utc)
    cutoff_reminders = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    cutoff_todos = (now - timedelta(days=182)).strftime("%Y-%m-%d")
    try:
        n = await db.purge_old_reminders(cutoff_reminders)
        if n:
            logging.info("Очищено старых напоминаний: %s", n)
    except Exception:
        logging.exception("Не удалось очистить старые напоминания")
    try:
        n2 = await db.purge_old_todos(cutoff_todos)
        if n2:
            logging.info("Удалено старых дел (6+ мес): %s", n2)
    except Exception:
        logging.exception("Не удалось очистить старые дела")


_last_cleanup_day: str = ""


async def reminder_loop() -> None:
    global _last_cleanup_day
    while True:
        try:
            await send_due_reminders()
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
    await db.init_db()
    bot_username = (await bot.get_me()).username
    with contextlib.suppress(Exception):
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Открыть календарь"),
                BotCommand(
                    command="invite", description="Ссылка-приглашение для партнёра"
                ),
                BotCommand(command="unlink", description="Отвязать партнёра"),
            ]
        )
    app = web.Application(middlewares=[api_middleware], client_max_size=1024 * 1024)
    app.router.add_get("/", index)
    app.router.add_get("/health", health_check)
    app.router.add_static("/css", INDEX_FILE.parent / "css")
    app.router.add_static("/js", INDEX_FILE.parent / "js")
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
    app.router.add_get("/api/challenges", api_get_challenges_data)
    app.router.add_post("/api/challenges", api_create_challenge)
    app.router.add_post("/api/challenges/{id}/action", api_challenge_action)
    app.router.add_post("/api/challenges/upload-url", api_upload_photo_url)
    app.router.add_get("/api/challenges/photo-url", api_challenge_photo_url)
    app.router.add_post("/api/challenges/{id}/photo", api_set_challenge_photo)
    app.router.add_get("/api/challenges/{id}/messages", api_get_challenge_messages)
    app.router.add_post("/api/challenges/{id}/messages", api_post_challenge_message)
    app.router.add_get("/api/shop", api_get_shop_items)
    app.router.add_post("/api/shop/buy", api_buy_shop_item)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logging.info("Web app: http://0.0.0.0:%s  (public: %s)", PORT, WEBAPP_URL)
    reminders = asyncio.create_task(reminder_loop())
    try:
        await dp.start_polling(bot)
    finally:
        reminders.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reminders
        await runner.cleanup()
        await bot.session.close()
        await db.close_db()


if __name__ == "__main__":
    asyncio.run(main())
