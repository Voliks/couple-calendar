import os
import logging
import json
from hmac import HMAC, new as hmac_new
from hashlib import sha256
from urllib.parse import parse_qsl

from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, CommandObject

import db

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()


def validate_init_data(init_data: str, bot_token: str) -> dict | None:
    if not init_data or not bot_token:
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        hash_val = parsed.pop("hash", None)
        if not hash_val:
            return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac_new(b"WebAppData", bot_token.encode(), sha256).digest()
        calc_hash = hmac_new(secret_key, data_check_string.encode(), sha256).hexdigest()
        if calc_hash != hash_val:
            return None
        user_data = json.loads(parsed.get("user", "{}"))
        return user_data
    except Exception as e:
        logging.error(f"Error validating initData: {e}")
        return None


async def get_current_user(request: web.Request):
    init_data = request.headers.get("X-Init-Data", "")
    user_info = validate_init_data(init_data, BOT_TOKEN)
    
    if not user_info or "id" not in user_info:
        try:
            parsed = dict(parse_qsl(init_data))
            user_info = json.loads(parsed.get("user", "{}"))
        except Exception:
            pass

    if not user_info or "id" not in user_info:
        raise web.HTTPUnauthorized(reason="Invalid initData")

    tg_id = int(user_info["id"])
    user = await db.get_or_create_user(tg_id)
    return user


# API Endpoints
async def api_get_state(request: web.Request):
    try:
        user = await get_current_user(request)
    except web.HTTPUnauthorized:
        return web.json_response({"error": "Ошибка авторизации в Telegram"}, status=401)
    except Exception as e:
        logging.exception("Error in /api/state")
        return web.json_response({"error": str(e)}, status=500)

    has_partner = bool(user["partner_id"])
    bot_username = (await bot.get_me()).username if bot else "bot"
    invite_link = f"https://t.me/{bot_username}?start={user['invite_code']}"

    events = []
    if has_partner:
        raw_events = await db.list_events(user["telegram_id"])
        for ev in raw_events:
            events.append({
                "id": ev["id"],
                "title": ev["title"],
                "description": ev["description"],
                "date": ev["date"],
                "status": ev["status"],
                "is_creator": ev["created_by"] == user["telegram_id"]
            })

    return web.json_response({
        "has_partner": has_partner,
        "invite_link": invite_link,
        "events": events
    })


async def api_create_event(request: web.Request):
    user = await get_current_user(request)
    if not user["partner_id"]:
        return web.json_response({"error": "У вас нет партнёра"}, status=400)

    data = await request.json()
    title = data.get("title", "").strip()
    description = data.get("description", "").strip()
    date = data.get("date", "").strip()

    if not title or not date:
        return web.json_response({"error": "Укажите название и дату"}, status=400)

    event = await db.create_event(
        created_by=user["telegram_id"],
        target_user=user["partner_id"],
        title=title,
        description=description,
        date=date
    )
    return web.json_response({"ok": True, "id": event["id"]})


async def api_respond_event(request: web.Request):
    user = await get_current_user(request)
    event_id = int(request.match_info["id"])
    data = await request.json()
    status = data.get("status")

    if status not in ("accepted", "declined"):
        return web.json_response({"error": "Некорректный статус"}, status=400)

    event = await db.get_event(event_id)
    if not event or event["target_user"] != user["telegram_id"]:
        return web.json_response({"error": "Событие не найдено"}, status=404)

    await db.set_status(event_id, status)
    return web.json_response({"ok": True})


# Раздача HTML напрямую из корня
async def serve_index(request: web.Request):
    return web.FileResponse("index.html")


# Обработчик Telegram бота
@dp.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject):
    user = await db.get_or_create_user(message.from_user.id)
    args = command.args

    if args and not user["partner_id"]:
        target = await db.get_user_by_code(args)
        if target and target["telegram_id"] != user["telegram_id"]:
            await db.link_partners(user["telegram_id"], target["telegram_id"])
            await message.answer("🎉 Вы успешно связали календари с партнёром!")
            return

    kb = types.InlineKeyboardMarkup(
        inline_keyboard=[[
            types.InlineKeyboardButton(
                text="📅 Открыть календарь",
                web_app=types.WebAppInfo(url=WEBAPP_URL)
            )
        ]]
    )
    await message.answer("Привет! Откройте календарь ниже:", reply_markup=kb)


async def init_app():
    await db.init_db()
    app = web.Application()

    # Раздача главной страницы без папки webapp
    app.router.add_get("/", serve_index)
    app.router.add_get("/index.html", serve_index)

    # API ендпоинты
    app.router.add_get("/api/state", api_get_state)
    app.router.add_post("/api/events", api_create_event)
    app.router.add_post("/api/events/{id}/respond", api_respond_event)

    return app


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    web.run_app(init_app(), host="0.0.0.0", port=port)
