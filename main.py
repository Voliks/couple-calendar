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
        ideas_out.append({
            "idx": i,
            "text": text,
            "status": own,
            "effective": effective,
            "by": r.get("user_id"),
        })

    can_respond_ideas = (
        not is_creator
        and overall == "pending"
        and len(ideas_out) > 0
    )

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
            e.get("decline_comment_creator", "") if is_creator
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

    is_private_personal = data["category"] == "personal" and data["visibility"] == "private"
    if is_private_personal:
        target_user = tg_user.id
    elif data["category"] == "personal":
        target_user = user["partner_id"] or tg_user.id
    else:
        target_user = user["partner_id"]

    event = await db.create_event(
        tg_user.id, target_user, data["title"], data["description"],
        data["date"], data["time"], data["category"], data["items"],
        color=data["color"], remind_before=data["remind_before"],
        date_end=data["date_end"], visibility=data["visibility"],
    )

    if data["category"] == "shop":
        notify(
            user["partner_id"],
            f"🛒 Партнёр добавил список покупок на "
            f"{fmt_range(data['date'], data['date_end'])}. "
            "Откройте календарь, чтобы посмотреть.",
        )
    elif data["category"] == "personal":
        if data["visibility"] == "shared" and user["partner_id"]:
            at = f" в {data['time']}" if data["time"] else ""
            notify(
                user["partner_id"],
                f"📝 Партнёр добавил общее личное дело на "
                f"{fmt_range(data['date'], data['date_end'])}{at}. "
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
            f"{icon} Новое дело на {fmt_range(data['date'], data['date_end'])}{at}.{extra} "
            "Откройте календарь, чтобы посмотреть.",
        )
    else:
        icon = CATEGORY_ICONS.get(data["category"], "➕")
        notify(
            user["partner_id"],
            f"{icon} Новое событие на {fmt_range(data['date'], data['date_end'])}"
            f"{(' в ' + data['time']) if data['time'] else ''}. "
            "Ответьте прямо здесь или откройте календарь.",
            reply_markup=event_reply_keyboard(event["id"]),
        )
    return web.json_response(event_to_dict(event, tg_user.id), status=201)


async def api_update_event(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    user = await db.get_or_create_user(tg_user.id)
    event = await member_event(request, tg_user.id)

    if event["category"] == "personal" and event["created_by"] != tg_user.id:
        raise ApiError("Редактировать личное событие может только автор", 403)
    if event["category"] != "todo" and event["category"] != "personal" and event["created_by"] != tg_user.id:
        raise ApiError("Редактировать событие может только его автор", 403)

    data = parse_event_payload(await read_json(request))

    identity_changed = (
        data["date"], data["date_end"], data["time"], data["category"]
    ) != (event["date"], event.get("date_end") or "", event["time"], event["category"])
    visibility_changed = data["visibility"] != event.get("visibility", "shared")
    changed = identity_changed or visibility_changed \
              or data["description"] != event["description"] \
              or data["color"] != event.get("color", "") \
              or data["remind_before"] != (event.get("remind_before") or 0) \
              or data["title"] != event["title"]
    if not changed:
        return web.json_response(event_to_dict(event, tg_user.id))

    reset = identity_changed and data["category"] not in ("shop", "todo", "personal")
    updated = await db.update_event(
        event["id"], data["title"], data["description"], data["date"],
        data["time"], data["category"], reset_status=reset,
        color=data["color"], remind_before=data["remind_before"],
        date_end=data["date_end"], visibility=data["visibility"],
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
                    f"📝 Партнёр сделал личное дело общим: "
                    f"{fmt_range(data['date'], data['date_end'])}{title_part(data['title'])}.",
                )
            else:
                notify(
                    partner_id,
                    f"📝 Партнёр скрыл личное дело: "
                    f"{fmt_range(data['date'], data['date_end'])}{title_part(data['title'])}.",
                )

    if data["category"] == "shop":
        notify(
            event["target_user"],
            f"✏️ Список покупок на {fmt_range(data['date'], data['date_end'])} изменён.",
        )
    elif data["category"] == "todo":
        notify(
            event["target_user"],
            f"📌 Дело на {fmt_range(data['date'], data['date_end'])} изменено.",
        )
    elif data["category"] != "personal":
        suffix = " Оно снова ждёт вашего ответа." if identity_changed else ""
        notify(
            event["target_user"],
            f"✏️ Событие на {fmt_range(data['date'], data['date_end'])}{title_part(data['title'])} изменено.{suffix}",
        )

    updated = await db.get_event(event["id"])
    return web.json_response(event_to_dict(updated, tg_user.id))


async def api_delete_event(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    event = await member_event(request, tg_user.id)

    if event["category"] == "personal" and event["created_by"] != tg_user.id:
        raise ApiError("Удалять личное событие может только автор", 403)
    if event["category"] not in ("todo", "personal") and event["created_by"] != tg_user.id:
        raise ApiError("Удалить событие может только его автор. Вы можете отказаться от него.", 403)

    await db.delete_event(event["id"])
    return web.json_response({"status": "deleted"})


async def api_respond(request: web.Request) -> web.Response:
    tg_user = authenticate(request, write=True)
    event = await member_event(request, tg_user.id)
    body = await read_json(request)

    if event["category"] in ("shop", "todo", "personal"):
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
            body_text = (
                f"💬 {tg_user.first_name} оставил(а) комментарий к событию "
                f"на {fmt_range(event['date'], event.get('date_end', ''))}{at}{title_part(event['title'])}:\n\n{comment}"
            )
        else:
            body_text = (
                f"💬 {tg_user.first_name} удалил(а) свой комментарий к событию "
                f"на {fmt_range(event['date'], event.get('date_end', ''))}{at}{title_part(event['title'])}."
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
    out = [{
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

        if c["category"] in ("todo", "personal") and c["time"] and c.get("remind_before"):
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
            prefix = "📝" if c["category"] == "personal" else "📌"
            await safe_send(
                c["user_id"],
                f"{prefix} Напоминание: {c['title']} в {c['time']} "
                f"(через {_humanize_minutes(c['remind_before'])}).",
            )
            continue

        if local_now.strftime("%Y-%m-%d") != c["date"] or local_now.hour < REMINDER_HOUR:
            continue
        if not await db.mark_reminded(c["event_id"], c["user_id"]):
            continue
        at = f" в {c['time']}" if c["time"] else ""
        rng = fmt_range(c["date"], c.get("date_end", ""))
        if c["category"] == "todo":
            await safe_send(
                c["user_id"],
                f"📌 Сегодня дело{at}{title_part(c['title'])}. "
                "Откройте календарь, чтобы посмотреть.",
            )
        elif c["category"] == "personal":
            await safe_send(
                c["user_id"],
                f"📝 Сегодня личное дело{at}{title_part(c['title'])}. "
                "Откройте календарь, чтобы посмотреть.",
            )
        else:
            await safe_send(
                c["user_id"],
                f"⏰ Сегодня у вас запланировано событие{at}{title_part(c['title'])} "
                f"(на {rng}). Откройте календарь, чтобы посмотреть детали.",
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

    app = web.Application(
        middlewares=[api_middleware],
        client_max_size=20 * 1024 * 1024
    )
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