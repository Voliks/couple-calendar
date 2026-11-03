let allChallenges = [];
let catalogData = {};
let currentChallenge = null;
let chatPollTimer = null;
let showArchive = false;
let selectedIdeaData = null; 
let selectedCategory = ""; 

/* ---------- Список ---------- */

async function openChallenges() {
  $("challengesOverlay").hidden = false;
  tg.BackButton?.show();
  $("challengesList").innerHTML = `<div class="spinner" style="margin:20px auto;"></div>`;
  
  // Обновляем кнопку архива
  const archiveBtn = $("toggleArchiveBtn");
  if (archiveBtn) {
    archiveBtn.textContent = showArchive ? "⬅️ Назад" : "🗄 Архив";
    archiveBtn.style.background = showArchive ? "var(--accent)" : "var(--bg2)";
    archiveBtn.style.color = showArchive ? "#fff" : "var(--text)";
  }

  // Скрываем блок создания в архиве
  $("createChallengeArea").style.display = showArchive ? "none" : "block";

  try {
    const res = await api("/challenges");
    // Сортируем: новые сверху
    allChallenges = (res.history || []).sort((a, b) => b.created_at - a.created_at);
    
    catalogData = res.catalog || {};
    $("challengeBalance").textContent = res.balance ?? 0;

    // Показываем либо архив, либо все активные (без slice, чтобы можно было прокручивать)
    let filtered;
    if (showArchive) {
      // В архиве показываем только то, что сервер пометил как is_archived
      filtered = allChallenges.filter(c => c.is_archived);
    } else {
      // В основном списке показываем:
      // 1. Все активные (pending, submitted)
      // 2. И только 3 последних завершенных (approved, rejected), которые еще не ушли в архив
      const active = allChallenges.filter(c => c.status === "pending" || c.status === "submitted");
      const recentlyDone = allChallenges.filter(c => (c.status === "approved" || c.status === "rejected") && !c.is_archived).slice(0, 3);
      
      filtered = [...active, ...recentlyDone].sort((a, b) => b.created_at - a.created_at);
    }

    // Инициализируем категории (если еще не выбрана)
    if (!selectedCategory && Object.keys(catalogData).length > 0) {
      selectedCategory = Object.keys(catalogData)[0];
      $("selectedCatText").textContent = selectedCategory;
    }

    renderCategories();
    updateIdeaList();
    renderChallenges(filtered);
  } catch (e) {
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
    default:          return c.status;
  }
}

function renderChallenges(listToRender = []) {
  const list = $("challengesList");
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

    // Крестик для ручной архивации (показываем только если челлендж не в архиве)
    const archiveIcon = !c.is_archived && !showArchive 
      ? `<button class="archive-btn" data-archive-id="${c.id}" style="background:none; border:none; color:var(--hint); font-size:18px; cursor:pointer; padding: 0 4px; margin-left: 8px;" title="Убрать в архив">×</button>` 
      : "";

    return `<div class="${cls}" data-challenge-id="${c.id}" style="padding: 10px 12px; margin-bottom: 0; border-radius: 12px; border-left-width: 3px; display: flex; flex-direction: column; gap: 6px; position: relative;">
      
      <div class="challenge-header" style="margin: 0; font-size: 11px; display: flex; justify-content: space-between; align-items: flex-start;">
        <span>${esc(c.category)}</span>
        <div style="display: flex; align-items: center;">
          <span style="opacity: 0.8;">${statusLabel(c)} · ${dateStr}</span>
          ${archiveIcon}
        </div>
      </div>
      
      <div class="challenge-text" style="font-size: 14px; line-height: 1.3; padding-right: 20px;">${esc(c.text)}</div>
      <div class="challenge-reward" style="font-size: 12px; margin-top: 2px;">🏆 ${c.reward} баллов</div>
    </div>`;
  }).join("");
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
    
    // По умолчанию выбираем первое
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
  let text = selectedIdeaData ? selectedIdeaData.text : "";
  const customInput = $("customChallengeInput");
  if (customInput && customInput.value.trim()) {
    text = customInput.value.trim();
  }

  if (!text) return toast("Выберите или введите задание");

  const points = parseInt($("newChallengeReward").value, 10) || 10;
  const hours = parseInt($("challengeDeadlineSelect").value, 10) || 24;

  const btn = $("createChallengeBtn");
  btn.disabled = true;
  btn.textContent = "Отправка…";
  try {
    await api("/challenges", { method: "POST", body: { category: selectedCategory, text, reward: points, hours } });
    toast("Вызов брошен!");
    tg.HapticFeedback?.notificationOccurred("success");
    await openChallenges();
  } catch (e) {
    toast(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Отправить партнёру";
  }
}

function closeChallenges() {
  $("challengesOverlay").hidden = true;
  if (!$("overlay").hidden || !$("challengeChatOverlay").hidden) tg.BackButton?.show();
  else tg.BackButton?.hide();
}

/* ---------- Детали челленджа (чат) ---------- */

function openChallengeDetails(id) {
  const c = allChallenges.find(x => x.id === id);
  if (!c) return;
  currentChallenge = c;

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
  const canReview = isCreator && c.status === "submitted";
  $("chatCreatorActions").style.display = canReview ? "flex" : "none";

  const canDelete = isCreator && c.status === "pending";
  $("chatDeleteBlock").style.display = canDelete ? "block" : "none";

  loadMessages(id);

  if (chatPollTimer) clearInterval(chatPollTimer);
  chatPollTimer = setInterval(() => {
    if (!$("challengeChatOverlay").hidden && currentChallenge?.id === id) {
      loadMessages(id, true);
    }
  }, 4000);
}

function closeChallengeDetails() {
  $("challengeChatOverlay").hidden = true;
  currentChallenge = null;
  if (chatPollTimer) { clearInterval(chatPollTimer); chatPollTimer = null; }
  if (!$("challengesOverlay").hidden) tg.BackButton?.show();
  else tg.BackButton?.hide();
}

/* ---------- Логика чата (сообщения) ---------- */

async function loadMessages(challengeId, silent = false) {
  const list = $("chatMessagesList");
  if (!list) return;
  if (!silent) list.innerHTML = `<div style="text-align:center; color:var(--hint); font-size:12px; padding:10px;">Загрузка...</div>`;
  
  try {
    const res = await api(`/challenges/${challengeId}/messages`);
    const msgs = res.messages || [];
    if (!msgs.length) {
      if (!silent) list.innerHTML = `<div style="text-align:center; color:var(--hint); font-size:12px; padding:10px;">Сообщений пока нет.</div>`;
      return;
    }
    const myId = state?.my_id || 0; 
    const currentCount = list.querySelectorAll('div').length;
    if (silent && currentCount === msgs.length) return;

    list.innerHTML = msgs.map(m => {
      const isMine = m.sender_id === myId;
      const align = isMine ? "align-self: flex-end; background: var(--accent); color: #fff;" : "align-self: flex-start; background: var(--bg2); color: var(--text);";
      return `<div style="max-width: 80%; padding: 8px 12px; border-radius: 12px; font-size: 14px; word-break: break-word; ${align}">${esc(m.text)}</div>`;
    }).join("");
    list.scrollTop = list.scrollHeight;
  } catch (e) {
    if (!silent) list.innerHTML = `<div style="text-align:center; color:rgb(var(--declined)); font-size:12px; padding:10px;">Ошибка: ${esc(e.message)}</div>`;
  }
}

async function sendMessage() {
  if (!currentChallenge) return;
  const input = $("chatMessageInput");
  const text = input.value.trim();
  if (!text) return;
  input.value = ""; input.disabled = true;
  try {
    await api(`/challenges/${currentChallenge.id}/messages`, { method: "POST", body: { text } });
    await loadMessages(currentChallenge.id);
  } catch (e) { toast(e.message); } finally { input.disabled = false; input.focus(); }
}

/* ---------- Загрузка фото ---------- */

async function prepareImage(file) {
  try {
    const bmp = await createImageBitmap(file);
    const k = Math.min(1, 1600 / Math.max(bmp.width, bmp.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bmp.width * k);
    canvas.height = Math.round(bmp.height * k);
    canvas.getContext("2d").drawImage(bmp, 0, 0, canvas.width, canvas.height);
    bmp.close?.();
    const blob = await new Promise((r) => canvas.toBlob(r, "image/jpeg", 0.85));
    if (blob && blob.size < file.size) return new File([blob], "photo.jpg", { type: "image/jpeg" });
  } catch (e) {}
  return file;
}

$("challengePhotoInput").addEventListener("change", async (ev) => {
  const pickedFiles = Array.from(ev.target.files || []);
  if (!pickedFiles.length || !currentChallenge) {
    ev.target.value = "";
    return;
  }

  const c = currentChallenge;
  const btn = $("chatAttachBtn");
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Загрузка 0/" + pickedFiles.length + "…";

  const uploadedPaths = [];
  try {
    for (let i = 0; i < pickedFiles.length; i++) {
      const file = pickedFiles[i];
      if (file.type && !file.type.startsWith("image/")) continue;
      btn.textContent = `Загрузка ${i + 1}/${pickedFiles.length}…`;
      
      let preparedFile = file;
      try { preparedFile = await prepareImage(file); } catch (err) {}

      const up = await api("/challenges/upload-url", { method: "POST", body: { filename: preparedFile.name || "photo.jpg", challenge_id: c.id } });
      const form = new FormData();
      form.append("cacheControl", "3600");
      form.append("", preparedFile);
      const resp = await fetch(up.upload_url, { method: "PUT", body: form });
      if (!resp.ok) throw new Error(`Ошибка при загрузке файла ${i + 1}`);
      uploadedPaths.push(up.path);
    }
    if (uploadedPaths.length === 0) throw new Error("Ни один файл не был загружен");
    await api(`/challenges/${c.id}/photo`, { method: "POST", body: { path: uploadedPaths.join(",") } });
    toast(`Загружено фото: ${uploadedPaths.length}`);
    tg.HapticFeedback?.notificationOccurred("success");
    closeChallengeDetails();
    await openChallenges();
  } catch (e) { toast(e.message || "Ошибка загрузки"); } finally { btn.disabled = false; btn.textContent = original; ev.target.value = ""; }
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
    await openChallenges();
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
    const urls = [];

    // Получаем все ссылки
    for (const path of paths) {
      const res = await api(`/challenges/photo-url?path=${encodeURIComponent(path)}`);
      if (res.view_url) urls.push(res.view_url);
    }

    if (!urls.length) throw new Error("Ссылки не получены");

    // Показываем наше модальное окно с фото
    const container = $("photoViewerContent");
    container.innerHTML = urls.map((url, i) => `
      <img src="${url}" style="max-width: 100%; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.5);" alt="Фото ${i+1}">
    `).join("");
    
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
  // Аккордеон категорий
  $("catAccordionHeader").onclick = () => {
    const content = $("catAccordionContent");
    content.style.display = content.style.display === "none" ? "block" : "none";
  };

  // Аккордеон заданий
  $("ideaAccordionHeader").onclick = () => {
    const content = $("ideaAccordionContent");
    content.style.display = content.style.display === "none" ? "block" : "none";
  };

  // Выбор категории
  $("catAccordionContent").addEventListener("click", (e) => {
    const card = e.target.closest("[data-cat]");
    if (!card) return;
    selectedCategory = card.dataset.cat;
    selectedIdeaData = null;
    $("selectedCatText").textContent = selectedCategory;
    $("selectedIdeaText").textContent = "Не выбрано";
    renderCategories();
    updateIdeaList();
    $("catAccordionContent").style.display = "none";
  });

  // Выбор задания
  $("challengeIdeaList").addEventListener("click", (e) => {
    const card = e.target.closest("[data-idea-idx]");
    if (!card) return;
    const text = card.dataset.ideaText;
    const points = parseInt(card.dataset.ideaPoints, 10);
    selectedIdeaData = { text, points };
    $("customChallengeInput").value = ""; 
    $("newChallengeReward").value = points;
    $("selectedIdeaText").textContent = text;
    updateIdeaList();
    $("ideaAccordionContent").style.display = "none";
  });

  // Кастомное задание
  $("customChallengeBtn").onclick = () => {
    const text = $("customChallengeInput").value.trim();
    if (!text) return toast("Введите текст задания");
    selectedIdeaData = null; 
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
    openChallenges();
  };

  $("challengeBtn").onclick = openChallenges;
  $("challengesClose").onclick = closeChallenges;
  $("createChallengeBtn").onclick = createChallenge;

    // Обработка клика по крестику (в архив)
  $("challengesList").addEventListener("click", async (e) => {
    const archiveBtn = e.target.closest(".archive-btn");
    if (archiveBtn) {
      e.stopPropagation(); // Чтобы не открылось окно челленджа
      const id = archiveBtn.dataset.archiveId;
      archiveBtn.disabled = true;
      archiveBtn.textContent = "⏳";
      
      try {
        await api(`/challenges/${id}/archive`, { method: "POST" });
        tg.HapticFeedback?.notificationOccurred("success");
        toast("Убрано в архив");
        await openChallenges();
      } catch (err) {
        toast(err.message);
        archiveBtn.disabled = false;
        archiveBtn.textContent = "×";
      }
      return;
    }
  });

  $("challengesList").addEventListener("click", async (ev) => {
    // 1. Сначала проверяем, не нажали ли мы на крестик архивации
    const archiveBtn = ev.target.closest(".archive-btn");
    if (archiveBtn) {
      ev.stopPropagation(); // Останавливаем дальнейшие клики
      ev.preventDefault();
      
      const id = archiveBtn.dataset.archiveId;
      archiveBtn.disabled = true;
      archiveBtn.textContent = "⏳";
      
      try {
        await api(`/challenges/${id}/archive`, { method: "POST" });
        tg.HapticFeedback?.notificationOccurred("success");
        toast("Убрано в архив");
        await openChallenges(); // Перерисовываем список
      } catch (err) {
        toast(err.message);
        archiveBtn.disabled = false;
        archiveBtn.textContent = "×";
      }
      return; // Выходим, чтобы не открывать карточку
    }

    // 2. Если нажали не на крестик, а на саму карточку — открываем её
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

  // Обработчики для окна просмотра фото
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
        await openChallenges();
      } catch (e) { toast(e.message); }
    });
  };

  $("chatSendBtn").onclick = sendMessage;
  $("chatMessageInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); sendMessage(); }
  });
})();