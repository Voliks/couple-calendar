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
        const oldInput = el.querySelector(".cl-input");
        const typed = oldInput ? oldInput.value : "";
        const hadFocus = oldInput && oldInput === document.activeElement;
        const sel = hadFocus ? [oldInput.selectionStart, oldInput.selectionEnd] : null;
        el.replaceWith(fresh);
        const newInput = fresh.querySelector(".cl-input");
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

function renderList() {
  $("listTitle").textContent = "События · " + DAY_FORMAT.format(parse(selected));
  const items = state.events
    .filter((e) => dayHasDate(e, selected))
    .map((e) => ({ id: String(e.id), html: cardHtml(e) }));
  if (!reconcile($("list"), items)) {
    $("list").innerHTML = `<div class="empty">На этот день ничего нет. Нажмите «+», чтобы добавить событие.</div>`;
  }
}

function replaceEvent(updated) {
  const i = state.events.findIndex((e) => e.id === updated.id);
  if (i >= 0) state.events[i] = updated; else state.events.push(updated);
  monthCache[monthKey()] = state.events;
  etag = null;
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
      const updated = await api(`/events/${eventId}/items/${itemId}`, { method: "PUT", body: { checked } });
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
      const updated = await api(`/events/${eventId}/items/${itemId}`, { method: "DELETE" });
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
      const updated = await api(`/events/${eventId}/items`, { method: "POST", body: { text } });
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
        { method: "POST", body: { position: newPosition } }
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
      const updated = await api(`/events/${event.id}/reaction`, { method: "POST", body: { emoji } });
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

$("list").addEventListener("click", async (ev) => {
  const card = ev.target.closest(".card");
  if (!card) return;
  const eventId = card.dataset.id;
  const event = findEvent(eventId);
  if (!event) return;

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
    openCommentDialog(event.my_decline_comment || "", async (comment) => {
      try {
        const updated = await api(`/events/${eventId}/comment`, { method: "POST", body: { comment } });
        replaceEvent(updated);
        tg.HapticFeedback?.notificationOccurred("success");
      } catch (err) { toast(err.message); }
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
    if (newStatus === "declined") {
      openCommentDialog(event.my_decline_comment || "", async (comment) => {
        try {
          const updated = await api(`/events/${eventId}/respond`, { method: "POST", body: { status: "declined", comment } });
          replaceEvent(updated);
          tg.HapticFeedback?.notificationOccurred("success");
        } catch (e) { toast(e.message); await load(); }
      });
      return;
    }
    statusBtn.parentElement.querySelectorAll("button").forEach((b) => (b.disabled = true));
    try {
      const updated = await api(`/events/${eventId}/respond`, { method: "POST", body: { status: newStatus } });
      replaceEvent(updated);
      tg.HapticFeedback?.notificationOccurred("success");
    } catch (e) { toast(e.message); await load(); }
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
});