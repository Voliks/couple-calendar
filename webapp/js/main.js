let sessionDead = false;

function showSessionExpired() {
  if (sessionDead) return;
  sessionDead = true;
  $("app").hidden = true;
  $("welcome").hidden = true;
  $("loadingSpinner").hidden = true;
  $("loadingHint").textContent = "Сессия истекла. Закройте и снова откройте приложение через бота.";
  $("loading").hidden = false;
}

async function load(silent = false) {
  if (sessionDead) return;
  const key = monthKey();
  try {
    const r = await request("/state?month=" + key, { etag: etagKey === key ? etag : null });
    if (key !== monthKey()) return false;
    if (r.status === 304) return false;
    state = { ...state, ...r.data };
    if (!(state.multi_select instanceof Set)) state.multi_select = new Set();
    etag = r.etag; etagKey = key;
    monthCache[key] = state.events;
    render();
    return true;
  } catch (e) {
    if (e.status === 401 && loaded) { showSessionExpired(); return; }
    if (!loaded) throw e;
    if (!silent) toast(e.message);
  }
}

async function bootstrap() {
  const hint = $("loadingHint");
  const slow = setTimeout(() => { hint.textContent = "Сервер просыпается, это может занять до минуты…"; }, 5000);
  for (;;) {
    try { 
      await loadConfig(); // Загружаем статику (цвета, реакции) один раз
      await load();       // Первый state уже содержит sex_ideas
      break; 
    }
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
  
  if (canShowApp) { 
    renderCalendar(); 
    renderList(); 
    renderStats(); 
    
    const badge = $("challengeBadge");
    if (badge) {
      badge.hidden = !state.has_new_challenges;
    }

    // Показываем баннер мягкой отвязки, если он есть
    const restoreBlock = $("restoreBlock");
    if (restoreBlock) {
      restoreBlock.hidden = !state.is_unlinking;
    }

    // Плашка «сегодня» + настроение дня (mood.js)
    if (typeof window.refreshMoodAndBanner === "function") {
      window.refreshMoodAndBanner();
    }
  }
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
  confirmDialog("Отвязать партнёра? Общие данные будут удалены навсегда через 24 часа. За это время вы сможете передумать и восстановить связь.", async () => {
    try {
      await api("/unlink", { method: "POST" });
      toast("Связь разорвана. У вас 24 часа на откат.");
      for (const k of Object.keys(monthCache)) delete monthCache[k];
      etag = null;
      await load();
    } catch (e) { toast(e.message); }
  });
};

$("restoreBtn")?.addEventListener("click", async () => {
  try {
    await api("/unlink/restore", { method: "POST" });
    toast("Связь восстановлена!");
    etag = null;
    await load();
  } catch (e) {
    toast(e.message);
  }
});

/* ---------- ТУРБО-ЗАГРУЗКА МОДУЛЕЙ ---------- */

// Читаем версии из <meta name="asset-versions">
let ASSET_V = {};
try {
  const meta = document.querySelector('meta[name="asset-versions"]');
  if (meta && meta.content) {
    ASSET_V = JSON.parse(meta.content);
  }
} catch (e) {
  console.warn("Не удалось прочитать версии ассетов", e);
}
const assetUrl = (name) => `js/${name}` + (ASSET_V[name] ? `?v=${ASSET_V[name]}` : "");

function loadScript(src) {
  return new Promise((resolve, reject) => {
    if (document.querySelector(`script[src="${src}"]`)) return resolve();
    const s = document.createElement("script");
    s.src = src;
    s.async = true;
    s.onload = resolve;
    s.onerror = () => reject(new Error("Ошибка загрузки"));
    document.head.appendChild(s);
  });
}

$("challengeBtn").onclick = async () => {
  if (typeof openChallenges !== "function") {
    const icon = $("challengeBtn").querySelector(".nav-icon");
    const oldText = icon.textContent;
    icon.textContent = "⏳";
    try {
      await loadScript(assetUrl("challenges.js"));
    } catch (e) {
      toast("Не удалось загрузить челленджи");
      icon.textContent = oldText;
      return;
    }
    icon.textContent = oldText;
  }
  if (typeof showArchive !== "undefined") {
    showArchive = false;
  }
  openChallenges();
};

$("shopBtn").onclick = async () => {
  if (typeof openShop !== "function") {
    const icon = $("shopBtn").querySelector(".nav-icon");
    const oldText = icon.textContent;
    icon.textContent = "⏳";
    try {
      await loadScript(assetUrl("shop.js"));
    } catch (e) {
      toast("Не удалось загрузить магазин");
      icon.textContent = oldText;
      return;
    }
    icon.textContent = oldText;
  }
  openShop();
};

/* ---------- ОПРОС СЕРВЕРА ---------- */

function startPolling() {
  const canPoll = () => !sessionDead && !document.hidden && !anyOverlayOpen()
    && pendingOps === 0 && !reactionPickerEl && !dragState && !gridDrag;

  // Интервал зависит от наличия партнёра: без партнёра — реже
  const baseDelay = () => state.has_partner ? 20000 : 60000;
  const maxDelay = () => state.has_partner ? 60000 : 120000;

  let delay = baseDelay();
  let pollTimer = null;

  const tick = async () => {
    if (canPoll()) {
      try {
        const changed = await load(true);
        delay = changed ? baseDelay() : Math.min(delay * 2, maxDelay());
      } catch (e) {}
    }
    scheduleNext();
  };

  const scheduleNext = () => {
    if (pollTimer) clearTimeout(pollTimer);
    if (document.hidden) return;
    pollTimer = setTimeout(tick, delay);
  };

  scheduleNext();

  document.addEventListener("pointerdown", () => { delay = baseDelay(); }, { passive: true });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      if (pollTimer) {
        clearTimeout(pollTimer);
        pollTimer = null;
      }
    } else {
      delay = baseDelay();
      if (canPoll()) {
        load(true).finally(() => scheduleNext());
      } else {
        scheduleNext();
      }
    }
  });
}

bootstrap();