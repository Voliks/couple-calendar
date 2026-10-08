# Календарь на двоих — Telegram Mini App

Бот (aiogram 3), HTTP API (aiohttp) и фронтенд (чистый HTML/JS/CSS) работают в одном процессе. База — SQLite.

## Структура

```
main.py            бот + API + раздача фронтенда
db.py              схема и запросы SQLite
webapp/index.html  Mini App
.env.example       пример настроек
requirements.txt
```

## Запуск

1. Создайте бота у @BotFather и получите токен.
2. Установите зависимости:
   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Скопируйте `.env.example` в `.env` и заполните `BOT_TOKEN` и `WEBAPP_URL`.
4. Telegram открывает Mini App только по HTTPS. Для локальной разработки пробросьте порт:
   ```bash
   cloudflared tunnel --url http://localhost:8080   # или: ngrok http 8080
   ```
   Полученный https-адрес пропишите в `WEBAPP_URL`.
5. Запустите:
   ```bash
   python main.py
   ```
6. Откройте бота, нажмите /start и «📅 Открыть Календарь».

## Как связать пару

Если партнёр не привязан, Mini App показывает экран с кнопкой «Отправить ссылку партнёру». Ссылка имеет вид `https://t.me/<bot>?start=ref_<КОД>`. Эту же ссылку выдаёт команда `/invite`. После перехода по ней второй человек связывается с первым, и оба получают доступ к календарю.

## Безопасность

Фронтенд отправляет на сервер `Telegram.WebApp.initData`, а сервер проверяет его HMAC-подпись токеном бота (`safe_parse_webapp_init_data`) и не принимает «голый» `initDataUnsafe.user.id`, который легко подделать. Сервер также проверяет, что на событие отвечает только адресат и только пока статус `pending`.
