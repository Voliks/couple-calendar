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

import db  # noqa: E402  (после load_dotenv, чтобы подхватить DB_PATH)

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://couple-calendar-blue.vercel.app").rstrip("/")
PORT = int(os.getenv("PORT", "8080"))
INDEX_FILE = Path(__file__).parent / "webapp" / "index.html"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INIT_DATA_MAX_AGE = 24 * 3600  # секунд

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
    except Exception:  # пользователь мог заблокировать бота
        logging.exception("Не удалось отправить сообщение %s", chat_id)


# ───────────────────────── Telegram-бот ─────────────────────────


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
        else:
            await db.link_partners(me.id, inviter["telegram_id"])
            notice = "💞 Готово! Вы связаны с партнёром.\n\n"
            await safe_send(
                inviter["telegram_id"],
                f"💞 {me.full_name} принял(а) ваше приглашение. Теперь у вас общий календарь!",
            )

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
    await message.answer(
        "Отправьте эту ссылку второму человеку — после перехода по ней вы будете связаны:\n\n"
        + invite_link(user["invite_code"])
    )


# ───────────────────────── HTTP API ─────────────────────────


def json_error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


def authenticate(request: web.Request):
    """Проверяет подпись initData от Telegram и возвращает пользователя."""
    raw = request.headers.get("X-Init-Data", "")
    try:
        data = safe_parse_webapp_init_data(BOT_TOKEN, raw)
    except ValueError:
        raise web.HTTPUnauthorized(
            text='{"error":"unauthorized"}', content_type="application/json"
        )
    if data.user is None or time.time() - data.auth_date.timestamp() > INIT_DATA_MAX_AGE:
        raise web.HTTPUnauthorized(
            text='{"error":"session expired"}', content_type="application/json"
        )
    return data.user


def event_to_dict(row, uid: int) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "date": row["date"],
        "status": row["status"],
        "is_creator": row["created_by"] == uid,
    }


async def api_state(request: web.Request) -> web.Response:
    tg_user = authenticate(request)
    user = await db.get_or_create_user(tg_user.id)
    has_partner = bool(user["partner_id"])
    events = await db.list_events(tg_user.id) if has_partner else []
    return web.json_response(
        {
            "has_partner": has_partner,
            "invite_link": invite_link(user["invite_code"]),
            "events": [event_to_dict(e, tg_user.id) for e in events],
        }
    )


async def api_create_event(request: web.Request) -> web.Response:
    tg_user = authenticate(request)
    user = await db.get_or_create_user(tg_user.id)
    if not user["partner_id"]:
        return json_error("Сначала свяжитесь с партнёром", 409)

    try:
        body = await request.json()
    except Exception:
        return json_error("Некорректный запрос")

    title = str(body.get("title", "")).strip()
    description = str(body.get("description", "")).strip()
    date = str(body.get("date", ""))
    if not title:
        return json_error("Введите название")
    if len(title) > 200 or len(description) > 2000:
        return json_error("Слишком длинный текст")
    if not DATE_RE.match(date):
        return json_error("Некорректная дата")

    event = await db.create_event(tg_user.id, user["partner_id"], title, description, date)
    await safe_send(
        user["partner_id"],
        f"➕ Новое событие на {date}: {title}! Откройте календарь для ответа.",
    )
    return web.json_response(event_to_dict(event, tg_user.id), status=201)


async def api_respond(request: web.Request) -> web.Response:
    tg_user = authenticate(request)
    try:
        event_id = int(request.match_info["id"])
        body = await request.json()
    except Exception:
        return json_error("Некорректный запрос")

    status = body.get("status")
    if status not in ("accepted", "declined"):
        return json_error("Некорректный статус")

    event = await db.get_event(event_id)
    if event is None:
        return json_error("Событие не найдено", 404)
    # Отвечать может только адресат и только на событие в статусе pending
    if event["target_user"] != tg_user.id:
        return json_error("Нет доступа", 403)
    if event["status"] != "pending":
        return json_error("Вы уже ответили на это событие", 409)

    await db.set_status(event_id, status)
    if status == "accepted":
        text = f"✅ {tg_user.first_name} согласился на событие {event['title']}!"
    else:
        text = f"❌ {tg_user.first_name} отклонил событие {event['title']}!"
    await safe_send(event["created_by"], text)

    event = await db.get_event(event_id)
    return web.json_response(event_to_dict(event, tg_user.id))


async def index(_: web.Request) -> web.FileResponse:
    return web.FileResponse(INDEX_FILE, headers={"Cache-Control": "no-cache"})


# ───────────────────────── CORS Middleware ─────────────────────────


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


# ───────────────────────── Запуск ─────────────────────────


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

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logging.info("Web app: http://0.0.0.0:%s  (public: %s)", PORT, WEBAPP_URL)

    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
