function checklistHtml(e) {
  const items = (e.checklist || []).map((it) => `
    <div class="checklist-item${it.checked ? " checked" : ""}" data-item="${it.id}">
      <span class="cl-drag" data-cl-drag role="button" tabindex="0" aria-label="Перетащить">⋮⋮</span>
      <input type="checkbox" ${it.checked ? "checked" : ""} data-cl-toggle aria-label="${esc(it.text)}">
      <span class="cl-text">${esc(it.text)}</span>
      <button type="button" class="cl-remove" data-cl-remove aria-label="Удалить пункт «${esc(it.text)}»">×</button>
    </div>`).join("");
  return `<div class="checklist">${items || '<div class="cl-empty">Список пока пуст</div>'}</div>
    <div class="cl-add-row">
      <input type="text" class="cl-input" maxlength="200" placeholder="Добавить пункт..." aria-label="Новый пункт списка">
      <button type="button" data-cl-add aria-label="Добавить пункт">+</button>
    </div>`;
}

function answerHtml(e, expired) {
  if (e.category === "shop" || e.category === "todo" || e.category === "personal") return "";
  if (e.is_creator) return "";
  if (e.status === "pending") {
    return expired ? "" : `<div class="answer">
      <button class="yes" data-status="accepted">Да</button>
      <button class="no" data-status="declined">Нет</button></div>`;
  }
  if (e.status === "accepted") {
    return `<div class="answer one"><button class="soft" data-status="declined">Отказаться</button></div>`;
  }
  return `<div class="answer one"><button class="soft" data-status="accepted">Всё-таки принять</button></div>`;
}

function reactionHtml(e) {
  if (e.category === "shop" || e.category === "todo" || e.category === "personal") return "";
  if (e.status !== "accepted" && e.status !== "declined") return "";
  const mine = e.reaction_mine || "";
  const partner = e.reaction_partner || "";
  const parts = [];
  if (mine) {
    parts.push(`<span class="reaction-badge mine" data-reaction-toggle title="Ваша реакция">${esc(mine)}</span>`);
  }
  if (partner) {
    parts.push(`<span class="reaction-badge" title="Реакция партнёра">${esc(partner)}</span>`);
  }
  if (e.status !== "accepted") return parts.join("");
  parts.push(`<button type="button" class="reaction-add" data-reaction-open aria-label="Поставить реакцию">😊+</button>`);
  return `<div class="reaction-row">${parts.join("")}</div>`;
}

function ideasHtml(e) {
  if (e.category !== "sex") return "";
  if (!e.ideas || !e.ideas.length) return "";
  const rows = e.ideas.map(it => {
    const eff = it.effective || "";
    const badge = eff === "accepted" ? "✅" : eff === "declined" ? "❌" : "⏳";
    const canPress = e.can_respond_ideas && !it.status;
    const buttons = canPress
      ? `<button type="button" class="idea-yes" data-idea-yes="${it.idx}" aria-label="Да">Да</button>
         <button type="button" class="idea-no"  data-idea-no="${it.idx}"  aria-label="Нет">Нет</button>`
      : "";
    return `<div class="idea-line" data-idea-idx="${it.idx}">
      <span class="idea-badge">${badge}</span>
      <span class="idea-text">${esc(it.text)}</span>
      <span class="idea-actions">${buttons}</span>
    </div>`;
  }).join("");
  return `<div class="ideas-list-card">${rows}</div>`;
}

function declineCommentHtml(e) {
  if (e.category === "shop" || e.category === "todo" || e.category === "personal") return "";
  if (e.status !== "declined") return "";

  const cCreator = e.decline_comment_creator || "";
  const cTarget  = e.decline_comment_target || "";
  const mine = e.my_decline_comment || "";

  const lines = [];
  if (cCreator) lines.push(`<p class="decline-comment">💬 Автор: ${esc(cCreator)}</p>`);
  if (cTarget)  lines.push(`<p class="decline-comment">💬 Партнёр: ${esc(cTarget)}</p>`);

  const button = `<button type="button" class="comment-add" data-comment-open>${
    mine ? "✏️ Изменить свой комментарий" : "💬 Оставить комментарий"
  }</button>`;

  return `${lines.join("")}${button}`;
}

// ---------- Кэш подписанных ссылок на фото (живут 60 мин на сервере, держим 50) ----------
const PHOTO_TTL = 50 * 60 * 1000;
const photoUrlCache = new Map();   // path -> { url, exp }
const photoInflight = new Map();   // path -> Promise<url|null>

function cachedPhotoUrl(path) {
  const hit = photoUrlCache.get(path);
  if (hit && hit.exp > Date.now()) return hit.url;
  if (hit) photoUrlCache.delete(path);
  return "";
}

function fetchPhotoUrl(path) {
  const cached = cachedPhotoUrl(path);
  if (cached) return Promise.resolve(cached);
  let p = photoInflight.get(path);
  if (!p) {
    p = api(`/comments/photo-url?path=${encodeURIComponent(path)}`)
      .then((res) => {
        if (res.view_url) photoUrlCache.set(path, { url: res.view_url, exp: Date.now() + PHOTO_TTL });
        return res.view_url || null;
      })
      .catch(() => null)
      .finally(() => photoInflight.delete(path));
    photoInflight.set(path, p);
  }
  return p;
}

// Отображение комментариев с поддержкой фото, кнопки удаления и «отправляется…»
function commentsListHtml(comments) {
  if (!comments.length) return `<div class="cm-empty">Нет комментариев</div>`;
  const myId = state?.my_id || 0;
  return comments.map((c) => {
    const pending = c.pending === true;
    let imgHtml = "";
    if (c.photo_id) {
      const known = cachedPhotoUrl(c.photo_id);
      imgHtml = `<div style="margin-top:6px;">
        <img data-src="${esc(c.photo_id)}" class="comment-img" alt="Фото" loading="lazy" decoding="async"${known ? ` src="${esc(known)}"` : ""}>
      </div>`;
    } else if (pending && c.uploading) {
      imgHtml = `<div class="cm-empty" style="margin-top:4px;">📷 Загружается…</div>`;
    }
    const isMine = c.sender_id === myId;
    const delBtn = isMine && !pending
      ? `<button type="button" class="cm-del" data-cm-del="${c.id}" aria-label="Удалить комментарий">×</button>`
      : "";
    return `<div class="cm${isMine ? " mine" : ""}" data-cm-id="${c.id}"${pending ? ' style="opacity:.6"' : ""}>
      <div class="cm-text">${c.text ? esc(c.text) : ""}</div>
      ${imgHtml}
      ${delBtn}
    </div>`;
  }).join("");
}

function eventCommentsHtml(e) {
  if (e.category === "shop") return "";
  
  return `
    <div class="event-comments-block" data-event-id="${e.id}">
      <div class="event-comments-list" id="comments-list-${e.id}">${commentsListHtml(e.comments || [])}</div>
      <div class="comment-row">
        <input type="text" class="comment-input" placeholder="Написать комментарий..." maxlength="500">
        <button class="btn-attach-comment" style="padding:0 12px; font-size:18px; background:var(--bg2); border-radius:8px; border:none;">📷</button>
        <button class="btn-send-comment">➤</button>
      </div>
    </div>
  `;
}

function cardHtml(e) {
  const cat = CATEGORIES[e.category] || CATEGORIES.rest;
  const sk = statusKey(e);
  const isShop = e.category === "shop";
  const isTodo = e.category === "todo";
  const isPersonal = e.category === "personal";
  const isRange = (e.date_end && e.date_end !== e.date);
  const remindText = ((isTodo || isPersonal) && e.remind_before && e.time)
    ? ` · 🔔 за ${humanizeMinutes(e.remind_before)}`
    : "";
  const dateLabel = fmtRange(e.date, e.date_end);
  const tag = `${cat.icon} ${cat.label} · ${dateLabel}${e.time ? " · " + esc(e.time) : ""}${remindText}`;

  const cardCls = isPersonal ? "personal" : (isTodo ? "todo" : (isShop ? "accepted" : sk));
  const hasColor = (isTodo || isPersonal || (e.category === "rest" && isRange)) && e.color;
  const cardStyle = hasColor ? ` style="--c: ${hexToRgb(e.color)};"` : "";

  const canEdit = e.can_edit !== undefined ? e.can_edit : e.is_creator;
  const actions = (canEdit)
    ? `<div class="card-actions">
         <button class="icon-btn edit-btn" aria-label="Редактировать" title="Редактировать">✏️</button>
         <button class="icon-btn delete-btn" aria-label="Удалить" title="Удалить">🗑️</button>
       </div>` : "";

  const statusLabel = (isShop || isTodo || isPersonal) ? "" : `<b>${STATUS_TEXT[sk]}</b>`;

  let privacy = "";
  if (isPersonal) {
    privacy = e.visibility === "private"
      ? `<div class="tag" style="margin-top:2px;color:rgb(var(--personal));font-weight:600;">🔒 Только для вас</div>`
      : `<div class="tag" style="margin-top:2px;color:rgb(var(--personal));font-weight:600;">🌐 Общее с партнёром</div>`;
  }

  let altTimeHtml = "";
  if (e.status === "declined" && e.alt_date) {
    altTimeHtml = `<div style="margin-top:8px; padding:8px 10px; border-radius:8px; background:rgba(36,129,204,0.1); font-size:13px; color:var(--accent); font-weight:600;">
      📅 Предложено другое время: ${fmtRange(e.alt_date, "")} ${e.alt_time ? "в " + esc(e.alt_time) : ""}
    </div>`;
  }

  return `<article class="card ${cardCls}" data-id="${e.id}"${cardStyle}>
    <div class="card-header">
      <div style="flex:1;min-width:0"><h3>${esc(e.title)}</h3><div class="tag">${tag}</div>${privacy}</div>
      ${actions}
    </div>
    ${isShop ? checklistHtml(e) : (e.description ? `<p>${esc(e.description)}</p>` : "")}
    ${isShop && e.description ? `<p>${esc(e.description)}</p>` : ""}
    ${ideasHtml(e)}
    ${reactionHtml(e)}
    ${declineCommentHtml(e)}
    ${altTimeHtml}
    ${eventCommentsHtml(e)}
    <div class="meta"><span>${e.is_creator ? "От вас" : "От партнёра"}</span>${statusLabel}</div>
    ${answerHtml(e, sk === "expired")}
  </article>`;
}

const sigs = new WeakMap();
function reconcile(container, items) {
  if (!items.length) { container.replaceChildren(); return false; }
  [...container.children].forEach((c) => { if (!c.dataset.id) c.remove(); });
  const existing = new Map([...container.children].map((c) => [c.dataset.id, c]));
  let prev = null;
  for (const it of items) {
    let el = existing.get(it.id);
    if (!el || sigs.get(el) !== it.html) {
      const tpl = document.createElement("template");
      tpl.innerHTML = it.html.trim();
      const fresh = tpl.content.firstElementChild;
      sigs.set(fresh, it.html);
      if (el) {
        const oldInput = el.querySelector(".cl-input, .comment-input");
        const typed = oldInput ? oldInput.value : "";
        const hadFocus = oldInput && oldInput === document.activeElement;
        const sel = hadFocus ? [oldInput.selectionStart, oldInput.selectionEnd] : null;
        el.replaceWith(fresh);
        const newInput = fresh.querySelector(".cl-input, .comment-input");
        if (newInput && typed) newInput.value = typed;
        if (newInput && hadFocus) {
          newInput.focus({ preventScroll: true });
          try { newInput.setSelectionRange(sel[0], sel[1]); } catch (e) {}
        }
      }
      el = fresh;
    }
    existing.delete(it.id);
    const expected = prev ? prev.nextElementSibling : container.firstElementChild;
    if (el !== expected) container.insertBefore(el, expected);
    prev = el;
  }
  existing.forEach((el) => el.remove());
  return true;
}

// Точечная перерисовка ТОЛЬКО списка комментариев: поле ввода, фокус и клавиатура не трогаются
function paintComments(eventId) {
  const ev = findEvent(eventId);
  if (!ev) return;
  const card = document.querySelector(`#list .card[data-id="${eventId}"]`);
  const list = card?.querySelector(".event-comments-list");
  if (!list) { renderList(); return; }
  list.innerHTML = commentsListHtml(ev.comments || []);
  sigs.set(card, cardHtml(ev));      // подпись карточки синхронна с DOM, лишней пересборки не будет
  loadCommentImages(list);
}

// Полная перезагрузка комментариев одного события (нужна редко — например, после ошибки)
async function loadEventComments(eventId) {
  try {
    const res = await api(`/events/${eventId}/comments`);
    const ev = findEvent(eventId);
    if (ev) ev.comments = res.comments || [];
    paintComments(eventId);
  } catch (e) {
    console.error("Ошибка загрузки комментариев", e);
  }
}

let tmpCommentSeq = 0;

// Отправка: комментарий появляется мгновенно, запрос уходит в фоне
function sendEventComment(eventId, inputEl, photo = null) {
  const text = inputEl.value.trim();
  if (!text && !photo) return;
  const ev = findEvent(eventId);
  if (!ev) return;
  inputEl.value = "";                       // поле НЕ блокируем и фокус не теряем — клавиатура остаётся

  const tmpId = `t${++tmpCommentSeq}`;
  const temp = {
    id: tmpId, sender_id: state.my_id, text, photo_id: "", created_at: Math.floor(Date.now() / 1000),
    pending: true, uploading: !!photo,
  };
  ev.comments = [...(ev.comments || []), temp];
  paintComments(eventId);
  tg.HapticFeedback?.impactOccurred?.("light");

  return queueOp(async () => {
    try {
      let photo_id = "";
      if (photo) photo_id = await uploadCommentPhoto(photo);
      const res = await api(`/events/${eventId}/comments`, { method: "POST", body: { text, photo_id }, adopt: true });
      const cur = findEvent(eventId);
      if (cur) {
        const arr = cur.comments || [];
        const next = arr.some((c) => c.id === tmpId)
          ? arr.map((c) => (c.id === tmpId ? res.comment : c))
          : [...arr, res.comment];            // state успели перезагрузить — временного уже нет
        const seen = new Set();
        cur.comments = next.filter((c) => !seen.has(c.id) && seen.add(c.id));   // без дублей
      }
      paintComments(eventId);
    } catch (e) {
      const cur = findEvent(eventId);
      if (cur) cur.comments = (cur.comments || []).filter((c) => c.id !== tmpId);
      paintComments(eventId);
      if (text && !inputEl.value) inputEl.value = text;   // вернуть набранный текст
      toast(e.message);
    }
  });
}

// Удаление: исчезает сразу, при ошибке возвращается на место
function deleteEventComment(eventId, cmId) {
  const ev = findEvent(eventId);
  if (!ev) return;
  const list = ev.comments || [];
  const idx = list.findIndex((c) => String(c.id) === String(cmId));
  if (idx < 0) return;
  const removed = list[idx];
  ev.comments = list.filter((c) => c !== removed);
  paintComments(eventId);

  return queueOp(async () => {
    try {
      await api(`/events/${eventId}/comments/${cmId}`, { method: "DELETE", adopt: true });
      tg.HapticFeedback?.notificationOccurred("success");
    } catch (e) {
      const cur = findEvent(eventId);
      if (e.status === 404) {
        toast("Комментарий уже удалён");      // уже нет на сервере — возвращать не нужно
      } else if (cur) {
        cur.comments = [...(cur.comments || []), removed].sort((x, y) => (x.created_at - y.created_at) || (Number(x.id) - Number(y.id)));
        paintComments(eventId);
        toast(e.message);
      }
    }
  });
}

function renderList() {
  const prevScrollY = window.scrollY;
  $("listTitle").textContent = "События · " + DAY_FORMAT.format(parse(selected));
  
  const dayEvents = state.events.filter((e) => dayHasDate(e, selected));

  const weight = (status) => {
    if (status === "declined" || status === "expired") return 2;
    if (status === "pending") return 0;
    return 1;
  };

  dayEvents.sort((a, b) => {
    const wA = weight(statusKey(a));
    const wB = weight(statusKey(b));
    if (wA !== wB) return wA - wB;
    return b.id - a.id;
  });

  const items = dayEvents.map((e) => ({ id: String(e.id), html: cardHtml(e) }));

  const ok = reconcile($("list"), items);
  if (!ok) {
    $("list").innerHTML = `<div class="empty">На этот день ничего нет. Нажмите «+», чтобы добавить событие.</div>`;
  }
  
  if (prevScrollY > 0) {
    requestAnimationFrame(() => {
      const max = document.documentElement.scrollHeight - window.innerHeight;
      window.scrollTo(0, Math.min(prevScrollY, Math.max(0, max)));
    });
  }

  if (typeof loadCommentImages === "function") loadCommentImages();
}

function replaceEvent(updated) {
  const i = state.events.findIndex((e) => e.id === updated.id);
  if (i >= 0) state.events[i] = updated; else state.events.push(updated);
  monthCache[monthKey()] = state.events;
  renderCalendar();
  renderList();
}

let pendingOps = 0;
let opChain = Promise.resolve();
function queueOp(fn) {
  pendingOps++;
  const run = opChain.then(fn);
  opChain = run.catch(() => {}).then(() => { pendingOps--; });
  return run;
}
const lastOp = () => pendingOps <= 1;

function findEvent(id) { return state.events.find((e) => String(e.id) === String(id)); }

function toggleItem(eventId, itemId, checked) {
  const ev = findEvent(eventId);
  const it = ev?.checklist.find((x) => String(x.id) === String(itemId));
  if (!it) return;
  it.checked = checked;
  renderList();
  queueOp(async () => {
    try {
      const updated = await api(`/events/${eventId}/items/${itemId}`, { method: "PUT", body: { checked }, adopt: true });
      tg.HapticFeedback?.selectionChanged();
      if (lastOp()) replaceEvent(updated);
    } catch (e) { toast(e.message); await load(); }
  });
}

function removeItem(eventId, itemId) {
  const ev = findEvent(eventId);
  if (!ev) return;
  ev.checklist = ev.checklist.filter((x) => String(x.id) !== String(itemId));
  renderList();
  queueOp(async () => {
    try {
      const updated = await api(`/events/${eventId}/items/${itemId}`, { method: "DELETE", adopt: true });
      if (lastOp()) replaceEvent(updated);
    } catch (e) { toast(e.message); await load(); }
  });
}

function addItem(eventId, input) {
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  queueOp(async () => {
    try {
      const updated = await api(`/events/${eventId}/items`, { method: "POST", body: { text }, adopt: true });
      tg.HapticFeedback?.selectionChanged();
      if (lastOp()) replaceEvent(updated);
    } catch (e) {
      toast(e.message);
      const again = [...document.querySelectorAll(".cl-input")].find((i) => i.closest(".card")?.dataset.id === String(eventId));
      if (again && !again.value) again.value = text;
    }
  });
}

function moveItem(eventId, itemId, newPosition) {
  const ev = findEvent(eventId);
  if (!ev) return;
  const idx = ev.checklist.findIndex(x => String(x.id) === String(itemId));
  if (idx < 0) return;
  newPosition = Math.max(0, Math.min(newPosition, ev.checklist.length - 1));
  if (idx === newPosition) return;
  const [moved] = ev.checklist.splice(idx, 1);
  ev.checklist.splice(newPosition, 0, moved);
  renderList();
  tg.HapticFeedback?.selectionChanged();
  queueOp(async () => {
    try {
      const updated = await api(
        `/events/${eventId}/items/${itemId}/move`,
        { method: "POST", body: { position: newPosition }, adopt: true }
      );
      if (lastOp()) replaceEvent(updated);
    } catch (e) { toast(e.message); await load(); }
  });
}

let dragState = null;

function onDragStart(ev) {
  const handle = ev.target.closest("[data-cl-drag]");
  if (!handle) return;
  const item = handle.closest(".checklist-item");
  const card = handle.closest(".card");
  if (!item || !card) return;

  ev.preventDefault();
  const rect = item.getBoundingClientRect();
  dragState = {
    eventId: card.dataset.id,
    itemId: item.dataset.item,
    el: item,
    pointerOffsetY: ev.clientY - rect.top,
    pointerId: ev.pointerId,
  };
  item.classList.add("dragging");
  try { handle.setPointerCapture(ev.pointerId); } catch (e) {}
}

function onDragMove(ev) {
  if (!dragState) return;
  ev.preventDefault();
  const list = dragState.el.parentElement;
  const items = [...list.querySelectorAll(".checklist-item")].filter(x => x !== dragState.el);
  let closest = null;
  let closestDist = Infinity;
  for (const el of items) {
    const rect = el.getBoundingClientRect();
    const mid = rect.top + rect.height / 2;
    const dist = Math.abs(ev.clientY - mid);
    if (dist < closestDist) { closestDist = dist; closest = el; }
  }
  if (!closest) return;
  const rect = closest.getBoundingClientRect();
  const after = ev.clientY > rect.top + rect.height / 2;
  if (after) {
    list.insertBefore(dragState.el, closest.nextSibling);
  } else {
    list.insertBefore(dragState.el, closest);
  }
}

function onDragEnd() {
  if (!dragState) return;
  const { eventId, itemId, el } = dragState;
  el.classList.remove("dragging");
  const list = el.parentElement;
  const items = [...list.querySelectorAll(".checklist-item")];
  const newPos = items.indexOf(el);
  const idsNow = items.map(x => String(x.dataset.item));
  dragState = null;

  const ev = findEvent(eventId);
  if (!ev) { renderList(); return; }
  const oldOrder = ev.checklist.map(x => String(x.id));
  if (idsNow.join(",") === oldOrder.join(",")) {
    return;
  }
  moveItem(eventId, itemId, newPos);
}

$("list").addEventListener("pointerdown", onDragStart);
$("list").addEventListener("pointermove", onDragMove);
$("list").addEventListener("pointerup", onDragEnd);
$("list").addEventListener("pointercancel", () => {
  if (!dragState) return;
  dragState.el.classList.remove("dragging");
  dragState = null;
  renderList();
});

let reactionPickerEl = null;
function openReactionPicker(event) {
  closeReactionPicker();
  const list = (state.allowed_reactions && state.allowed_reactions.length)
    ? state.allowed_reactions : ["❤️","🔥","😍","😂","👍","🎉","😮","🥰"];
  const el = document.createElement("div");
  el.className = "reaction-picker";
  el.innerHTML = list.map(r => `<button type="button" data-r="${r}">${r}</button>`).join("")
                + `<button type="button" data-r="" title="Убрать">✖️</button>`;
  document.body.appendChild(el);
  reactionPickerEl = el;
  el.addEventListener("click", async (e) => {
    e.stopPropagation();
    const b = e.target.closest("[data-r]");
    if (!b) return;
    const emoji = b.dataset.r;
    closeReactionPicker();
    try {
      const updated = await api(`/events/${event.id}/reaction`, { method: "POST", body: { emoji }, adopt: true });
      replaceEvent(updated);
      tg.HapticFeedback?.notificationOccurred("success");
    } catch (err) { toast(err.message); }
  });
  setTimeout(() => {
    document.addEventListener("pointerdown", outsideCloseReaction);
  }, 0);
}
function outsideCloseReaction(e) {
  if (reactionPickerEl && !reactionPickerEl.contains(e.target)) closeReactionPicker();
}
function closeReactionPicker() {
  if (reactionPickerEl) { reactionPickerEl.remove(); reactionPickerEl = null; }
  document.removeEventListener("pointerdown", outsideCloseReaction);
}

/* ---------- ЕДИНОЕ ОКНО ОТКАЗА / ПЕРЕНОСА ---------- */

function openDeclineDialog(event, onConfirm) {
  const overlay = $("declineOverlay");
  const commentEl = $("declineComment");
  const offerNewEl = $("declineOfferNew");
  const newDateBlock = $("declineNewDateBlock");
  const newDateEl = $("declineNewDate");
  const newTimeEl = $("declineNewTime");
  const errorEl = $("declineError");
  const confirmBtn = $("declineConfirm");

  // Начальные значения
  commentEl.value = event.my_decline_comment || "";
  offerNewEl.checked = false;
  newDateBlock.hidden = true;
  newDateEl.value = event.date;
  newTimeEl.value = event.time || "";
  errorEl.textContent = "";
  confirmBtn.textContent = "Отклонить";

  overlay.hidden = false;
  tg.BackButton?.show();

  const close = () => {
    overlay.hidden = true;
    tg.BackButton?.hide();
    // Сбрасываем обработчики, чтобы не висели
    offerNewEl.onchange = null;
    $("declineCancel").onclick = null;
    $("declineConfirm").onclick = null;
    overlay.onclick = null;
  };

  offerNewEl.onchange = () => {
    newDateBlock.hidden = !offerNewEl.checked;
    confirmBtn.textContent = offerNewEl.checked ? "Отклонить и перенести" : "Отклонить";
    if (offerNewEl.checked) {
      if (!newDateEl.value) {
        const d = parse(event.date);
        d.setDate(d.getDate() + 1);
        newDateEl.value = fmt(d.getFullYear(), d.getMonth(), d.getDate());
      }
      if (!newTimeEl.value) newTimeEl.value = event.time || "";
    }
  };

  $("declineCancel").onclick = close;
  overlay.onclick = (e) => { if (e.target === overlay) close(); };

  $("declineConfirm").onclick = async () => {
    const comment = commentEl.value.trim();
    let altDate = "";
    let altTime = "";

    if (offerNewEl.checked) {
      altDate = newDateEl.value;
      altTime = newTimeEl.value;
      if (!altDate) {
        errorEl.textContent = "Выберите новую дату";
        return;
      }
      if (altDate === event.date && (altTime || "") === (event.time || "")) {
        errorEl.textContent = "Новая дата и время совпадают с текущими";
        return;
      }
    }

    confirmBtn.disabled = true;
    const originalText = confirmBtn.textContent;
    confirmBtn.textContent = "Отправка...";

    try {
      await onConfirm(comment, altDate, altTime);
      close();
    } catch (err) {
      errorEl.textContent = err.message || "Ошибка";
    } finally {
      confirmBtn.disabled = false;
      confirmBtn.textContent = originalText;
    }
  };

  setTimeout(() => commentEl.focus(), 50);
}

/* ---------- ОБРАБОТЧИК КЛИКОВ ---------- */

$("list").addEventListener("click", async (ev) => {
  // Удаление комментария
  const delBtn = ev.target.closest("[data-cm-del]");
  if (delBtn) {
    ev.preventDefault();
    ev.stopPropagation();
    const cmId = delBtn.dataset.cmDel;
    const block = delBtn.closest(".event-comments-block");
    const eventId = block?.dataset.eventId;
    if (!eventId) return;
    confirmDialog("Удалить этот комментарий?", () => deleteEventComment(eventId, cmId));
    return;
  }

  const card = ev.target.closest(".card");
  if (!card) return;
  const eventId = card.dataset.id;
  const event = findEvent(eventId);
  if (!event) return;

  const sendCommentBtn = ev.target.closest(".btn-send-comment");
  if (sendCommentBtn) {
    const block = sendCommentBtn.closest(".event-comments-block");
    const input = block.querySelector(".comment-input");
    sendEventComment(block.dataset.eventId, input);
    return;
  }

  const ideaYes = ev.target.closest("[data-idea-yes]");
  const ideaNo  = ev.target.closest("[data-idea-no]");
  if (ideaYes || ideaNo) {
    const btn = ideaYes || ideaNo;
    const idx = Number(btn.dataset.ideaYes ?? btn.dataset.ideaNo);
    const status = ideaYes ? "accepted" : "declined";
    const line = ev.target.closest(".idea-line");
    if (line) line.querySelectorAll("button").forEach(b => (b.disabled = true));
    try {
      const updated = await api(`/events/${eventId}/idea`, { method: "POST", body: { idx, status } });
      replaceEvent(updated);
      etag = null;
      tg.HapticFeedback?.notificationOccurred("success");
    } catch (err) {
      toast(err.message);
      await load(true);
    }
    return;
  }

  if (ev.target.closest("[data-reaction-open]") || ev.target.closest("[data-reaction-toggle]")) {
    if (event.category === "shop" || event.category === "todo" || event.category === "personal") return;
    if (event.status !== "accepted") return;
    openReactionPicker(event);
    return;
  }

  if (ev.target.closest("[data-comment-open]")) {
    if (event.category === "shop" || event.category === "todo" || event.category === "personal") return;
    if (event.status !== "declined") return;
    openDeclineDialog(event, async (comment, altDate, altTime) => {
      const updated = await api(`/events/${eventId}/comment`, {
        method: "POST",
        body: { comment }
      });
      replaceEvent(updated);
      tg.HapticFeedback?.notificationOccurred("success");
    });
    return;
  }

  const toggle = ev.target.closest("[data-cl-toggle]");
  if (toggle) { toggleItem(eventId, toggle.closest("[data-item]").dataset.item, toggle.checked); return; }

  const remove = ev.target.closest("[data-cl-remove]");
  if (remove) { removeItem(eventId, remove.closest("[data-item]").dataset.item); return; }

  if (ev.target.closest("[data-cl-add]")) { addItem(eventId, card.querySelector(".cl-input")); return; }

  const statusBtn = ev.target.closest("[data-status]");
  if (statusBtn) {
    const newStatus = statusBtn.dataset.status;

    // --- ОТКАЗ (с опциональным переносом) ---
    if (newStatus === "declined") {
      openDeclineDialog(event, async (comment, altDate, altTime) => {
        const updated = await api(`/events/${eventId}/respond`, {
          method: "POST",
          body: {
            status: "declined",
            comment,
            alt_date: altDate || "",
            alt_time: altTime || ""
          }
        });
        replaceEvent(updated);
        etag = null; etagKey = "";
        await load(true);
        tg.HapticFeedback?.notificationOccurred("success");
        toast(altDate ? "Отклонено с предложением переноса" : "Отклонено");
      });
      return;
    }

    // --- ПРИНЯТИЕ ---
    statusBtn.parentElement.querySelectorAll("button").forEach((b) => (b.disabled = true));
    try {
      const updated = await api(`/events/${eventId}/respond`, { method: "POST", body: { status: newStatus } });
      replaceEvent(updated);
      etag = null; etagKey = "";
      await load(true);
      tg.HapticFeedback?.notificationOccurred("success");
    } catch (e) {
      toast(e.message);
      await load(true);
    }
    return;
  }

  if (ev.target.closest(".edit-btn")) { openForm(event); return; }

  if (ev.target.closest(".delete-btn")) {
    confirmDialog("Удалить это событие?", async () => {
      try {
        await api(`/events/${eventId}`, { method: "DELETE" });
        tg.HapticFeedback?.notificationOccurred("success");
        toast("Удалено");
        etag = null;
        await load();
      } catch (e) { toast(e.message); }
    });
  }
});

$("list").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && ev.target.classList?.contains("cl-input")) {
    ev.preventDefault();
    addItem(ev.target.closest(".card").dataset.id, ev.target);
  }
  if (ev.key === "Enter" && !ev.isComposing && ev.target.classList?.contains("comment-input")) {
    ev.preventDefault();
    const block = ev.target.closest(".event-comments-block");
    sendEventComment(block.dataset.eventId, ev.target);
  }
});

/* ---------- ЗАГРУЗКА ФОТО В КОММЕНТАРИИ ---------- */

// Сжимаем фото до 1600px / JPEG 0.82 прямо в браузере: вместо 4–8 МБ уходит ~200–400 КБ
async function compressImage(file, maxSide = 1600, quality = 0.82) {
  if (!file.type.startsWith("image/") || file.type === "image/gif" || file.size < 300 * 1024) return file;
  try {
    const bmp = await createImageBitmap(file, { imageOrientation: "from-image" });
    const scale = Math.min(1, maxSide / Math.max(bmp.width, bmp.height));
    const w = Math.max(1, Math.round(bmp.width * scale));
    const h = Math.max(1, Math.round(bmp.height * scale));
    const canvas = document.createElement("canvas");
    canvas.width = w; canvas.height = h;
    canvas.getContext("2d").drawImage(bmp, 0, 0, w, h);
    bmp.close?.();
    const blob = await new Promise((res) => canvas.toBlob(res, "image/jpeg", quality));
    return blob && blob.size < file.size ? blob : file;
  } catch (e) {
    return file;   // например HEIC без поддержки в браузере — отправим как есть
  }
}

// Сжатие и получение upload-url идут параллельно; возвращает path в хранилище
async function uploadCommentPhoto(file) {
  const [blob, up] = await Promise.all([
    compressImage(file),
    api("/comments/upload-url", { method: "POST", body: { filename: uploadFilename(file) } }),
  ]);
  const form = new FormData();
  form.append("cacheControl", "3600");
  form.append("", blob);
  const resp = await fetch(up.upload_url, { method: "PUT", body: form });
  if (!resp.ok) throw new Error("Ошибка загрузки фото");
  return up.path;
}
// большие фото сжимаются в JPEG; маленькие и GIF уходят как есть
function uploadFilename(file) { return file.size < 300 * 1024 || file.type === "image/gif" ? file.name : "photo.jpg"; }

const commentFileInput = document.createElement("input");
commentFileInput.type = "file";
commentFileInput.accept = "image/*";
commentFileInput.style.display = "none";
document.body.appendChild(commentFileInput);

let pendingCommentContext = null;

$("list").addEventListener("click", (ev) => {
  const btn = ev.target.closest(".btn-attach-comment");
  if (!btn) return;
  const input = btn.closest(".comment-row").querySelector(".comment-input");
  const block = btn.closest(".event-comments-block");
  pendingCommentContext = { eventId: block.dataset.eventId, inputEl: input };
  commentFileInput.click();
});

commentFileInput.addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  if (!file) return;
  ev.target.value = "";

  // чат челленджа использует тот же input
  if (typeof currentChallenge !== "undefined" && currentChallenge) {
    const btn = $("chatSendBtn");
    btn.disabled = true;
    try {
      const path = await uploadCommentPhoto(file);
      const text = $("chatMessageInput").value.trim();
      await api(`/challenges/${currentChallenge.id}/messages`, { method: "POST", body: { text, photo_id: path } });
      $("chatMessageInput").value = "";
      await loadMessages(currentChallenge.id, true, true);
      tg.HapticFeedback?.notificationOccurred("success");
    } catch (e) {
      toast(e.message);
    } finally {
      btn.disabled = false;
    }
    return;
  }

  if (pendingCommentContext) {
    const { eventId, inputEl } = pendingCommentContext;
    pendingCommentContext = null;
    sendEventComment(eventId, inputEl, file);   // оптимистично: «📷 Загружается…» сразу
  }
});

// Подтягивает ссылки только для картинок без src; параллельно, с кэшем и дедупликацией запросов
async function loadCommentImages(root = document) {
  const images = root.querySelectorAll("img.comment-img[data-src]:not([src])");
  await Promise.all([...images].map(async (img) => {
    const path = img.dataset.src;
    if (!path) return;
    const url = await fetchPhotoUrl(path);
    if (!img.isConnected) return;
    if (url) {
      img.src = url;
    } else {
      img.alt = "Ошибка загрузки";
    }
  }));
}

// открытие фото — одним делегированным обработчиком (а не onclick на каждой картинке)
$("list").addEventListener("click", (ev) => {
  const img = ev.target.closest("img.comment-img[src]");
  if (img) window.open(img.src, "_blank");
});
