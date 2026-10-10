import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import time
from collections import deque
from datetime import date as date_cls, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

import db

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://couple-calendar-blue.vercel.app").rstrip("/")
PORT = int(os.getenv("PORT", "8080"))
INDEX_FILE = Path(__file__).parent / "webapp" / "index.html"

REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "9"))
SHOW_TITLES = os.getenv("NOTIFY_SHOW_TITLES", "0") == "1"

CATEGORY_TITLES = {
    "rest": "Отдых",
    "sex": "Секс",
    "shop": "Магазин",
    "todo": "Дела",
}

CATEGORY_ICONS = {
    "rest": "🌿",
    "sex": "🔥",
    "shop": "🛒",
    "todo": "📌",
}

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
    o.strip().rstrip("/") for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()
}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
CATEGORIES = {"rest", "sex", "shop", "todo"}
INIT_DATA_MAX_AGE = 24 * 3600

RATE_LIMIT = 40
RATE_WINDOW = 60

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

bot_username: str = ""


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def invite_link(code: str) -> str:
    return f"https://t.me/{bot_username}?start=ref_{code}"


def open_app_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 Открыть Календарь", web_app=WebAppInfo(url=WEBAPP_URL))]
        ]
    )


def event_reply_keyboard(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Принять", callback_data=f"ev:accept:{event_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ev:decline:{event_id}"),
            ],
            [InlineKeyboardButton(text="📅 Открыть Календарь", web_app=WebAppInfo(url=WEBAPP_URL))],
        ]
    )


async def safe_send(chat_id: int, text: str, reply_markup=None) -> None:
    try:
        await bot.send_message(
            chat_id, text, reply_markup=reply_markup or open_app_keyboard()
        )
    except Exception:
        logging.exception("Не удалось отправить сообщение %s", chat_id)


_bg_tasks: set = set()


def notify(chat_id: int, text: str, reply_markup=None) -> None:
    task = asyncio.create_task(safe_send(chat_id, text, reply_markup))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def fmt_date(s: str) -> str:
    try:
        return date_cls.fromisoformat(s).strftime("%d.%m.%Y")
    except ValueError:
        return s


def title_part(title: str) -> str:
    return f" «{title}»" if SHOW_TITLES else ""


def _humanize_minutes(m: int) -> str:
    if m < 60:
        return f"{m} мин"
    if m < 1440:
        h = m // 60
        return f"{h} ч"
    d = m // 1440
    return f"{d} дн"


@lru_cache(maxsize=256)
def get_zone(name: str):
    if not name or len(name) > 64:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None


_rate_hits: dict[int, deque] = {}


def rate_limit(uid: int) -> None:
    now = time.monotonic()
    q = _rate_hits.setdefault(uid, deque())
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        raise ApiError("Слишком много запросов, подождите минуту", 429)
    q.append(now)
    if not q:
        _rate_hits.pop(uid, None)


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
        f"{notice}Привет, {me.first_name}! Это общий календарь на двоих: "
        "планируйте даты и встречи и отвечайте на них в пару касаний.",
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
        f"Отправьте эту ссылку второму человеку — после перехода по ней вы будете связаны "
        f"(действует {days} дн.):\n\n" + invite_link(user["invite_code"])
    )


@router.message(Command("unlink"))
async def cmd_unlink(message: Message):
    user = await db.get_or_create_user(message.from_user.id)
    if not user["partner_id"]:
        await message.answer("У вас нет партнёра — отвязывать нечего.")
        return
    await message.answer(
        "Отвязать партнёра? Все общие события будут удалены безвозвратно.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Да, отвязать", callback_data="unlink:yes"),
                    InlineKeyboardButton(text="Отмена", callback_data="unlink:no"),
                ]
            ]
        ),
    )


@router.callback_query(F.data == "unlink:no")
async def unlink_cancel(call: CallbackQuery):
    await call.message.edit_text("Отменено.")
    await call.answer()


@router.callback_query(F.data == "unlink:yes")
async def unlink_confirm(call: CallbackQuery):
    partner = await db.unlink_partners(call.from_user.id)
    if partner is None:
        await call.message.edit_text("У вас нет партнёра.")
    else:
        await call.message.edit_text("Готово: связь разорвана, общие события удалены.")
        notify(partner, "💔 Партнёр отвязал вас. Общие события удалены.")
    await call.answer()


@router.callback_query(F.data.startswith("ev:"))
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

    if event["category"] in ("shop", "todo"):
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
        f"{icon} {who} ответил(а) на событие {fmt_date(event['date'])}{title_part(event['title'])}.",
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


def parse_event_payload(body: dict) -> dict:
    description = str(body.get("description", "")).strip()
    date = str(body.get("date", ""))
    event_time = str(body.get("time") or "").strip()
    category = str(body.get("category", "rest"))
    color = str(body.get("color", "")).strip()

    try:
        remind_before = int(body.get("remind_before", 0))
    except (TypeError, ValueError):
        remind_before = 0

    if category not in CATEGORIES:
        raise ApiError("Неизвестная категория")
    if len(description) > 2000:
        raise ApiError("Слишком длинный текст")
    if not DATE_RE.match(date):
        raise ApiError("Некорректная дата")
    try:
        year = date_cls.fromisoformat(date).year
    except ValueError:
        raise ApiError("Некорректная дата")
    if not 2000 <= year <= 2100:
        raise ApiError("Некорректная дата")
    if event_time and not TIME_RE.match(event_time):
        raise ApiError("Некорректное время")

    if category == "todo":
        if color not in db.TODO_COLORS:
            raise ApiError("Выберите цвет для дела")
        if remind_before not in ALLOWED_REMIND_BEFORE:
            raise ApiError("Недопустимое время напоминания")
        if remind_before and not event_time:
            raise ApiError("Напоминание возможно только если указано время")
    else:
        color = ""
        remind_before = 0

    title = CATEGORY_TITLES.get(category, "Событие")

    return {
        "title": title,
        "description": description,
        "date": date,
        "time": event_time,
        "category": category,
        "color": color,
        "remind_before": remind_before,
        "items": normalize_items(body.get("checklist")) if category == "shop" else [],
    }


def event_to_dict(e: dict, uid: int) -> dict:
    is_creator = e["created_by"] == uid
    partner_id = e["target_user"] if is_creator else e["created_by"]
    reactions = e.get("reactions", {}) or {}
    my_reaction = reactions.get(uid, "")
    partner_reaction = reactions.get(partner_id, "")
    return {
        "id": e["id"],
        "title": e["title"],
        "description": e["description"],
        "date": e["date"],
        "time": e["time"],
        "category": e["category"],
        "status": e["status"],
        "color": e.get("color", ""),
        "remind_before": e.get("remind_before", 0),
        "reaction_mine": my_reaction,
        "reaction_partner": partner_reaction,
        "decline_comment_creator": e.get("decline_comment_creator", ""),
        "decline_comment_target": e.get("decline_comment_target", ""),
        "my_comment_slot": "creator" if is_creator else "target",
        "my_decline_comment": (
            e.get("decline_comment_creator", "") if is_creator
            else e.get("decline_comment_target", "")
        ),
        "is_creator": is_creator,
        "checklist": e["checklist"],
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
        events = []
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
    if not user["partner_id"]:
        raise ApiError("Сначала свяжитесь с партнёром", 409)

    data = parse_event_payload(await read_json(request))
    if await db.count_events_created(tg_user.id) >= db.MAX_EVENTS_PER_USER:
        raise ApiError("Достигнут лимит событий", 409)

    event = await db.create_event(
        tg_user.id, user["partner_id"], data["title"], data["description"],
        data["date"], data["time"], data["category"], data["items"],
        color=data["color"], remind_before=data["remind_before"],
    )

    if data["category"] == "shop":
        notify(
            user["partner_id"],
            f"🛒 Партнёр добавил список покупок на {fmt_date(data['date'])}. "
            "Откройте календарь, чтобы посмотреть.",
        )
    elif data["category"] == "todo":
        icon = CATEGORY_ICONS.get("todo", "📌")
        at = f" в {data['time']}" if data["time"] else ""
        extra = ""
        if data["time"] and data["remind_before"]:
            extra = f" (напомню за {_humanize_minutes(data['remind_before'])})"
        notify(
            user["partner_id"],
            f"{icon} Новое дело на {fmt_date(data['date'])}{at}.{extra} "
            "Откройте календарь, чтобы посмотреть.",
        )
    else:
        icon = CATEGORY_ICONS.get(data["category"], "➕")
        notify(
            user["partner_id"],
            f"{icon} Новое событие на {fmt_date(data['date'])}"
            f"{(' в ' + data['time']) if data['time'] else ''}. "
            "Ответьте прямо здесь или откройте календарь.",
            reply_markup=event_reply_keyboard(event["id"]),
        )
    return web.json_response(event_to_dict(event, tg_user.id), status=201)


async def api_update_event(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    event = await member_event(request, tg_user.id)
    if event["category"] != "todo" and event["created_by"] != tg_user.id:
        raise ApiError("Редактировать событие может только его автор", 403)

    data = parse_event_payload(await read_json(request))
    identity_changed = (
        data["title"], data["category"], data["date"], data["time"]
    ) != (event["title"], event["category"], event["date"], event["time"])
    changed = identity_changed or data["description"] != event["description"] \
              or data["color"] != event.get("color", "") \
              or data["remind_before"] != (event.get("remind_before") or 0)
    if not changed:
        return web.json_response(event_to_dict(event, tg_user.id))

    reset = identity_changed and data["category"] not in ("shop", "todo")
    updated = await db.update_event(
        event["id"], data["title"], data["description"], data["date"],
        data["time"], data["category"], reset_status=reset,
        color=data["color"], remind_before=data["remind_before"],
    )
    if data["category"] == "shop":
        notify(
            event["target_user"],
            f"✏️ Список покупок на {fmt_date(data['date'])} изменён.",
        )
    elif data["category"] == "todo":
        notify(
            event["target_user"],
            f"📌 Дело на {fmt_date(data['date'])} изменено.",
        )
    else:
        suffix = " Оно снова ждёт вашего ответа." if identity_changed else ""
        notify(
            event["target_user"],
            f"✏️ Событие на {fmt_date(data['date'])}{title_part(data['title'])} изменено.{suffix}",
        )
    return web.json_response(event_to_dict(updated, tg_user.id))


async def api_delete_event(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    event = await member_event(request, tg_user.id)
    if event["category"] != "todo" and event["created_by"] != tg_user.id:
        raise ApiError("Удалить событие может только его автор. Вы можете отказаться от него.", 403)

    await db.delete_event(event["id"])
    return web.json_response({"status": "deleted"})


async def api_respond(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    event = await member_event(request, tg_user.id)
    body = await read_json(request)

    if event["category"] in ("shop", "todo"):
        raise ApiError("Это не требует ответа")

    status = body.get("status")
    if status not in ("accepted", "declined"):
        raise ApiError("Некорректный статус")
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
        icon = "✅" if status == "accepted" else "❌"
        extra = f"\n💬 {comment}" if status == "declined" and comment else ""
        notify(
            event["created_by"],
            f"{icon} {tg_user.first_name} {what} событие на "
            f"{fmt_date(event['date'])}{title_part(event['title'])}.{extra}",
        )
        event = await db.get_event(event["id"])
    return web.json_response(event_to_dict(event, tg_user.id))


async def api_set_reaction(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    event = await member_event(request, tg_user.id)
    body = await read_json(request)
    emoji = str(body.get("emoji") or "").strip()

    if event["category"] in ("shop", "todo"):
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
    if event["category"] in ("shop", "todo"):
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
            body_text = (
                f"💬 {tg_user.first_name} оставил(а) комментарий к событию "
                f"на {fmt_date(event['date'])}{at}{title_part(event['title'])}:\n\n{comment}"
            )
        else:
            body_text = (
                f"💬 {tg_user.first_name} удалил(а) свой комментарий к событию "
                f"на {fmt_date(event['date'])}{at}{title_part(event['title'])}."
            )
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
    return web.json_response(event_to_dict(await db.get_event(event["id"]), tg_user.id), status=201)


async def api_set_item(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    event = await _shop_event(request, tg_user.id)
    checked = (await read_json(request)).get("checked")
    if not isinstance(checked, bool):
        raise ApiError("Некорректный запрос")
    if not await db.set_item_checked(event["id"], _int_param(request, "item_id"), checked):
        raise ApiError("Пункт не найден", 404)
    return web.json_response(event_to_dict(await db.get_event(event["id"]), tg_user.id))


async def api_delete_item(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    event = await _shop_event(request, tg_user.id)
    if not await db.delete_item(event["id"], _int_param(request, "item_id")):
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
    out = [{
        "id": e["id"],
        "title": e["title"],
        "description": e["description"],
        "date": e["date"],
        "time": e["time"],
        "category": e["category"],
        "status": e["status"],
        "color": e.get("color", ""),
        "is_creator": e["created_by"] == tg_user.id,
        "checklist": [],
    } for e in items]
    return web.json_response({"items": out})


async def api_unlink(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    partner = await db.unlink_partners(tg_user.id)
    if partner is None:
        raise ApiError("У вас нет партнёра", 409)
    notify(partner, "💔 Партнёр отвязал вас. Общие события удалены.")
    return web.json_response({"status": "unlinked"})


async def index(_: web.Request) -> web.FileResponse:
    return web.FileResponse(INDEX_FILE, headers={"Cache-Control": "no-cache"})


async def send_due_reminders() -> None:
    now_utc = datetime.now(timezone.utc)
    lo = (now_utc - timedelta(days=3)).strftime("%Y-%m-%d")
    hi = (now_utc + timedelta(days=3)).strftime("%Y-%m-%d")

    for c in await db.reminder_candidates(lo, hi):
        tz = get_zone(c["tz"]) or timezone.utc
        local_now = now_utc.astimezone(tz)

        if c["category"] == "todo" and c["time"] and c.get("remind_before"):
            try:
                hh, mm = c["time"].split(":")
                event_dt_local = datetime.fromisoformat(
                    f"{c['date']}T{hh}:{mm}:00"
                ).replace(tzinfo=tz)
            except Exception:
                continue
            remind_at = event_dt_local - timedelta(minutes=c["remind_before"])
            if not (remind_at <= local_now < remind_at + timedelta(minutes=2)):
                continue
            if not await db.mark_reminded(c["event_id"], c["user_id"]):
                continue
            await safe_send(
                c["user_id"],
                f"📌 Напоминание: {c['title']} в {c['time']} "
                f"(через {_humanize_minutes(c['remind_before'])}).",
            )
            continue

        if local_now.strftime("%Y-%m-%d") != c["date"] or local_now.hour < REMINDER_HOUR:
            continue
        if not await db.mark_reminded(c["event_id"], c["user_id"]):
            continue
        at = f" в {c['time']}" if c["time"] else ""
        if c["category"] == "todo":
            await safe_send(
                c["user_id"],
                f"📌 Сегодня дело{at}{title_part(c['title'])}. "
                "Откройте календарь, чтобы посмотреть.",
            )
        else:
            await safe_send(
                c["user_id"],
                f"⏰ Сегодня у вас запланировано событие{at}{title_part(c['title'])}. "
                "Откройте календарь, чтобы посмотреть детали.",
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
                BotCommand(command="invite", description="Ссылка-приглашение для партнёра"),
                BotCommand(command="unlink", description="Отвязать партнёра"),
            ]
        )

    app = web.Application(middlewares=[api_middleware])
    app.router.add_get("/", index)
    app.router.add_get("/health", health_check)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/archive.json", api_archive)
    app.router.add_get("/api/todos/archive", api_archived_todos)
    app.router.add_post("/api/events", api_create_event)
    app.router.add_put("/api/events/{id}", api_update_event)
    app.router.add_delete("/api/events/{id}", api_delete_event)
    app.router.add_post("/api/events/{id}/respond", api_respond)
    app.router.add_post("/api/events/{id}/reaction", api_set_reaction)
    app.router.add_post("/api/events/{id}/comment", api_set_decline_comment)
    app.router.add_post("/api/events/{id}/items", api_add_item)
    app.router.add_put("/api/events/{id}/items/{item_id}", api_set_item)
    app.router.add_delete("/api/events/{id}/items/{item_id}", api_delete_item)
    app.router.add_post("/api/sex-ideas", api_add_sex_idea)
    app.router.add_delete("/api/sex-ideas/{id}", api_delete_sex_idea)
    app.router.add_post("/api/unlink", api_unlink)

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
