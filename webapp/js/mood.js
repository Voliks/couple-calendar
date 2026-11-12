// Настроение дня.
const MOOD_COLLAPSED_KEY = "mood_collapsed";

let moodState = { date: "", mine: null, partner: null, partner_id: 0 };

function moodDateStr() {
  return todayStr();
}

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

function moodCellHtml(label, m, isMine) {
  const has = !!(m && (m.emoji || m.text));
  const cls = "mood-cell" + (has ? "" : " empty");
  if (!has) {
    return `<div class="${cls}">
      <span class="m-emoji">💭</span>
      <div style="flex:1;min-width:0">
        <div class="m-label">${label}</div>
        <div class="m-text">${isMine ? "Поделитесь настроением" : "Пока не поделился(ась)"}</div>
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
  const editBtn = $("moodEditBtn");
  
  if (!block || !row || !quickInput || !editBtn) {
    console.warn("Не найдены элементы настроения в HTML");
    return;
  }

  if (!state.has_partner) {
    block.hidden = true;
    return;
  }
  block.hidden = false;

  const mineHas = !!(moodState.mine && (moodState.mine.emoji || moodState.mine.text));

  if (mineHas) {
    quickInput.hidden = true;
    row.hidden = false;
    editBtn.hidden = false;
    row.innerHTML =
      moodCellHtml("Вы", moodState.mine, true) +
      moodCellHtml("Партнёр", moodState.partner, false);
  } else {
    quickInput.hidden = false;
    row.hidden = true;
    editBtn.hidden = true;
  }
}

async function quickSetMood() {
  if (!state.has_partner) return;
  const text = ($("moodQuickText").value || "").trim().slice(0, 280);
  const emoji = $("moodQuickEmoji").value || "";
  
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
    $("moodQuickEmoji").value = "";
    renderMood();
    tg.HapticFeedback?.notificationOccurred("success");
  } catch (e) {
    toast(e.message);
  } finally {
    btn.disabled = false;
  }
}

function resetMoodForEdit() {
  $("moodQuickText").value = (moodState.mine && moodState.mine.text) || "";
  $("moodQuickEmoji").value = (moodState.mine && moodState.mine.emoji) || "";
  $("moodRow").hidden = true;
  $("moodQuickInput").hidden = false;
  $("moodEditBtn").hidden = true;
}

// Связка событий
(() => {
  initMoodCollapsed();

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

  const editBtn = $("moodEditBtn");
  if (editBtn) editBtn.addEventListener("click", resetMoodForEdit);
})();

// Вызывается из main.js#render()
window.refreshMoodAndBanner = () => {
  if (!state.has_partner) return;
  
  if (state.moods) {
    moodState = {
      date: state.moods.date,
      mine: state.moods.mine || { emoji: "", text: "" },
      partner: state.moods.partner || { emoji: "", text: "" },
      partner_id: state.partner_id || 0,
    };
    renderMood();
  } else {
    // Если в state.moods пусто, показываем пустой блок
    moodState.mine = { emoji: "", text: "" };
    renderMood();
  }
};