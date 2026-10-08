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


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject) -> None:
    user = await db.get_or_create_user(message.from_user.id)
    arg = command.args or ""

    if arg.startswith("ref_"):
        code = arg[4:].strip()
        if code and code != user["invite_code"]:
            inviter = await db.get_user_by_code(code)
            if inviter and inviter["telegram_id"] != user["telegram_id"]:
                await db.link_partners(user["telegram_id"], inviter["telegram_id"])
                user = await db.get_user(user["telegram_id"])
                await message.answer(
                    "🎉 Вы успешно связали календари! Теперь вы видите планы друг друга.",
                    reply_markup=open_app_keyboard(),
                )
                try:
                    await bot.send_message(
                        inviter["telegram_id"],
                        "🎉 Ваша половинка подключилась! Календарь общий.",
                        reply_markup=open_app_keyboard(),
                    )
                except Exception:
                    pass
                return

    if user["partner_id"]:
        txt = "❤️ Календарь готов к работе. Нажмите кнопку ниже, чтобы открыть."
    else:
        txt = (
            "Привет! Это общий календарь для двоих.\n\n"
            f"Отправь партнеру эту ссылку, чтобы связать аккаунты:\n"
            f"`{invite_link(user['invite_code'])}`"
        )

    await message.answer(txt, parse_mode="Markdown", reply_markup=open_app_keyboard())


@router.message(Command("invite"))
async def cmd_invite(message: Message) -> None:
    user = await db.get_or_create_user(message.from_user.id)
    await message.answer(
        f"Ваша ссылка для приглашения партнёра:\n`{invite_link(user['invite_code'])}`",
        parse_mode="Markdown",
    )


async def auth_user(request: web.Request) -> dict:
    init_data = request.headers.get("X-Init-Data")
    if not init_data:
        raise web.HTTPUnauthorized(reason="Missing initData")
    try:
        parsed = safe_parse_webapp_init_data(BOT_TOKEN, init_data)
    except ValueError:
        raise web.HTTPUnauthorized(reason="Invalid initData signature")

    if time.time() - parsed.auth_date.timestamp() > INIT_DATA_MAX_AGE:
        raise web.HTTPUnauthorized(reason="initData expired")

    user = await db.get_or_create_user(parsed.user.id)
    return user


async def health_check(request: web.Request) -> web.Response:
    return web.Response(text="OK", status=200)


async def api_state(request: web.Request) -> web.Response:
    user = await auth_user(request)
    partner = await db.get_user(user["partner_id"]) if user["partner_id"] else None
    events = await db.list_events(user["telegram_id"])
    return web.json_response({
        "me": {"id": user["telegram_id"]},
        "partner": {"id": partner["telegram_id"]} if partner else None,
        "invite_link": invite_link(user["invite_code"]),
        "events": events,
    })


async def api_create_event(request: web.Request) -> web.Response:
    user = await auth_user(request)
    if not user["partner_id"]:
        return web.json_response({"error": "Партнёр не привязан"}, status=400)

    data = await request.json()
    category = data.get("category", "Свободное время")
    title = data.get("title", "").strip() or category
    description = data.get("description", "").strip()
    date = data.get("date", "").strip()
    items = data.get("items", [])

    if not DATE_RE.match(date):
        return web.json_response({"error": "Неверный формат даты (ГГГГ-ММ-ДД)"}, status=400)

    event = await db.create_event(
        created_by=user["telegram_id"],
        target_user=user["partner_id"],
        category=category,
        title=title,
        description=description,
        date=date,
        items=items,
    )
    return web.json_response({"event": event})


async def api_respond(request: web.Request) -> web.Response:
    user = await auth_user(request)
    event_id = int(request.match_info["id"])
    data = await request.json()
    status = data.get("status")
    if status not in ("accepted", "declined"):
        return web.json_response({"error": "Некорректный статус"}, status=400)

    event = await db.get_event(event_id)
    if not event or event["target_user"] != user["telegram_id"]:
        return web.json_response({"error": "Событие не найдено"}, status=404)

    await db.set_status(event_id, status)
    event = await db.get_event(event_id)
    return web.json_response({"event": event})


async def api_toggle_item(request: web.Request) -> web.Response:
    user = await auth_user(request)
    item_id = int(request.match_info["id"])
    await db.toggle_checklist_item(item_id)
    return web.json_response({"ok": True})


async def api_add_item(request: web.Request) -> web.Response:
    user = await auth_user(request)
    event_id = int(request.match_info["id"])
    data = await request.json()
    text = data.get("text", "").strip()
    if text:
        await db.add_checklist_item(event_id, text)
    event = await db.get_event(event_id)
    return web.json_response({"event": event})


async def index(request: web.Request) -> web.FileResponse:
    if not INDEX_FILE.exists():
        raise web.HTTPNotFound(reason="index.html not found")
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
    app.router.add_get("/health", health_check)
    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/events", api_create_event)
    app.router.add_post("/api/events/{id}/respond", api_respond)
    app.router.add_post("/api/items/{id}/toggle", api_toggle_item)
    app.router.add_post("/api/events/{id}/items", api_add_item)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logging.info(f"Web app запущена на порту {PORT}")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
