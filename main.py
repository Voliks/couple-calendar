import asyncio
import logging
import os
import re
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

import db

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://couple-calendar-blue.vercel.app").rstrip("/")
PORT = int(os.getenv("PORT", "8080"))
INDEX_FILE = Path(__file__).parent / "webapp" / "index.html"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INIT_DATA_MAX_AGE = 24 * 3600

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

bot_username: str = ""


def invite_link(code: str) -> str:
    return f"https://t.me/{bot_username}?start=ref_{code}"


def open_app_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 Открыть Календарь", web_app=WebAppInfo(url=WEBAPP_URL))]
        ]
    )


async def safe_send(chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text, reply_markup=open_app_keyboard())
    except Exception:
        logging.exception("Не удалось отправить сообщение %s", chat_id)


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
        elif user["partner_id"] == inviter["partner_id"] and user["partner_id"] is not None:
            notice = "Вы уже связаны с этим партнёром 💞\n\n"
        elif user["partner_id"] or inviter["partner_id"]:
            notice = "⚠️ У одного из вас уже есть пара, связать не получилось.\n\n"
        else:
            await db.link_partners(me.id, inviter["telegram_id"])
            notice = "💞 Готово! Вы связаны с партнёром.\n\n"
            await safe_send(
                inviter["telegram_id"],
                f"💞 {me.full_name} принял(а) ваше приглашение. Теперь у вас общий календарь!",
            )

    await message.answer(
        f"{notice}Привет, {me.first_name}! Это общий календарь на двоих.",
        reply_markup=open_app_keyboard(),
    )


@router.message(Command("invite"))
async def cmd_invite(message: Message):
    user = await db.get_or_create_user(message.from_user.id)
    if user["partner_id"]:
        await message.answer("У вас уже есть партнёр 💞", reply_markup=open_app_keyboard())
        return
    await message.answer(
        "Отправьте эту ссылку второму человеку:\n\n" + invite_link(user["invite_code"])
    )


def json_error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


def authenticate(request: web.Request):
    raw = request.headers.get("X-Init-Data", "")
    try:
        data = safe_parse_webapp_init_data(BOT_TOKEN, raw)
    except ValueError:
        raise web.HTTPUnauthorized(text='{"error":"unauthorized"}', content_type="application/json")
    if data.user is None or time.time() - data.auth_date.timestamp() > INIT_DATA_MAX_AGE:
        raise web.HTTPUnauthorized(text='{"error":"session expired"}', content_type="application/json")
    return data.user


def event_to_dict(e: dict, uid: int) -> dict:
    return {
        "id": e["id"],
        "category": e["category"],
        "title": e["title"],
        "description": e["description"],
        "date": e["date"],
        "status": e["status"],
        "items": e.get("items", []),
        "is_creator": e["created_by"] == uid,
    }


async def api_state(request: web.Request) -> web.Response:
    tg_user = authenticate(request)
    user = await db.get_or_create_user(tg_user.id)
    has_partner = bool(user["partner_id"])
    events = await db.list_events(tg_user.id) if has_partner else []
    return web.json_response({
        "me": {"id": tg_user.id},
        "has_partner": has_partner,
        "invite_link": invite_link(user["invite_code"]),
        "events": [event_to_dict(e, tg_user.id) for e in events],
    })


async def api_create_event(request: web.Request) -> web.Response:
    tg_user = authenticate(request)
    user = await db.get_or_create_user(tg_user.id)
    if not user["partner_id"]:
        return json_error("Сначала свяжитесь с партнёром", 409)

    body = await request.json()
    category = body.get("category", "Свободное время")
    title = str(body.get("title", "")).strip() or category
    description = str(body.get("description", "")).strip()
    date = str(body.get("date", ""))
    items = body.get("items", [])

    if not DATE_RE.match(date):
        return json_error("Некорректная дата")

    event = await db.create_event(tg_user.id, user["partner_id"], category, title, description, date, items)
    await safe_send(user["partner_id"], f"➕ Новое событие [{category}] на {date}: {title}!")
    return web.json_response(event_to_dict(event, tg_user.id), status=201)


async def api_respond(request: web.Request) -> web.Response:
    tg_user = authenticate(request)
    event_id = int(request.match_info["id"])
    body = await request.json()
    status = body.get("status")

    if status not in ("accepted", "declined"):
        return json_error("Некорректный статус")

    event = await db.get_event(event_id)
    if not event or event["target_user"] != tg_user.id:
        return json_error("Доступ запрещен", 403)

    await db.set_status(event_id, status)
    text = f"✅ {tg_user.first_name} согласился(лась) на {event['title']}!" if status == "accepted" else f"❌ {tg_user.first_name} отклонил(а) {event['title']}."
    await safe_send(event["created_by"], text)

    updated_event = await db.get_event(event_id)
    return web.json_response(event_to_dict(updated_event, tg_user.id))


async def api_toggle_item(request: web.Request) -> web.Response:
    authenticate(request)
    item_id = int(request.match_info["id"])
    await db.toggle_checklist_item(item_id)
    return web.json_response({"ok": True})


async def api_add_item(request: web.Request) -> web.Response:
    authenticate(request)
    event_id = int(request.match_info["id"])
    body = await request.json()
    text = str(body.get("text", "")).strip()
    if text:
        await db.add_checklist_item(event_id, text)
    event = await db.get_event(event_id)
    return web.json_response(event_to_dict(event, 0))


async def index(_: web.Request) -> web.FileResponse:
    return web.FileResponse(INDEX_FILE, headers={"Cache-Control": "no-cache"})


@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        response = web.Response()
    else:
        response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Init-Data"
    return response


async def main() -> None:
    global bot_username
    logging.basicConfig(level=logging.INFO)
    await db.init_db()
    bot_username = (await bot.get_me()).username

    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/", index)
    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/events", api_create_event)
    app.router.add_post("/api/events/{id}/respond", api_respond)
    app.router.add_post("/api/items/{id}/toggle", api_toggle_item)
    app.router.add_post("/api/events/{id}/items", api_add_item)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logging.info(f"Сервер запущен на порту {PORT}")

    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
