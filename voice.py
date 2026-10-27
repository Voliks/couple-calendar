"""Голосовые заметки: Telegram voice → Groq Whisper → Groq LLM → событие в календаре.

Внимание: аудио и распознанный текст отправляются во внешний сервис Groq.
Без GROQ_API_KEY обработчик просто сообщает, что функция не настроена.
"""
import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone

import aiohttp
from aiogram import F, Router
from aiogram.types import Message

from common import (
    CATEGORIES,
    CATEGORY_TITLES,
    TIME_RE,
    event_reply_keyboard,
    fmt_date,
    fmt_range,
    get_zone,
    notify,
    valid_date,
)
import db

router = Router()

GROQ_URL = "https://api.groq.com/openai/v1"
STT_MODEL = os.getenv("GROQ_STT_MODEL", "whisper-large-v3")
LLM_MODEL = os.getenv("GROQ_LLM_MODEL", "openai/gpt-oss-20b")
VOICE_LANGUAGE = os.getenv("VOICE_LANGUAGE", "").strip()  # например "ru"; пусто = автоопределение
MAX_VOICE_SECONDS = 90
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=45)

WEEKDAYS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
RU_WEEKDAYS = {
    "понедельник": 0, "вторник": 1, "среда": 2, "среду": 2, "четверг": 3,
    "пятница": 4, "пятницу": 4, "суббота": 5, "субботу": 5, "воскресенье": 6,
}


def _rule_date(text: str, today: date) -> date | None:
    """Жёсткие правила для частых русских фраз: нейросеть на них иногда ошибается."""
    t = text.lower()
    if "послезавтра" in t:
        return today + timedelta(days=2)
    if "завтра" in t:
        return today + timedelta(days=1)
    if "через месяц" in t:
        return today + timedelta(days=30)
    if "через две недели" in t or "через 2 недели" in t:
        return today + timedelta(days=14)
    if "через неделю" in t:
        return today + timedelta(days=7)
    for name, num in RU_WEEKDAYS.items():
        if re.search(r"\b" + name + r"\b", t):
            ahead = num - today.weekday()
            if ahead <= 0:
                ahead += 7
            if "следующ" in t:
                ahead += 7
            return today + timedelta(days=ahead)
    if re.search(r"\bсегодня\b", t):
        return today
    return None


def _build_prompt(text: str, today: date) -> str:
    upcoming = "\n".join(
        f"{(today + timedelta(days=i)).isoformat()} {WEEKDAYS_EN[(today + timedelta(days=i)).weekday()]}"
        for i in range(0, 22)
    )
    return f"""Today is {today.isoformat()} ({WEEKDAYS_EN[today.weekday()]}).
Calendar for the next 3 weeks:
{upcoming}

The user said (any language): "{text}"

Task:
1. category: one of rest, sex, shop, todo, personal.
   shop = shopping list; todo = a shared chore/task; personal = a private note for the user only.
2. date: exact YYYY-MM-DD taken from the calendar above ("next Wednesday", "через 2 недели", "ensi keskiviikkona" ...).
   If no date is mentioned use today. If the user means "the nearest shopping list", use "nearest".
3. time: HH:MM (24h) if a time of day is mentioned, otherwise "".
4. title: a short title (max 60 chars) in the user's language.
5. items: for shop, an array of products; otherwise [].

Return STRICTLY a JSON object:
{{"category": "todo", "date": "{today.isoformat()}", "time": "", "title": "...", "items": []}}"""


async def _transcribe(session: aiohttp.ClientSession, key: str, audio: bytes) -> str:
    form = aiohttp.FormData()
    form.add_field("file", audio, filename="voice.ogg", content_type="audio/ogg")
    form.add_field("model", STT_MODEL)
    if VOICE_LANGUAGE:
        form.add_field("language", VOICE_LANGUAGE)
    async with session.post(
        f"{GROQ_URL}/audio/transcriptions", data=form, headers={"Authorization": f"Bearer {key}"}
    ) as resp:
        if resp.status != 200:
            logging.error("Groq STT error %s: %s", resp.status, (await resp.text())[:300])
            raise RuntimeError("stt")
        return str((await resp.json()).get("text", "")).strip()


async def _parse_intent(session: aiohttp.ClientSession, key: str, text: str, today: date) -> dict:
    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": _build_prompt(text, today)}],
        "response_format": {"type": "json_object"},
    }
    async with session.post(
        f"{GROQ_URL}/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    ) as resp:
        if resp.status != 200:
            logging.error("Groq LLM error %s: %s", resp.status, (await resp.text())[:300])
            raise RuntimeError("llm")
        data = await resp.json()
    ev = json.loads(data["choices"][0]["message"]["content"])
    if not isinstance(ev, dict):
        raise RuntimeError("llm-shape")
    return ev


def _sanitize(ev: dict, text: str, today: date) -> dict:
    """Приводит ответ нейросети к безопасному виду (валидация как в HTTP API)."""
    cat = str(ev.get("category") or "todo")
    if cat not in CATEGORIES:
        cat = "todo"

    raw_date = str(ev.get("date") or "").strip()
    nearest = raw_date.lower() in ("nearest", "ближайший")
    target = today
    if not nearest and valid_date(raw_date):
        target = date.fromisoformat(raw_date)
    rule = _rule_date(text, today)
    if rule is not None:
        target, nearest = rule, False
    if target < today:
        target = today

    time_ = str(ev.get("time") or "").strip()
    if not TIME_RE.match(time_):
        time_ = ""

    title = str(ev.get("title") or "").strip()[:100]
    if cat not in ("todo", "personal") or not title:
        title = CATEGORY_TITLES[cat]

    items = []
    raw_items = ev.get("items")
    if cat == "shop" and isinstance(raw_items, list):
        items = [str(x).strip()[:200] for x in raw_items if str(x).strip()][: db.MAX_ITEMS_PER_EVENT]

    return {"category": cat, "date": target.isoformat(), "nearest": nearest,
            "time": time_, "title": title, "items": items}


async def _add_to_shop_list(uid: int, ev: dict, today: date) -> tuple[dict | None, str]:
    """Ищет подходящий список покупок. Возвращает (событие, дата для нового списка)."""
    if ev["nearest"]:
        start = today.isoformat()
        end = (today + timedelta(days=31)).isoformat()
    else:
        start = ev["date"]
        end = (date.fromisoformat(ev["date"]) + timedelta(days=1)).isoformat()
    shops = [e for e in await db.list_events_range(uid, start, end) if e["category"] == "shop"]
    shops.sort(key=lambda e: (e["date"], e["id"]))
    target_date = ev["date"] if not ev["nearest"] else today.isoformat()
    return (shops[0] if shops else None), target_date


@router.message(F.voice)
async def handle_voice_message(message: Message):
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        await message.answer("Голосовые заметки не настроены (нет GROQ_API_KEY).")
        return
    if message.voice.duration and message.voice.duration > MAX_VOICE_SECONDS:
        await message.answer(f"Слишком длинное сообщение (максимум {MAX_VOICE_SECONDS} сек).")
        return

    await message.answer("🎤 Слушаю и распознаю…")
    uid = message.from_user.id
    try:
        user = await db.get_or_create_user(uid)
        tz = get_zone(user["tz"]) or timezone.utc
        today = datetime.now(timezone.utc).astimezone(tz).date()

        buf = await message.bot.download(message.voice)
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
            text = await _transcribe(session, key, buf.getvalue())
            if not text:
                await message.answer("Я ничего не расслышал.")
                return
            ev = _sanitize(await _parse_intent(session, key, text, today), text, today)

        cat = ev["category"]
        partner = user["partner_id"]
        if cat != "personal" and not partner:
            await message.answer("Сначала привяжите партнёра: /invite")
            return
        if await db.count_events_created(uid) >= db.MAX_EVENTS_PER_USER:
            await message.answer("Достигнут лимит событий.")
            return

        header = f"Вы сказали: «{text}»\n\n"

        if cat == "shop":
            if not ev["items"]:
                await message.answer(header + "Я не понял, какие продукты добавить.")
                return
            existing, target_date = await _add_to_shop_list(uid, ev, today)
            if existing:
                added = 0
                for item in ev["items"]:
                    if await db.add_item(existing["id"], item):
                        added += 1
                await message.answer(
                    header + f"✅ Добавил {added} из {len(ev['items'])} в список на {fmt_date(existing['date'])}."
                )
                return
            await db.create_event(
                created_by=uid, target_user=partner, title=ev["title"],
                description="Создано голосом", date=target_date, event_time="",
                category="shop", items=[{"text": t, "checked": False} for t in ev["items"]],
            )
            notify(partner, f"🛒 Партнёр добавил список покупок на {fmt_range(target_date, '')}. Откройте календарь, чтобы посмотреть.")
            await message.answer(header + f"✅ Создал список на {fmt_date(target_date)} ({len(ev['items'])} шт.).")
            return

        color = db.TODO_COLORS[6] if cat in ("todo", "personal") else ""
        if cat == "personal":
            target_user, visibility = uid, "private"  # личное по умолчанию видно только автору
        else:
            target_user, visibility = partner, "shared"

        event = await db.create_event(
            created_by=uid, target_user=target_user, title=ev["title"],
            description="Добавлено голосом", date=ev["date"], event_time=ev["time"],
            category=cat, color=color, visibility=visibility,
        )
        at = f" в {ev['time']}" if ev["time"] else ""
        if cat == "todo":
            notify(partner, f"📌 Новое дело на {fmt_range(ev['date'], '')}{at}. Откройте календарь, чтобы посмотреть.")
        elif cat in ("rest", "sex"):
            icon = "🌿" if cat == "rest" else "🔥"
            notify(partner, f"{icon} Новое событие на {fmt_range(ev['date'], '')}{at}. Ответьте прямо здесь или откройте календарь.",
                   reply_markup=event_reply_keyboard(event["id"]))
        await message.answer(
            header + f"✅ Добавлено в «{CATEGORY_TITLES[cat]}»:\n📅 {fmt_date(ev['date'])}{at}\n📝 {ev['title']}"
            + ("\n🔒 Видно только вам" if cat == "personal" else "")
        )
    except Exception:
        logging.exception("Ошибка при обработке голоса")
        await message.answer("Произошла ошибка при обработке голоса.")
