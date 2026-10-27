let gridDrag = null;

function dayElFromPoint(x, y) {
  const el = document.elementFromPoint(x, y);
  return el ? el.closest(".day") : null;
}

function applyMultiSelect(dates) {
  const next = new Set(dates);
  const cur = state.multi_select;
  if (cur && cur.size === next.size && [...next].every((d) => cur.has(d))) return;
  state.multi_select = next;
  updateMultiColorPreview();
  renderCalendar();
}

function clearMultiSelect() {
  state.multi_select = new Set();
  updateMultiColorPreview();
  renderCalendar();
}

function updateMultiColorPreview() {
  const hex = formColor || "";
  if (hex) {
    const rgb = hexToRgb(hex);
    document.documentElement.style.setProperty("--multi-color-bg", `rgba(${rgb}, .28)`);
    document.documentElement.style.setProperty("--multi-color-border", `rgba(${rgb}, .55)`);
  } else {
    document.documentElement.style.setProperty("--multi-color-bg", "rgba(99, 102, 241, .28)");
    document.documentElement.style.setProperty("--multi-color-border", "rgba(99, 102, 241, .55)");
  }
}

function statusKey(e) { return e.status === "pending" && e.date < todayStr() ? "expired" : e.status; }

function dayHasDate(event, date) {
  const end = event.date_end || event.date;
  return date >= event.date && date <= end;
}

function renderCalendar() {
  $("title").textContent = MONTHS_FORMAT.format(new Date(view.y, view.m, 1));

  const byDay = {};
  for (const e of state.events) {
    const start = e.date;
    const end = e.date_end || e.date;
    if (!start || !end) continue;
    const startD = parse(start), endD = parse(end);
    const cur = new Date(startD);
    while (cur <= endD) {
      const key = fmt(cur.getFullYear(), cur.getMonth(), cur.getDate());
      (byDay[key] ||= []).push(e);
      cur.setDate(cur.getDate() + 1);
    }
  }

  const offset = (new Date(view.y, view.m, 1).getDay() + 6) % 7;
  const days = new Date(view.y, view.m + 1, 0).getDate();
  const today = todayStr();
  const multi = state.multi_select || new Set();
  let html = "";
  for (let i = 0; i < offset; i++) html += "<div></div>";
  for (let d = 1; d <= days; d++) {
    const date = fmt(view.y, view.m, d);
    const list = byDay[date] || [];

    const personals = list.filter(x => x.category === "personal").slice().sort((a, b) => a.id - b.id);
    const personalLast = personals.length ? personals[personals.length - 1] : null;

    const todoRange = list
      .filter(x => x.category === "todo" && x.date_end && x.date_end !== x.date)
      .sort((a, b) => a.id - b.id);
    const todoRangeLast = todoRange.length ? todoRange[todoRange.length - 1] : null;

    const restRange = list
      .filter(x => x.category === "rest" && x.date_end && x.date_end !== x.date)
      .sort((a, b) => a.id - b.id);
    const restRangeLast = restRange.length ? restRange[restRange.length - 1] : null;

    let personalStyle = "";
    let hasPersonal = false;
    let fillColor = "";
    if (personalLast) {
      hasPersonal = true;
      fillColor = hexToRgb(personalLast.color || "#6366f1");
    } else if (todoRangeLast) {
      hasPersonal = true;
      fillColor = hexToRgb(todoRangeLast.color || "#8b5cf6");
    } else if (restRangeLast) {
      hasPersonal = true;
      fillColor = hexToRgb(restRangeLast.color || "#22c55e");
    }
    if (hasPersonal) {
      personalStyle = ` style="--personal-bg: rgba(${fillColor}, .28); --personal-border: rgba(${fillColor}, .55); --personal-fg: var(--text);"`;
    }

    const todos = list
      .filter(x => x.category === "todo" && !(x.date_end && x.date_end !== x.date))
      .slice().sort((a, b) => a.id - b.id);
    const todoBar = todos.length
      ? `<span class="todo-bar" aria-hidden="true">${todos.map(t =>
          `<i style="background:${esc(t.color || "#8b5cf6")}"></i>`).join("")}</span>`
      : "";

    const nonTodos = list.filter(x => {
      if (x.category === "todo" || x.category === "personal") return false;
      if (x.category === "rest" && x.date_end && x.date_end !== x.date) return false;
      return true;
    });
    const st = new Set(nonTodos.map(statusKey));
    const dots = ["pending", "accepted", "declined", "expired"]
      .filter((s) => st.has(s)).map((s) => `<i class="dot ${s}"></i>`).join("");

    const isMulti = multi.has(date);
    const cls = ["day",
      date === today && "today",
      date === selected && "selected",
      isMulti && "multi",
      hasPersonal && "has-personal",
    ].filter(Boolean).join(" ");
    const label = DAY_FORMAT.format(new Date(view.y, view.m, d))
      + (list.length ? `, событий: ${list.length}` : "");
    html += `<button class="${cls}" data-date="${date}" aria-pressed="${isMulti}" aria-label="${esc(label)}"${personalStyle}>
               ${todoBar}
               <span class="num">${d}</span><span class="dots" aria-hidden="true">${dots}</span>
             </button>`;
  }
  $("grid").innerHTML = html;
}

const LONG_PRESS_MS = 350;
const MOVE_SLOP = 8;

function cancelLongPress() {
  if (gridDrag && gridDrag.timer) { clearTimeout(gridDrag.timer); gridDrag.timer = null; }
}

// Мышь: тянем сразу. Палец: горизонтальный свайп или долгое нажатие включают выбор диапазона,
// а обычное вертикальное движение остаётся прокруткой страницы.
$("grid").addEventListener("pointerdown", (ev) => {
  if (ev.button !== undefined && ev.button !== 0) return;
  const day = ev.target.closest(".day");
  if (!day) return;
  if (!$("overlay").hidden || !$("ideasOverlay").hidden || !$("archiveOverlay").hidden || !$("catOverlay").hidden) return;

  const isTouch = ev.pointerType === "touch";
  gridDrag = {
    pointerId: ev.pointerId,
    anchor: day.dataset.date,
    active: !isTouch,
    swiping: false,
    moved: false,
    startX: ev.clientX,
    startY: ev.clientY,
    timer: null,
  };
  if (isTouch) {
    const id = ev.pointerId;
    gridDrag.timer = setTimeout(() => {
      if (gridDrag && gridDrag.pointerId === id) {
        gridDrag.timer = null;
        gridDrag.active = true;
        tg.HapticFeedback?.impactOccurred?.("light");
      }
    }, LONG_PRESS_MS);
  }
  applyMultiSelect([day.dataset.date]);
  try { $("grid").setPointerCapture(ev.pointerId); } catch (e) {}
});

$("grid").addEventListener("pointermove", (ev) => {
  if (!gridDrag || gridDrag.pointerId !== ev.pointerId) return;
  const dx = Math.abs(ev.clientX - gridDrag.startX);
  const dy = Math.abs(ev.clientY - gridDrag.startY);
  if (!gridDrag.moved && (dx > MOVE_SLOP || dy > MOVE_SLOP)) gridDrag.moved = true;

  if (!gridDrag.active) {
    if (!gridDrag.moved) return;
    cancelLongPress();
    if (dx <= dy) return;       // вертикальное движение — это прокрутка
    gridDrag.active = true;     // горизонтальный свайп — выбираем диапазон
  }

  const day = dayElFromPoint(ev.clientX, ev.clientY);
  if (!day) return;

  const a = gridDrag.anchor;
  const b = day.dataset.date;
  if (!a || !b) return;
  const [lo, hi] = a <= b ? [a, b] : [b, a];

  const dates = [];
  const cur = parse(lo), end = parse(hi);
  while (cur <= end) {
    dates.push(fmt(cur.getFullYear(), cur.getMonth(), cur.getDate()));
    cur.setDate(cur.getDate() + 1);
  }
  if (dates.length > 1) gridDrag.swiping = true;
  applyMultiSelect(dates);
});

// Пока идёт выбор диапазона пальцем, страница не должна прокручиваться
$("grid").addEventListener("touchmove", (ev) => {
  if (gridDrag && gridDrag.active) ev.preventDefault();
}, { passive: false });

function endGridDrag(ev) {
  if (!gridDrag || gridDrag.pointerId !== ev.pointerId) return;
  cancelLongPress();
  const wasSwipe = gridDrag.swiping;
  const dates = [...state.multi_select];
  gridDrag = null;

  if (!dates.length) return;

  if (wasSwipe) {
    selected = dates[0];
    renderList();
    openCategoryPicker(dates);
    return;
  }

  selected = dates[0];
  tg.HapticFeedback?.selectionChanged();
  clearMultiSelect();
  renderList();
}
$("grid").addEventListener("pointerup", endGridDrag);
$("grid").addEventListener("pointercancel", (ev) => {
  if (!gridDrag || gridDrag.pointerId !== ev.pointerId) return;
  cancelLongPress();
  gridDrag = null;
  clearMultiSelect();
});

// Клавиатура и скринридеры: Enter/Space на кнопке дня даёт click без pointer-событий
$("grid").addEventListener("click", (ev) => {
  if (ev.detail !== 0) return;
  const day = ev.target.closest(".day");
  if (!day) return;
  selected = day.dataset.date;
  clearMultiSelect();
  renderList();
  $("grid").querySelector(`[data-date="${selected}"]`)?.focus();
});

let pendingRange = [];

function openCategoryPicker(dates) {
  if (!dates.length) return;
  const sorted = [...dates].sort();
  pendingRange = sorted;
  const start = sorted[0];
  const end = sorted[sorted.length - 1];
  const isRange = end !== start;

  $("catRange").textContent = !isRange
    ? DAY_FORMAT.format(parse(start))
    : `${fmtRange(start, end)} · ${sorted.length} ${pluralDays(sorted.length)}`;

  document.querySelectorAll(".cat-btn").forEach(btn => {
    const cat = btn.dataset.cat;
    btn.hidden = isRange && (cat === "sex" || cat === "shop");
  });

  $("catOverlay").hidden = false;
  tg.BackButton?.show();
}

function pluralDays(n) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return "день";
  if (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) return "дня";
  return "дней";
}

function closeCategoryPicker() {
  $("catOverlay").hidden = true;
  if (!$("overlay").hidden) tg.BackButton?.show(); else tg.BackButton?.hide();
  pendingRange = [];
}

$("catCancel").onclick = () => { closeCategoryPicker(); clearMultiSelect(); };
$("catOverlay").addEventListener("click", (e) => {
  if (e.target === $("catOverlay")) { closeCategoryPicker(); clearMultiSelect(); }
});

document.querySelectorAll(".cat-btn").forEach(btn => {
  btn.onclick = () => {
    const cat = btn.dataset.cat;
    const dates = [...pendingRange];
    $("catOverlay").hidden = true;
    openFormForRange(dates, cat);
  };
});

function shiftMonth(delta) {
  const d = new Date(view.y, view.m + delta, 1);
  view = { y: d.getFullYear(), m: d.getMonth() };
  const t = new Date();
  const sameAsToday = view.y === t.getFullYear() && view.m === t.getMonth();
  selected = sameAsToday ? todayStr() : fmt(view.y, view.m, 1);
  state.events = monthCache[monthKey()] || [];
  clearMultiSelect();
  renderCalendar();
  renderList();
  load();
}
$("prev").onclick = () => shiftMonth(-1);
$("next").onclick = () => shiftMonth(1);

function goToday() {
  const t = new Date();
  view = { y: t.getFullYear(), m: t.getMonth() };
  selected = todayStr();
  state.events = monthCache[monthKey()] || [];
  clearMultiSelect();
  renderCalendar();
  renderList();
  load();
  tg.HapticFeedback?.selectionChanged();
}
$("todayBtn").onclick = goToday;

(() => {
  const title = $("title");
  let timer = null;
  const start = () => { timer = setTimeout(() => { timer = null; goToday(); }, 600); };
  const cancel = () => { if (timer) { clearTimeout(timer); timer = null; } };
  title.addEventListener("pointerdown", start);
  title.addEventListener("pointerup", cancel);
  title.addEventListener("pointercancel", cancel);
  title.addEventListener("pointerleave", cancel);
  title.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); goToday(); } });
})();