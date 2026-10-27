"""Общие настройки, константы и хелперы (используются main.py и voice.py)."""
import asyncio
import logging
import os
import re
from datetime import date as date_cls
from functools import lru_cache
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from dotenv import load_dotenv

# .env нужно прочитать до импорта db (он читает TURSO_* при импорте)
load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Не задана обязательная переменная окружения {name} (см. .env.example)")
    return value


BOT_TOKEN = _require("BOT_TOKEN")
WEBAPP_URL = _require("WEBAPP_URL").rstrip("/")

bot = Bot(token=BOT_TOKEN)

CATEGORY_TITLES = {"rest": "Отдых", "sex": "Секс", "shop": "Магазин", "todo": "Дела", "personal": "Личное"}
CATEGORY_ICONS = {"rest": "🌿", "sex": "🔥", "shop": "🛒", "todo": "📌", "personal": "📝"}
CATEGORIES = set(CATEGORY_TITLES)

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def valid_date(s: str) -> bool:
    if not DATE_RE.match(s):
        return False
    try:
        year = date_cls.fromisoformat(s).year
    except ValueError:
        return False
    return 2000 <= year <= 2100


def fmt_date(s: str) -> str:
    try:
        return date_cls.fromisoformat(s).strftime("%d.%m.%Y")
    except ValueError:
        return s


def fmt_range(date_start: str, date_end: str) -> str:
    if not date_end or date_end == date_start:
        return fmt_date(date_start)
    return f"{fmt_date(date_start)} – {fmt_date(date_end)}"


def humanize_minutes(m: int) -> str:
    if m < 60:
        return f"{m} мин"
    if m < 1440:
        return f"{m // 60} ч"
    return f"{m // 1440} дн"


@lru_cache(maxsize=256)
def get_zone(name: str):
    if not name or len(name) > 64:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def open_app_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📅 Открыть Календарь", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])


def event_reply_keyboard(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Принять", callback_data=f"ev:accept:{event_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ev:decline:{event_id}"),
        ],
        [InlineKeyboardButton(text="📅 Открыть Календарь", web_app=WebAppInfo(url=WEBAPP_URL))],
    ])


async def safe_send(chat_id: int, text: str, reply_markup=None) -> None:
    try:
        await bot.send_message(chat_id, text, reply_markup=reply_markup or open_app_keyboard())
    except Exception:
        logging.exception("Не удалось отправить сообщение %s", chat_id)


_bg_tasks: set = set()


def notify(chat_id: int, text: str, reply_markup=None) -> None:
    """Отправить сообщение в фоне, не блокируя ответ API."""
    task = asyncio.create_task(safe_send(chat_id, text, reply_markup))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
