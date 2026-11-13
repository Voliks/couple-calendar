let allChallenges = [];
let catalogData = {};
let currentChallenge = null;
let chatPollTimer = null;
let chatPollGen = 0;
let chatMessages = [];
let chatPending = [];      // отправляемые сообщения (показываются сразу, полупрозрачные)
let chatTmpSeq = 0;
let chatOpChain = Promise.resolve();   // отправки идут строго по порядку
let showArchive = false;
let selectedIdeaData = null;
let selectedCustomChallengeText = "";
let selectedCategory = "";
let challengesRequestSeq = 0;
let chatRequestSeq = 0;
let chatLoadPromise = null;
let creatingChallenge = false;
let uploadingPhotos = false;

/* ---------- Список ---------- */

function syncArchiveUi() {
  const archiveBtn = $("toggleArchiveBtn");
  if (archiveBtn) {
    archiveBtn.textContent = showArchive ? "⬅️ Назад" : "🗄 Архив";
    archiveBtn.style.background = showArchive ? "var(--accent)" : "var(--bg2)";
    archiveBtn.style.color = showArchive ? "#fff" : "var(--text)";
  }
  $("createChallengeArea").style.display = showArchive ? "none" : "block";
}

function visibleChallenges() {
  return showArchive
    ? allChallenges.filter(c => c.status === "archived" || c.is_archived)
    : allChallenges.filter(c => c.status !== "archived" && !c.is_archived);
}

async function openChallenges({ refresh = false, focusId = null } = {}) {
  const requestId = ++challengesRequestSeq;
  const light = refresh && Object.keys(catalogData).length > 0;
  if (!light) {
    $("challengesOverlay").hidden = false;
    tg.BackButton?.show();
    $("challengesList").innerHTML = `<div class="spinner" style="margin:20px auto;"></div>`;
  }
  syncArchiveUi();

  try {
    const res = await api(light ? "/challenges?refresh=1" : "/challenges");
    if (requestId !== challengesRequestSeq || $("challengesOverlay").hidden) return;
    
    allChallenges = [...(res.history || [])].sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
    if (res.catalog) catalogData = res.catalog;
    $("challengeBalance").textContent = res.balance ?? 0;

    // Скрываем бейдж, так как мы прочитали челленджи
    const badge = $("challengeBadge");
    if (badge) badge.hidden = true;

    if (!light) {
      if (!selectedCategory && Object.keys(catalogData).length > 0) {
        selectedCategory = Object.keys(catalogData)[0];
        $("selectedCatText").textContent = selectedCategory;
      }
      renderCategories();
      updateIdeaList();
    }
    renderChallenges(visibleChallenges());

    if (focusId) {
      const c = allChallenges.find(x => x.id === focusId);
      if (c) {
        requestAnimationFrame(() => openChallengeDetails(focusId));
      } else {
        toast("Вызов не найден — возможно, он удалён");
      }
    }
  } catch (e) {
    if (requestId !== challengesRequestSeq || $("challengesOverlay").hidden) return;
    $("challengesList").innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    toast(e.message);
  }
}

function statusLabel(c) {
  const isCreator = c.is_creator;
  switch (c.status) {
    case "pending":   return isCreator ? "⏳ Ждёт" : "⏳ Тебе бросили";
    case "submitted": return "🔍 На проверке";
    case "approved":  return "✅ Выполнено";
    case "rejected":  return "❌ Отклонено";
    case "archived":  return "🗄 В архиве";
    default:          return c.status;
  }
}

function renderChallenges(listToRender = []) {
  const list = $("challengesList");
  const scrollTop = list.scrollTop;
  if (!listToRender.length) {
    list.innerHTML = `<div class="empty">${showArchive ? "Архив пуст" : "Пока нет вызовов. Брось первый!"}</div>`;
    return;
  }

  list.innerHTML = listToRender.map(c => {
    const cls = [
      "challenge-card",
      c.is_creator ? "" : "them",
      c.status === "approved" ? "done" : "",
      c.status === "rejected" ? "rejected" : ""
    ].filter(Boolean).join(" ");
    
    let dateStr = "";
    if (c.created_at) {
      const d = new Date(c.created_at * 1000);
      dateStr = d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' });
    }

    const archiveIcon = c.status !== "archived" && !c.is_archived && !showArchive 
      ? `<button class="archive-btn" data-archive-id="${c.id}" style="background:none; border:none; color:var(--hint); font-size:22px; cursor:pointer; padding: 0 8px; line-height:1;" title="Убрать в архив">×</button>` 
      : "";

    return `<div class="${cls}" data-challenge-id="${c.id}" style="padding: 10px 12px; margin-bottom: 0; border-radius: 12px; border-left-width: 3px; display: flex; flex-direction: column; gap: 6px; position: relative;">
      
      <div class="challenge-header" style="margin: 0; font-size: 11px; display: flex; justify-content: space-between; align-items: center; width: 100%;">
        <span>${esc(c.category)}</span>
        <div style="display: flex; align-items: center; gap: 6px;">
          <span style="opacity: 0.8;">${statusLabel(c)} · ${dateStr}</span>
          ${archiveIcon}
        </div>
      </div>
      
      <div class="challenge-text" style="font-size: 14px; line-height: 1.3; padding-right: 20px;">${esc(c.text)}</div>
      <div class="challenge-reward" style="font-size: 12px; margin-top: 2px;">🏆 ${c.reward} баллов</div>
    </div>`;
  }).join("");
  
  list.scrollTop = scrollTop;
}

/* ---------- Категории и Задания (аккордеоны) ---------- */

function renderCategories() {
  const list = $("catAccordionContent");
  list.innerHTML = Object.keys(catalogData).map(cat => {
    const isSelected = cat === selectedCategory;
    return `<div class="card accepted" style="margin-bottom: 4px; display: flex; justify-content: space-between; align-items: center; padding: 10px 12px; border-left: 4px solid ${isSelected ? 'var(--accent)' : 'transparent'}; cursor: pointer; background: ${isSelected ? 'rgba(36,129,204,0.1)' : 'var(--bg2)'};" data-cat="${esc(cat)}">
      <div style="font-size: 14px; font-weight: 500;">${esc(cat)}</div>
    </div>`;
  }).join("");
}

function updateIdeaList() {
  const ideas = catalogData[selectedCategory] || [];
  const list = $("challengeIdeaList");
  
  if (!ideas.length) {
    list.innerHTML = `<div style="padding: 10px; color: var(--hint); text-align: center; font-size: 14px;">Нет доступных заданий</div>`;
    return;
  }

  list.innerHTML = ideas.map((idea, index) => {
    const text = typeof idea === "object" ? idea.challenge : idea;
    const points = typeof idea === "object" ? idea.points : 10;
    
    if (index === 0 && !selectedIdeaData) {
      selectedIdeaData = { text, points };
      $("newChallengeReward").value = points;
      $("selectedIdeaText").textContent = text;
    }
    
    const isSelected = selectedIdeaData && selectedIdeaData.text === text;

    return `<div class="card accepted" style="margin-bottom: 4px; display: flex; justify-content: space-between; align-items: center; padding: 10px 12px; border-left: 4px solid ${isSelected ? 'var(--accent)' : 'transparent'}; cursor: pointer; background: ${isSelected ? 'rgba(36,129,204,0.1)' : 'var(--bg2)'};" data-idea-idx="${index}" data-idea-text="${esc(text)}" data-idea-points="${points}">
      <div style="font-size: 14px; font-weight: 500; word-break: break-word; margin-right: 10px;">${esc(text)}</div>
      <div style="font-size: 13px; font-weight: bold; white-space: nowrap; color: #eab308;">🏆 ${points}</div>
    </div>`;
  }).join("");
}

/* ---------- Создание ---------- */

async function createChallenge() {
  if (creatingChallenge) return;
  const text = selectedCustomChallengeText || (selectedIdeaData ? selectedIdeaData.text : "");
  if (!text) return toast("Выберите или введите задание");

  const points = parseInt($("newChallengeReward").value, 10) || 10;
  const hours = parseInt($("challengeDeadlineSelect").value, 10) || 24;

  const btn = $("createChallengeBtn");
  creatingChallenge = true;
  btn.disabled = true;
  btn.textContent = "Отправка…";
  try {
    await api("/challenges", { method: "POST", body: { category: selectedCategory, text, reward: points, hours } });
    selectedCustomChallengeText = "";
    toast("Вызов брошен!");
    tg.HapticFeedback?.notificationOccurred("success");
    await openChallenges({ refresh: true });
  } catch (e) {
    toast(e.message);
  } finally {
    creatingChallenge = false;
    btn.disabled = false;
    btn.textContent = "Отправить партнёру";
  }
}

function closeChallenges() {
  $("challengesOverlay").hidden = true;
  showArchive = false;
  if (!$("overlay").hidden || !$("challengeChatOverlay").hidden) tg.BackButton?.show();
  else tg.BackButton?.hide();
}

/* ---------- Детали челленджа (чат) ---------- */

function openChallengeDetails(id) {
  const c = allChallenges.find(x => x.id === id);
  if (!c) return;
  currentChallenge = c;
  chatRequestSeq++;
  chatLoadPromise = null;

  $("challengeChatOverlay").hidden = false;
  tg.BackButton?.show();

  $("chatCategory").textContent = c.category;
  $("chatTaskText").textContent = c.text;
  $("chatReward").textContent = `🏆 ${c.reward} баллов`;
  
  let deadlineStr = "";
  if (c.deadline > 0) {
    const d = new Date(c.deadline * 1000);
    deadlineStr = ` | ⏳ До: ${d.toLocaleDateString()} ${d.toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})}`;
  }
  $("chatStatus").textContent = `Статус: ${statusLabel(c)}${deadlineStr}`;

  const hasPhoto = !!c.photo_id;
  $("chatPhotoBlock").style.display = hasPhoto ? "block" : "none";
  
  if (hasPhoto) {
    const count = c.photo_id.split(",").filter(Boolean).length;
    $("chatViewPhotoBtn").textContent = count > 1 ? `🖼 Посмотреть фото (${count} шт.)` : `🖼 Посмотреть фото`;
  }

  const isTarget = !c.is_creator;
  const canAttach = isTarget && (c.status === "pending" || c.status === "rejected");
  $("chatActionBlock").style.display = canAttach ? "block" : "none";

  const isCreator = c.is_creator;
  const canReview = isCreator && (c.status === "pending" || c.status === "submitted");
  $("chatCreatorActions").style.display = canReview ? "flex" : "none";

  const canDelete = isCreator && c.status === "pending";
  $("chatDeleteBlock").style.display = canDelete ? "block" : "none";

  loadMessages(id);
  startChatPolling(id);
}

function startChatPolling(id) {
  stopChatPolling();
  const gen = ++chatPollGen;
  const tick = async (delay) => {
    if (gen !== chatPollGen) return;
    let next = delay;
    if (!document.hidden && !$("challengeChatOverlay").hidden && currentChallenge?.id === id) {
      const fresh = await loadMessages(id, true);
      next = fresh ? 8000 : Math.min(Math.round(delay * 1.5), 30000);
    }
    if (gen === chatPollGen) chatPollTimer = setTimeout(() => tick(next), next);
  };
  chatPollTimer = setTimeout(() => tick(8000), 8000);
}

function stopChatPolling() {
  chatPollGen++;
  if (chatPollTimer) { clearTimeout(chatPollTimer); chatPollTimer = null; }
}

function closeChallengeDetails() {
  $("challengeChatOverlay").hidden = true;
  currentChallenge = null;
  chatPending = [];
  chatRequestSeq++;
  chatLoadPromise = null;
  stopChatPolling();
  if (!$("challengesOverlay").hidden) tg.BackButton?.show();
  else tg.BackButton?.hide();
}

/* ---------- Логика чата (сообщения) ---------- */

function renderChatMessages(list, forceScroll = false) {
  const ownIds = new Set(chatMessages.map((m) => m.id));
  // отправленное, но ещё не подтянутое опросом, показываем как «в пути»; уже пришедшее — скрываем
  const pending = chatPending.filter((p) => !(p.serverId && ownIds.has(p.serverId)));
  const all = chatMessages.concat(pending);
  if (!all.length) {
    list.innerHTML = `<div style="text-align:center; color:var(--hint); font-size:12px; padding:10px;">Сообщений пока нет.</div>`;
    return;
  }
  // не дёргаем прокрутку, если человек читает историю выше
  const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 90;
  const myId = state?.my_id || 0;
  list.innerHTML = all.map(m => {
    const isMine = m.sender_id === myId;
    const align = isMine ? "align-self: flex-end; background: var(--accent); color: #fff;" : "align-self: flex-start; background: var(--bg2); color: var(--text);";

    let imgHtml = "";
    if (m.photo_id) {
      const known = typeof cachedPhotoUrl === "function" ? cachedPhotoUrl(m.photo_id) : "";
      imgHtml = `<img data-src="${esc(m.photo_id)}" class="comment-img" alt="Фото" loading="lazy" decoding="async"${known ? ` src="${esc(known)}"` : ""}>`;
    }

    const delBtn = isMine && !m.pending
      ? `<button type="button" class="msg-del" data-msg-del="${m.id}" aria-label="Удалить сообщение" style="background:none; border:none; color:inherit; font-size:14px; padding:0 4px; opacity:0.7; cursor:pointer;">×</button>`
      : "";

    return `<div style="max-width: 80%; padding: 8px 12px; border-radius: 12px; font-size: 14px; word-break: break-word; position: relative; ${align}${m.pending ? " opacity:.6;" : ""}">
      ${m.text ? `<div>${esc(m.text)}</div>` : ""}
      ${imgHtml}
      ${delBtn}
    </div>`;
  }).join("");
  if (forceScroll || nearBottom) list.scrollTop = list.scrollHeight;

  if (typeof loadCommentImages === "function") loadCommentImages(list);
}

async function loadMessages(challengeId, silent = false, force = false) {
  const list = $("chatMessagesList");
  if (!list) return 0;
  if (silent && chatLoadPromise && !force) return chatLoadPromise;
  if (!silent) {
    chatMessages = [];
    list.innerHTML = `<div style="text-align:center; color:var(--hint); font-size:12px; padding:10px;">Загрузка...</div>`;
  }
  const requestId = ++chatRequestSeq;
  const after = chatMessages.length ? chatMessages[chatMessages.length - 1].id : 0;

  const request = (async () => {
    try {
      const res = await api(`/challenges/${challengeId}/messages${after ? `?after=${after}` : ""}`);
      if (requestId !== chatRequestSeq || currentChallenge?.id !== challengeId || $("challengeChatOverlay").hidden) return 0;
      const lastId = chatMessages.length ? chatMessages[chatMessages.length - 1].id : 0;
      const fresh = (res.messages || []).filter(m => m.id > lastId);
      if (!fresh.length && silent) return 0;
      const first = chatMessages.length === 0;
      chatMessages = chatMessages.concat(fresh);
      renderChatMessages(list, first);
      return fresh.length;
    } catch (e) {
      if (requestId === chatRequestSeq && !silent) list.innerHTML = `<div style="text-align:center; color:rgb(var(--declined)); font-size:12px; padding:10px;">Ошибка: ${esc(e.message)}</div>`;
      return 0;
    }
  })();
  if (silent) chatLoadPromise = request;
  try { return await request; }
  finally { if (chatLoadPromise === request) chatLoadPromise = null; }
}

// Сообщение появляется мгновенно; поле ввода не блокируется, клавиатура остаётся открытой
function sendMessage() {
  if (!currentChallenge) return;
  const input = $("chatMessageInput");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  const challengeId = currentChallenge.id;
  const tmp = { id: `t${++chatTmpSeq}`, sender_id: state.my_id, text, photo_id: "", pending: true, serverId: 0 };
  chatPending.push(tmp);
  renderChatMessages($("chatMessagesList"), true);
  tg.HapticFeedback?.impactOccurred?.("light");

  chatOpChain = chatOpChain.then(async () => {
    try {
      const res = await api(`/challenges/${challengeId}/messages`, { method: "POST", body: { text }, adopt: true });
      tmp.serverId = res.message?.id || 0;
      // подтягиваем инкрементально (вдруг есть и сообщения партнёра) — так порядок и `after` не ломаются
      if (currentChallenge?.id === challengeId) await loadMessages(challengeId, true, true);
    } catch (e) {
      toast(e.message);
      if (!input.value) input.value = text;
    } finally {
      chatPending = chatPending.filter((p) => p !== tmp);
      if (currentChallenge?.id === challengeId) renderChatMessages($("chatMessagesList"));
    }
  });
}

/* ---------- Загрузка фото ---------- */

// Сжатие фото — общая compressImage из list.js (1600 px, JPEG 0.82), чтобы настройки были в одном месте

$("challengePhotoInput").addEventListener("change", async (ev) => {
  const pickedFiles = Array.from(ev.target.files || []);
  if (!pickedFiles.length || !currentChallenge || uploadingPhotos) {
    ev.target.value = "";
    return;
  }

  const c = currentChallenge;
  const btn = $("chatAttachBtn");
  const original = btn.textContent;
  uploadingPhotos = true;
  btn.disabled = true;
  const validFiles = pickedFiles.filter(file => !file.type || file.type.startsWith("image/"));
  const uploadedPaths = new Array(validFiles.length);
  let nextIndex = 0;
  let completed = 0;
  let uploadError = null;

  async function uploadWorker() {
    while (!uploadError && nextIndex < validFiles.length) {
      const index = nextIndex++;
      const file = validFiles[index];
      try {
        btn.textContent = `Подготовка ${completed + 1}/${validFiles.length}…`;
        const preparedFile = await compressImage(file);
        const up = await api("/challenges/upload-url", { method: "POST", body: { filename: "photo.jpg", challenge_id: c.id } });
        const form = new FormData();
        form.append("cacheControl", "3600");
        form.append("", preparedFile);
        const resp = await fetch(up.upload_url, { method: "PUT", body: form });
        if (!resp.ok) throw new Error(`Ошибка при загрузке файла ${index + 1}`);
        uploadedPaths[index] = up.path;
        completed++;
        btn.textContent = `Загрузка ${completed}/${validFiles.length}…`;
      } catch (error) {
        uploadError ||= error;
        throw error;
      }
    }
  }

  try {
    if (!validFiles.length) throw new Error("Выберите файлы изображений");
    const workers = await Promise.allSettled(Array.from({ length: Math.min(2, validFiles.length) }, () => uploadWorker()));
    const failedWorker = workers.find(result => result.status === "rejected");
    if (failedWorker) throw failedWorker.reason;
    btn.textContent = "Сохранение...";
    await api(`/challenges/${c.id}/photo`, { method: "POST", body: { path: uploadedPaths.join(",") } });

    toast(`Загружено фото: ${uploadedPaths.length}`);
    tg.HapticFeedback?.notificationOccurred("success");
    closeChallengeDetails();
    await openChallenges({ refresh: true });
  } catch (e) {
    toast(e.message || "Ошибка загрузки");
  } finally {
    uploadingPhotos = false;
    btn.disabled = false;
    btn.textContent = original;
    ev.target.value = "";
  }
});

/* ---------- Действия создателя ---------- */

async function challengeAction(action) {
  if (!currentChallenge) return;
  const c = currentChallenge;
  try {
    await api(`/challenges/${c.id}/action`, { method: "POST", body: { action } });
    toast(action === "approve" ? "Засчитано!" : "Отклонено");
    tg.HapticFeedback?.notificationOccurred("success");
    closeChallengeDetails();
    await openChallenges({ refresh: true });
  } catch (e) { toast(e.message); }
}

/* ---------- Просмотр фото ---------- */

async function viewChallengePhoto() {
  if (!currentChallenge?.photo_id) return;
  const btn = $("chatViewPhotoBtn");
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Загрузка фото…";
  
  try {
    const paths = currentChallenge.photo_id.split(",").filter(Boolean);
    const results = await Promise.all(paths.map(path =>
      api(`/challenges/photo-url?path=${encodeURIComponent(path)}`)
    ));
    const urls = results.map(result => result.view_url).filter(Boolean);
    if (!urls.length) throw new Error("Ссылки не получены");

    const container = $("photoViewerContent");
    container.replaceChildren();
    urls.forEach((url, index) => {
      let parsed;
      try { parsed = new URL(url, location.href); } catch { return; }
      if (parsed.protocol !== "https:" && parsed.protocol !== "http:") return;
      const img = document.createElement("img");
      img.src = parsed.href;
      img.loading = "lazy";
      img.alt = `Фото ${index + 1}`;
      img.style.cssText = "max-width: 100%; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.5);";
      container.appendChild(img);
    });
    if (!container.childElementCount) throw new Error("Получены некорректные ссылки на фото");
    
    $("photoViewerOverlay").hidden = false;
    
  } catch (e) {
    toast(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

/* ---------- Привязка событий ---------- */

try {
  tg.BackButton?.onClick(() => {
    if (!$("photoViewerOverlay").hidden) {
      $("photoViewerOverlay").hidden = true;
      return;
    }
    if (!$("challengeChatOverlay").hidden) closeChallengeDetails();
    else if (!$("challengesOverlay").hidden) closeChallenges();
  });
} catch (e) {}

(() => {
  $("catAccordionHeader").onclick = () => {
    const content = $("catAccordionContent");
    content.style.display = content.style.display === "none" ? "block" : "none";
  };

  $("ideaAccordionHeader").onclick = () => {
    const content = $("ideaAccordionContent");
    content.style.display = content.style.display === "none" ? "block" : "none";
  };

  $("catAccordionContent").addEventListener("click", (e) => {
    const card = e.target.closest("[data-cat]");
    if (!card) return;
    selectedCategory = card.dataset.cat;
    selectedIdeaData = null;
    selectedCustomChallengeText = "";
    $("customChallengeInput").value = "";
    $("selectedCatText").textContent = selectedCategory;
    $("selectedIdeaText").textContent = "Не выбрано";
    renderCategories();
    updateIdeaList();
    $("catAccordionContent").style.display = "none";
  });

  $("challengeIdeaList").addEventListener("click", (e) => {
    const card = e.target.closest("[data-idea-idx]");
    if (!card) return;
    const text = card.dataset.ideaText;
    const points = parseInt(card.dataset.ideaPoints, 10);
    selectedIdeaData = { text, points };
    selectedCustomChallengeText = "";
    $("customChallengeInput").value = ""; 
    $("newChallengeReward").value = points;
    $("selectedIdeaText").textContent = text;
    updateIdeaList();
    $("ideaAccordionContent").style.display = "none";
  });

  $("customChallengeBtn").onclick = () => {
    const text = $("customChallengeInput").value.trim();
    if (!text) return toast("Введите текст задания");
    selectedIdeaData = null;
    selectedCustomChallengeText = text;
    $("selectedIdeaText").textContent = text;
    $("customChallengeInput").value = "";
    $("newChallengeReward").value = "10";
    toast("Своё задание добавлено!");
    $("ideaAccordionContent").style.display = "none";
  };

  $("customChallengeInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      $("customChallengeBtn").click();
    }
  });

  $("toggleArchiveBtn").onclick = () => {
    showArchive = !showArchive;
    syncArchiveUi();
    renderChallenges(visibleChallenges());
  };

  $("challengesClose").onclick = closeChallenges;
  $("createChallengeBtn").onclick = createChallenge;

  $("challengesList").addEventListener("click", async (ev) => {
    const archiveBtn = ev.target.closest(".archive-btn");
    if (archiveBtn) {
      ev.stopPropagation();
      ev.preventDefault();
      
      const id = archiveBtn.dataset.archiveId;
      archiveBtn.textContent = "⏳";
      archiveBtn.disabled = true;
      
      try {
        await api(`/challenges/${id}/archive`, { method: "POST" });
        tg.HapticFeedback?.notificationOccurred("success");
        toast("Убрано в архив");
        const c = allChallenges.find((x) => String(x.id) === String(id));
        if (c) c.status = "archived";
        renderChallenges(visibleChallenges());
      } catch (err) {
        toast(err.message);
        archiveBtn.textContent = "×";
        archiveBtn.disabled = false;
      }
      return;
    }
  });

  $("challengesList").addEventListener("click", (ev) => {
    if (ev.target.closest(".archive-btn")) return;
    const card = ev.target.closest("[data-challenge-id]");
    if (!card) return;
    openChallengeDetails(Number(card.dataset.challengeId));
  });

  $("challengesOverlay").addEventListener("click", (e) => {
    if (e.target === $("challengesOverlay")) closeChallenges();
  });
  $("challengeChatOverlay").addEventListener("click", (e) => {
    if (e.target === $("challengeChatOverlay")) closeChallengeDetails();
  });

  $("photoViewerClose").onclick = () => { $("photoViewerOverlay").hidden = true; };
  $("photoViewerOverlay").onclick = (e) => { 
    if (e.target === $("photoViewerOverlay")) $("photoViewerOverlay").hidden = true; 
  };

  $("chatBackBtn").onclick = closeChallengeDetails;
  $("chatAttachBtn").onclick = () => $("challengePhotoInput").click();
  $("chatViewPhotoBtn").onclick = viewChallengePhoto;
  $("chatApproveBtn").onclick = () => challengeAction("approve");
  $("chatRejectBtn").onclick = () => challengeAction("reject");
  
  $("chatDeleteBtn").onclick = async () => {
    if (!currentChallenge) return;
    confirmDialog("Точно удалить этот вызов?", async () => {
      try {
        await api(`/challenges/${currentChallenge.id}`, { method: "DELETE" });
        toast("Вызов удален");
        tg.HapticFeedback?.notificationOccurred("success");
        closeChallengeDetails();
        await openChallenges({ refresh: true });
      } catch (e) { toast(e.message); }
    });
  };

  $("chatSendBtn").onclick = sendMessage;
  $("chatMessageInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); sendMessage(); }
  });

  // 📷 Кнопка фото в чате
  const attachBtn = $("chatAttachCommentBtn");
  if (attachBtn) {
    attachBtn.onclick = () => {
      if (!currentChallenge) return;
      if (typeof commentFileInput !== "undefined" && commentFileInput) {
        commentFileInput.click();
      } else {
        toast("Ошибка: поле загрузки не найдено");
      }
    };
  }

  // 🗑 Удаление сообщения в чате челленджа
  $("chatMessagesList").addEventListener("click", async (ev) => {
    const btn = ev.target.closest("[data-msg-del]");
    if (!btn || !currentChallenge) return;
    ev.preventDefault();
    ev.stopPropagation();
    const msgId = btn.dataset.msgDel;
    confirmDialog("Удалить это сообщение?", async () => {
      const challengeId = currentChallenge?.id;
      const removed = chatMessages.find(m => String(m.id) === String(msgId));
      if (!challengeId || !removed) return;
      chatMessages = chatMessages.filter(m => m !== removed);
      renderChatMessages($("chatMessagesList"));
      try {
        await api(`/challenges/${challengeId}/messages/${msgId}`, { method: "DELETE", adopt: true });
        tg.HapticFeedback?.notificationOccurred("success");
      } catch (e) {
        if (e.status !== 404 && currentChallenge?.id === challengeId) {   // 404 — уже удалено, откат не нужен
          chatMessages = chatMessages.concat(removed).sort((x, y) => x.id - y.id);
          renderChatMessages($("chatMessagesList"));
        }
        toast(e.message);
      }
    });
  });
})();