async function load(silent = false) {
  const key = monthKey();
  try {
    const r = await request("/state?month=" + key, { etag: etagKey === key ? etag : null });
    if (key !== monthKey()) return;
    if (r.status === 304) return;
    state = { ...state, ...r.data };
    if (!(state.multi_select instanceof Set)) state.multi_select = new Set();
    etag = r.etag; etagKey = key;
    monthCache[key] = state.events;
    render();
  } catch (e) {
    if (!loaded) throw e;
    if (!silent) toast(e.message);
  }
}

async function bootstrap() {
  const hint = $("loadingHint");
  const slow = setTimeout(() => { hint.textContent = "Сервер просыпается, это может занять до минуты…"; }, 5000);
  for (;;) {
    try { await load(); break; }
    catch (e) {
      if (e.status === 401) {
        clearTimeout(slow);
        $("loadingSpinner").hidden = true;
        hint.textContent = "Откройте приложение через бота в Telegram.";
        return;
      }
      hint.textContent = e.message + " Пробуем снова…";
      await sleep(3000);
    }
  }
  clearTimeout(slow);
  loaded = true;
  $("loading").hidden = true;
  render();
  startPolling();
}

function render() {
  $("welcome").hidden = state.has_partner;
  const canShowApp = state.has_partner || (state.events && state.events.length > 0);
  $("app").hidden = !canShowApp;
  if (canShowApp) { renderCalendar(); renderList(); renderStats(); }
}

function renderStats() {
  const block = $("statsBlock");
  const grid = $("statsGrid");
  const s = state.stats;
  if (!s || (!s.total && !s.pending && !s.accepted && !s.declined)) {
    block.hidden = true;
    return;
  }
  block.hidden = false;
  grid.innerHTML = `
    <div class="stat-cell pending"><div class="num">${s.pending}</div><div class="lbl">Ждут</div></div>
    <div class="stat-cell accepted"><div class="num">${s.accepted}</div><div class="lbl">Принято</div></div>
    <div class="stat-cell declined"><div class="num">${s.declined}</div><div class="lbl">Отклонено</div></div>
  `;
}

$("archiveBtn").onclick = async () => {
  try {
    const r = await api("/todos/archive");
    const items = r.items || [];
    if (!items.length) {
      $("archiveList").innerHTML = `<div class="cl-empty">Архив пуст</div>`;
    } else {
      $("archiveList").innerHTML = items.map(e => `
        <div class="archive-item">
          <span class="archive-dot" style="background:${esc(e.color || "#8b5cf6")}"></span>
          <div class="archive-text">
            <div>${esc(e.title)}${e.description ? ` — ${esc(e.description)}` : ""}</div>
            <div class="archive-date">${esc(e.date)}${e.time ? " · " + esc(e.time) : ""}</div>
          </div>
        </div>
      `).join("");
    }
    $("archiveOverlay").hidden = false;
    tg.BackButton?.show();
  } catch (e) { toast(e.message); }
};
$("archiveClose").onclick = () => {
  $("archiveOverlay").hidden = true;
  if (!$("overlay").hidden) tg.BackButton?.show(); else tg.BackButton?.hide();
};
$("archiveOverlay").addEventListener("click", (e) => {
  if (e.target === $("archiveOverlay")) $("archiveClose").click();
});

$("share").onclick = () => {
  const url = "https://t.me/share/url?url=" + encodeURIComponent(state.invite_link) +
              "&text=" + encodeURIComponent("Давай вести общий календарь на двоих!");
  tg.openTelegramLink(url);
};
$("copy").onclick = async () => {
  try { await navigator.clipboard.writeText(state.invite_link); toast("Ссылка скопирована"); }
  catch { toast(state.invite_link); }
};
$("unlink").onclick = () => {
  confirmDialog("Отвязать партнёра? Все общие события будут удалены безвозвратно. Личные (приватные) события останутся у вас.", async () => {
    try {
      await api("/unlink", { method: "POST" });
      toast("Партнёр отвязан");
      for (const k of Object.keys(monthCache)) delete monthCache[k];
      etag = null;
      await load();
    } catch (e) { toast(e.message); }
  });
};

(() => {
  const fab = $("add");
  let lastY = window.scrollY;
  let ticking = false;
  const THRESHOLD = 8;

  function onScroll() {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => {
      const y = window.scrollY;
      const diff = y - lastY;
      if (y < 40) {
        fab.classList.remove("fab-hidden");
      } else if (diff > THRESHOLD) {
        fab.classList.add("fab-hidden");
      } else if (diff < -THRESHOLD) {
        fab.classList.remove("fab-hidden");
      }
      lastY = y;
      ticking = false;
    });
  }

  window.addEventListener("scroll", onScroll, { passive: true });

  const observer = new MutationObserver(() => {
    const anyOpen = !$("overlay").hidden || !$("ideasOverlay").hidden || !$("archiveOverlay").hidden || !$("catOverlay").hidden;
    if (anyOpen) fab.classList.remove("fab-hidden");
  });
  ["overlay", "ideasOverlay", "archiveOverlay", "catOverlay"].forEach(id => {
    observer.observe($(id), { attributes: true, attributeFilter: ["hidden"] });
  });
})();

function startPolling() {
  const canPoll = () => !document.hidden && $("overlay").hidden && $("ideasOverlay").hidden
    && $("archiveOverlay").hidden && $("catOverlay").hidden
    && pendingOps === 0 && !reactionPickerEl && !dragState && !gridDrag;
  setInterval(() => { if (canPoll()) load(true); }, 15000);
  document.addEventListener("visibilitychange", () => { if (canPoll()) load(true); });
}
$("scanBtn").onclick = () => {
  // Если Telegram WebApp доступен, показываем нативный попап
  if (tg.showPopup) {
    tg.showPopup({
      title: "Добавить фото",
      message: "Выберите источник",
      buttons: [
        { id: "camera", type: "default", text: "📷 Камера" },
        { id: "gallery", type: "default", text: "🖼 Галерея" },
        { id: "cancel", type: "cancel", text: "Отмена" }
      ]
    }, (buttonId) => {
      if (buttonId === "camera") {
        openFileInput(true);  // true = камера
      } else if (buttonId === "gallery") {
        openFileInput(false); // false = галерея
      }
    });
  } else {
    // Fallback для обычного браузера
    openFileInput(false);
  }
};

function openFileInput(useCamera) {
  const input = $("photoInput");
  if (!input) return;
  
  // Сбрасываем предыдущий файл
  input.value = "";
  
  // Настраиваем input под камеру или галерею
  if (useCamera) {
    input.setAttribute("capture", "environment");
  } else {
    input.removeAttribute("capture");
  }
  
  input.click();
}

$("photoInput").onchange = async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  
  $("photoInput").value = "";
  
  toast("Отправляем фото...");
  $("scanBtn").disabled = true;
  
  try {
    const res = await uploadPhoto(file);
    toast(`Готово! Добавлено ${res.added} событий`);
    await load();
  } catch (err) {
    toast(err.message);
  } finally {
    $("scanBtn").disabled = false;
  }
};

bootstrap();