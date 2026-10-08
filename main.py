import asyncio
import logging
import os

from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import db

# Логирование
logging.basicConfig(level=logging.INFO)

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 8080))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# --- FSM Стейты ---
class Form(StatesGroup):
    waiting_for_invite = State()
    event_title = State()
    event_desc = State()
    event_date = State()
    event_category = State()
    edit_desc = State()
    edit_date = State()


# --- HTTP Health Check Сервер для Render ---
async def health_check(request):
    return web.Response(text="OK", status=200)


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"Health check сервер запущен на порту {PORT}")


# --- Вспомогательные функции ---
def get_main_keyboard():
    kb = [
        [types.KeyboardButton(text="➕ Создать событие"), types.KeyboardButton(text="📅 Мои события")],
        [types.KeyboardButton(text="🔗 Партнёр"), types.KeyboardButton(text="ℹ️ Помощь")],
    ]
    return types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


# --- Хэндлеры команд ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user = await db.get_or_create_user(message.from_user.id)
    text = (
        f"Привет, {message.from_user.first_name}!\n\n"
        f"Ваш код для подключения партнёра: `{user['invite_code']}`\n\n"
        "Отправьте этот код вашему партнёру или введите его код через кнопку '🔗 Партнёр'."
    )
    await message.answer(text, parse_mode="Markdown", reply_markup=get_main_keyboard())


@dp.message(F.text == "🔗 Партнёр")
async def partner_menu(message: types.Message, state: FSMContext):
    user = await db.get_or_create_user(message.from_user.id)
    if user["partner_id"]:
        await message.answer("Вы уже подключены к партнёру! ❤️")
    else:
        await state.set_state(Form.waiting_for_invite)
        await message.answer(
            f"Ваш код: `{user['invite_code']}`\n\n"
            "Введите пригласительный код вашего партнёра:",
            parse_mode="Markdown"
        )


@dp.message(Form.waiting_for_invite)
async def process_invite_code(message: types.Message, state: FSMContext):
    code = message.text.strip()
    partner = await db.get_user_by_code(code)

    if not partner:
        await message.answer("Неверный код. Попробуйте еще раз.")
        return

    if partner["telegram_id"] == message.from_user.id:
        await message.answer("Нельзя ввести свой собственный код!")
        return

    await db.link_partners(message.from_user.id, partner["telegram_id"])
    await state.clear()
    await message.answer("Ура! Партнёр успешно привязан 🎉", reply_markup=get_main_keyboard())

    try:
        await bot.send_message(partner["telegram_id"], "Ваш партнёр успешно подключился к вам! 🎉")
    except Exception as e:
        logging.error(f"Ошибка отправки уведомления партнёру: {e}")


# --- Создание событий ---
@dp.message(F.text == "➕ Создать событие")
async def start_create_event(message: types.Message, state: FSMContext):
    user = await db.get_or_create_user(message.from_user.id)
    if not user["partner_id"]:
        await message.answer("Сначала привяжите партнёра через меню '🔗 Партнёр'!")
        return

    kb = [
        [types.KeyboardButton(text="Свободное время"), types.KeyboardButton(text="Магазин")],
        [types.KeyboardButton(text="Важное событие")]
    ]
    await state.set_state(Form.event_category)
    await message.answer(
        "Выберите категорию события:",
        reply_markup=types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
    )


@dp.message(Form.event_category)
async def process_category(message: types.Message, state: FSMContext):
    category = message.text
    await state.update_data(category=category)
    await state.set_state(Form.event_title)

    if category == "Магазин":
        await message.answer(
            "Введите название списка покупок (или список товаров через запятую):",
            reply_markup=types.ReplyKeyboardRemove()
        )
    else:
        await message.answer("Введите название события:", reply_markup=types.ReplyKeyboardRemove())


@dp.message(Form.event_title)
async def process_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text)
    await state.set_state(Form.event_desc)
    await message.answer("Введите описание (или отправьте '-' чтобы пропустить):")


@dp.message(Form.event_desc)
async def process_desc(message: types.Message, state: FSMContext):
    desc = "" if message.text == "-" else message.text
    await state.update_data(description=desc)
    await state.set_state(Form.event_date)
    await message.answer("Введите дату (например: 2026-10-15 или 'Сегодня'):")


@dp.message(Form.event_date)
async def process_date(message: types.Message, state: FSMContext):
    data = await state.get_data()
    user = await db.get_or_create_user(message.from_user.id)

    # Парсим чек-лист, если категория "Магазин"
    items = []
    if data["category"] == "Магазин":
        items = [i.strip() for i in data["title"].split(",") if i.strip()]

    event = await db.create_event(
        created_by=message.from_user.id,
        target_user=user["partner_id"],
        title=data["title"],
        description=data["description"],
        category=data["category"],
        date=message.text,
        items=items
    )

    await state.clear()
    await message.answer("Событие успешно создано!", reply_markup=get_main_keyboard())

    # Уведомление партнёру
    try:
        if data["category"] == "Магазин":
            text = f"🛒 Новый список покупок от партнёра:\n<b>{data['title']}</b>\nДата: {message.text}"
        else:
            text = f"📅 Новое приглашение от партнёра:\n<b>{data['title']}</b>\nДата: {message.text}"

        kb = None
        if event["status"] == "pending":
            kb = types.InlineKeyboardMarkup(inline_keyboard=[
                [
                    types.InlineKeyboardButton(text="✅ Принять", callback_data=f"accept_{event['id']}"),
                    types.InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{event['id']}")
                ]
            ])

        await bot.send_message(user["partner_id"], text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logging.error(f"Ошибка уведомления партнера: {e}")


# --- Список событий ---
@dp.message(F.text == "📅 Мои события")
async def list_events_cmd(message: types.Message):
    events = await db.list_events(message.from_user.id)

    if not events:
        await message.answer("У вас пока нет активных событий.")
        return

    for e in events:
        status_emoji = {"pending": "⏳ Ожидает", "accepted": "✅ Подтверждено", "rejected": "❌ Отклонено"}.get(e["status"], "")
        text = f"<b>{e['title']}</b> ({e['category']})\n📅 Дата: {e['date']}\nСтатус: {status_emoji}\n"

        if e["description"]:
            text += f"Описание: {e['description']}\n"

        inline_buttons = []

        if e["category"] == "Магазин" and e["items"]:
            text += "\nСписок товаров:\n"
            for item in e["items"]:
                check = "✅" if item["is_completed"] else "🔲"
                text += f"{check} {item['title']}\n"
                inline_buttons.append([
                    types.InlineKeyboardButton(
                        text=f"{check} {item['title']}",
                        callback_data=f"toggle_{item['id']}"
                    )
                ])

        inline_buttons.append([
            types.InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete_{e['id']}")
        ])

        kb = types.InlineKeyboardMarkup(inline_keyboard=inline_buttons)
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


# --- Коллбэки ---
@dp.callback_query(F.data.startswith("accept_"))
async def accept_event(callback: types.CallbackQuery):
    event_id = int(callback.data.split("_")[1])
    await db.set_status(event_id, "accepted")
    await callback.message.edit_text(callback.message.text + "\n\n✅ **Принято!**", parse_mode="Markdown")


@dp.callback_query(F.data.startswith("reject_"))
async def reject_event(callback: types.CallbackQuery):
    event_id = int(callback.data.split("_")[1])
    await db.set_status(event_id, "rejected")
    await callback.message.edit_text(callback.message.text + "\n\n❌ **Отклонено!**", parse_mode="Markdown")


@dp.callback_query(F.data.startswith("toggle_"))
async def toggle_item(callback: types.CallbackQuery):
    item_id = int(callback.data.split("_")[1])
    await db.toggle_checklist_item(item_id)
    await callback.answer("Статус обновлен")


@dp.callback_query(F.data.startswith("delete_"))
async def delete_event_cb(callback: types.CallbackQuery):
    event_id = int(callback.data.split("_")[1])
    await db.delete_event(event_id)
    await callback.message.delete()
    await callback.answer("Событие удалено")


# --- Главная функция ---
async def main():
    logging.info("Инициализация базы данных...")
    await db.init_db()

    logging.info("Запуск веб-сервера...")
    await start_web_server()

    logging.info("Запуск бота...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())