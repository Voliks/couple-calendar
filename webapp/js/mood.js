// Настроение дня. Ленивый модуль, грузится вместе с main.js.

const MOOD_EMOJIS = ["😊","😐","😔","😡","🥰","😴","🤒","🤩","😢","😤","🥳","🤔"];
const MOOD_COLLAPSED_KEY = "mood_collapsed";

let moodState = { date: "", mine: null, partner: null, partner_id: 0 };
let moodSelected = "";
let moodLoadSeq = 0;

function moodDateStr() {
  return todayStr();
}

/* ---------- Сворачивание/разворачивание ---------- */

function applyMoodCollapsed(collapsed) {
  const block = $("moodBlock");
  if (!block) return;
  block.classList.toggle("collapsed", collapsed);
  try { localStorage.setItem(MOOD_COLLAPSED_KEY, collapsed ? "1" : "0"); } catch (e) {}
}

function initMoodCollapsed() {
  let collapsed = false;
  try { collapsed = localStorage.getItem(MOOD_COLLAPSED_KEY) === "1"; } catch (e) {}
  applyMoodCollapsed(collapsed);

  const toggle = $("moodToggle");
  if (toggle) {
    toggle.addEventListener("click", (e) => {
      if (e.target.closest("#moodEditBtn")) return;
      const block = $("moodBlock");
      if (!block) return;
      applyMoodCollapsed(!block.classList.contains("collapsed"));
      tg.HapticFeedback?.selectionChanged();
    });
  }
}

/* ---------- Отрисовка ---------- */

async function loadMood() {
  if (!state.has_partner) return;
  const seq = ++moodLoadSeq;
  try {
    const res = await api(`/mood?date=${encodeURIComponent(moodDateStr())}`);
    if (seq !== moodLoadSeq) return;
    moodState = {
      date: res.date,
      mine: res.mine || { emoji: "", text: "" },
      partner: res.partner || { emoji: "", text: "" },
      partner_id: res.partner_id || 0,
    };
    renderMood();
  } catch (e) {
    console.warn("mood load:", e.message);
  }
}

function moodCellHtml(label, m, isMine) {
  const has = !!(m && (m.emoji || m.text));
  const cls = "mood-cell" + (has ? "" : " empty");
  if (!has) {
    return `<div class="${cls}">
      <span class="m-emoji">💭</span>
      <div style="flex:1;min-width:0">
        <div class="m-label">${label}</div>
        <div class="m-text">${isMine ? "Нажмите «Изменить»" : "Пока не поделился(ась)"}</div>
      </div>
    </div>`;
  }
  return `<div class="${cls}">
    <span class="m-emoji">${esc(m.emoji || "💭")}</span>
    <div style="flex:1;min-width:0">
      <div class="m-label">${label}</div>
      <div class="m-text">${esc(m.text || "")}</div>
    </div>
  </div>`;
}

function renderMood() {
  const block = $("moodBlock");
  const row = $("moodRow");
  const quickInput = $("moodQuickInput");
  if (!block || !row || !quickInput) return;
  if (!state.has_partner) {
    block.hidden = true;
    return;
  }
  block.hidden = false;

  const mineHas = !!(moodState.mine && (moodState.mine.emoji || moodState.mine.text));

  if (!mineHas) {
    row.innerHTML = "";
    quickInput.hidden = false;
    
    const emojisContainer = $("moodQuickEmojis");
    if (emojisContainer && !emojisContainer.innerHTML) {
      emojisContainer.innerHTML = MOOD_EMOJIS.map((e) =>
        `<button type="button" data-quick-emoji="${e}">${e}</button>`
      ).join("");
    }
  } else {
    quickInput.hidden = true;
    row.innerHTML =
      moodCellHtml("Вы", moodState.mine, true) +
      moodCellHtml("Партнёр", moodState.partner, false);
  }
}

/* ---------- Быстрая установка (без модалки) ---------- */

async function quickSetMood() {
  if (!state.has_partner) return;
  const text = ($("moodQuickText").value || "").trim().slice(0, 280);
  const emoji = moodSelected || "";
  
  if (!emoji && !text) {
    return toast("Выберите эмодзи или напишите текст");
  }

  const btn = $("moodQuickSave");
  btn.disabled = true;
  try {
    await api("/mood", {
      method: "POST",
      body: { date: moodDateStr(), emoji, text },
    });
    moodState.mine = { emoji, text };
    $("moodQuickText").value = "";
    moodSelected = "";
    renderMood();
    tg.HapticFeedback?.notificationOccurred("success");
  } catch (e) {
    toast(e.message);
  } finally {
    btn.disabled = false;
  }
}

/* ---------- Редактор настроения (модалка) ---------- */

function openMoodEditor() {
  if (!state.has_partner) return;
  moodSelected = (moodState.mine && moodState.mine.emoji) || "";
  $("moodText").value = (moodState.mine && moodState.mine.text) || "";
  const grid = $("moodEmojiGrid");
  grid.innerHTML = MOOD_EMOJIS.map((e) =>
    `<button type="button" class="mood-emoji${e === moodSelected ? " selected" : ""}" data-emoji="${e}">${e}</button>`
  ).join("");
  $("moodOverlay").hidden = false;
  tg.BackButton?.show();
}

function closeMoodEditor() {
  $("moodOverlay").hidden = true;
  if (!$("overlay").hidden) tg.BackButton?.show(); else tg.BackButton?.hide();
}

async function saveMood() {
  if (!state.has_partner) return;
  const text = $("moodText").value.trim().slice(0, 280);
  const emoji = moodSelected || "";
  const btn = $("moodSave");
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = "Сохраняем…";
  try {
    await api("/mood", {
      method: "POST",
      body: { date: moodDateStr(), emoji, text },
    });
    moodState.mine = { emoji, text };
    renderMood();
    closeMoodEditor();
    tg.HapticFeedback?.notificationOccurred("success");
  } catch (e) {
    toast(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

/* ---------- Связка событий ---------- */

(() => {
  initMoodCollapsed();

  const quickEmojis = $("moodQuickEmojis");
  if (quickEmojis) {
    quickEmojis.addEventListener("click", (e) => {
      const b = e.target.closest("[data-quick-emoji]");
      if (!b) return;
      moodSelected = moodSelected === b.dataset.quickEmoji ? "" : b.dataset.quickEmoji;
      quickEmojis.querySelectorAll("button").forEach((el) => {
        el.classList.toggle("selected", el.dataset.quickEmoji === moodSelected);
      });
      tg.HapticFeedback?.selectionChanged();
    });
  }

  const quickSaveBtn = $("moodQuickSave");
  if (quickSaveBtn) quickSaveBtn.addEventListener("click", quickSetMood);
  
  const quickTextInput = $("moodQuickText");
  if (quickTextInput) {
    quickTextInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        quickSetMood();
      }
    });
  }

  const grid = $("moodEmojiGrid");
  if (grid) {
    grid.addEventListener("click", (e) => {
      const b = e.target.closest("[data-emoji]");
      if (!b) return;
      moodSelected = moodSelected === b.dataset.emoji ? "" : b.dataset.emoji;
      grid.querySelectorAll(".mood-emoji").forEach((el) => {
        el.classList.toggle("selected", el.dataset.emoji === moodSelected);
      });
      tg.HapticFeedback?.selectionChanged();
    });
  }

  const editBtn = $("moodEditBtn");
  if (editBtn) editBtn.addEventListener("click", openMoodEditor);
  
  const cancelBtn = $("moodCancel");
  if (cancelBtn) cancelBtn.addEventListener("click", closeMoodEditor);
  
  const saveBtn = $("moodSave");
  if (saveBtn) saveBtn.addEventListener("click", saveMood);

  const ov = $("moodOverlay");
  if (ov) {
    ov.addEventListener("click", (e) => {
      if (e.target === ov) closeMoodEditor();
    });
  }

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && ov && !ov.hidden) closeMoodEditor();
  });
})();

// Точка входа из main.js#render()
window.refreshMoodAndBanner = () => {
  if (state.has_partner) loadMood();
};