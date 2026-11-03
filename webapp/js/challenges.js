let allChallenges = [];
let catalogData = {};
let currentChallenge = null;
let chatPollTimer = null;
let showArchive = false;

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

  try {
    const res = await api("/challenges");
    
    // Сортируем: новые сверху
    allChallenges = (res.history || []).sort((a, b) => b.created_at - a.created_at);
    
    catalogData = res.catalog || {};
    $("challengeBalance").textContent = res.balance ?? 0;

    // Скрываем интерфейс создания, если мы в архиве
    const formRow = document.querySelector(".row:has(#createChallengeBtn)");
    if (formRow) formRow.style.display = showArchive ? "none" : "grid";
    
    const elementsToToggle = [
      "label[for='challengeCategorySelect']", "#challengeCategorySelect",
      "label[for='challengeIdeaSelect']", "#challengeIdeaSelect",
      "#customChallengeBlock", 
      "label[for='newChallengeReward']", "#newChallengeReward",
      "label[for='challengeDeadlineSelect']", "#challengeDeadlineSelect"
    ];
    elementsToToggle.forEach(sel => {
      const el = document.querySelector(sel);
      if (el) el.style.display = showArchive ? "none" : (sel === "#customChallengeBlock" ? "flex" : "block");
    });

    // Фильтруем: в основном списке только 3 последних неархивных, в архиве — все архивные
    let filtered;
    if (showArchive) {
      filtered = allChallenges.filter(c => c.is_archived);
    } else {
      filtered = allChallenges.filter(c => !c.is_archived).slice(0, 3);
    }
    
    const catSelect = $("challengeCategorySelect");
    const prevCat = catSelect.value;
    catSelect.innerHTML = Object.keys(catalogData).map(cat =>
      `<option value="${esc(cat)}">${esc(cat)}</option>`
    ).join("");
    if (prevCat && catalogData[prevCat]) catSelect.value = prevCat;

    updateIdeaSelect();
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

    return `<div class="${cls}" data-challenge-id="${c.id}">
      <div class="challenge-header">
        <span>${esc(c.category)}</span>
        <span style="text-align: right; display: flex; flex-direction: column; align-items: flex-end;">
          <span>${statusLabel(c)}</span>
          <span style="font-size: 10px; opacity: 0.6; margin-top: 2px;">${dateStr}</span>
        </span>
      </div>
      <div class="challenge-text">${esc(c.text)}</div>
      <div class="challenge-reward">🏆 ${c.reward} баллов</div>
    </div>`;
  }).join("");
}

/* ---------- Выбор идеи / создание ---------- */

function updateIdeaSelect() {
  const cat = $("challengeCategorySelect").value;
  const ideas = catalogData[cat] || [];
  const sel = $("challengeIdeaSelect");
  sel.innerHTML = ideas.map(idea => {
    const text = typeof idea === "object" ? idea.challenge : idea;
    const points = typeof idea === "object" ? idea.points : 10;
    return `<option value="${esc(text)}" data-points="${points}">${esc(text)} (${points} б.)</option>`;
  }).join("");
  updateRewardInput();
}

function updateRewardInput() {
  const sel = $("challengeIdeaSelect");
  const opt = sel.options[sel.selectedIndex];
  if (opt && opt.dataset.points) {
    $("newChallengeReward").value = opt.dataset.points;
  }
}

async function createChallenge() {
  const cat = $("challengeCategorySelect").value;
  const sel = $("challengeIdeaSelect");
  const opt = sel.options[sel.selectedIndex];
  if (!opt || !opt.value) return toast("Выберите задание");

  const text = opt.value;
  const points = parseInt($("newChallengeReward").value, 10) || parseInt(opt.dataset.points, 10) || 10;
  const hours = parseInt($("challengeDeadlineSelect").value, 10) || 24;

  const btn = $("createChallengeBtn");
  btn.disabled = true;
  btn.textContent = "Отправка…";
  try {
    await api("/challenges", { method: "POST", body: { category: cat, text, reward: points, hours } });
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
  
  // Вывод дедлайна
  let deadlineStr = "";
  if (c.deadline > 0) {
    const d = new Date(c.deadline * 1000);
    deadlineStr = ` | ⏳ До: ${d.toLocaleDateString()} ${d.toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})}`;
  }
  $("chatStatus").textContent = `Статус: ${statusLabel(c)}${deadlineStr}`;

  // Фото
  const hasPhoto = !!c.photo_id;
  $("chatPhotoBlock").style.display = hasPhoto ? "block" : "none";
  
  if (hasPhoto) {
    const count = c.photo_id.split(",").filter(Boolean).length;
    $("chatViewPhotoBtn").textContent = count > 1 
      ? `🖼 Посмотреть фото (${count} шт.)` 
      : `🖼 Посмотреть прикрепленное фото`;
  }

  // Исполнитель: прикрепить фото, если pending или rejected
  const isTarget = !c.is_creator;
  const canAttach = isTarget && (c.status === "pending" || c.status === "rejected");
  $("chatActionBlock").style.display = canAttach ? "block" : "none";

  // Создатель: засчитать/отклонить, если submitted
  const isCreator = c.is_creator;
  const canReview = isCreator && c.status === "submitted";
  $("chatCreatorActions").style.display = canReview ? "flex" : "none";

  // Показываем кнопку удаления (только создатель и только в статусе pending)
  const canDelete = isCreator && c.status === "pending";
  const delBlock = $("chatDeleteBlock");
  if (delBlock) delBlock.style.display = canDelete ? "block" : "none";

  // Загружаем историю сообщений
  loadMessages(id);

  // Запускаем автообновление чата каждые 4 секунды
  if (chatPollTimer) clearInterval(chatPollTimer);
  chatPollTimer = setInterval(() => {
    if (!$("challengeChatOverlay").hidden && currentChallenge?.id === id) {
      loadMessages(id, true); // true = тихая загрузка
    }
  }, 4000);
}

function closeChallengeDetails() {
  $("challengeChatOverlay").hidden = true;
  currentChallenge = null;
  
  if (chatPollTimer) {
    clearInterval(chatPollTimer);
    chatPollTimer = null;
  }

  if (!$("challengesOverlay").hidden) tg.BackButton?.show();
  else tg.BackButton?.hide();
}

/* ---------- Логика чата (сообщения) ---------- */

async function loadMessages(challengeId, silent = false) {
  const list = $("chatMessagesList");
  if (!list) return;
  
  if (!silent) {
    list.innerHTML = `<div style="text-align:center; color:var(--hint); font-size:12px; padding:10px;">Загрузка...</div>`;
  }
  
  try {
    const res = await api(`/challenges/${challengeId}/messages`);
    const msgs = res.messages || [];
    
    if (!msgs.length) {
      if (!silent) list.innerHTML = `<div style="text-align:center; color:var(--hint); font-size:12px; padding:10px;">Сообщений пока нет. Напишите первым!</div>`;
      return;
    }
    
    const myId = state?.my_id || 0; 
    
    const currentCount = list.querySelectorAll('div').length;
    if (silent && currentCount === msgs.length) return;

    list.innerHTML = msgs.map(m => {
      const isMine = m.sender_id === myId;
      const align = isMine ? "align-self: flex-end; background: var(--accent); color: #fff;" : "align-self: flex-start; background: var(--bg2); color: var(--text);";
      return `<div style="max-width: 80%; padding: 8px 12px; border-radius: 12px; font-size: 14px; word-break: break-word; ${align}">
        ${esc(m.text)}
      </div>`;
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
  
  input.value = "";
  input.disabled = true;
  
  try {
    await api(`/challenges/${currentChallenge.id}/messages`, {
      method: "POST",
      body: { text }
    });
    await loadMessages(currentChallenge.id);
  } catch (e) {
    toast(e.message);
  } finally {
    input.disabled = false;
    input.focus();
  }
}

/* ---------- Загрузка фото в Supabase (мультизагрузка) ---------- */

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
  } catch (e) { /* оставляем оригинал */ }
  return file;
}

$("challengePhotoInput").addEventListener("change", async (ev) => {
  const pickedFiles = ev.target.files;
  ev.target.value = ""; // Очищаем инпут
  if (!pickedFiles || !pickedFiles.length || !currentChallenge) return;

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
      
      const preparedFile = await prepareImage(file);

      const up = await api("/challenges/upload-url", {
        method: "POST",
        body: { filename: preparedFile.name || "photo.jpg", challenge_id: c.id },
      });

      const form = new FormData();
      form.append("cacheControl", "3600");
      form.append("", preparedFile);
      const resp = await fetch(up.upload_url, { method: "PUT", body: form });
      
      if (!resp.ok) {
        console.error("Supabase upload error", resp.status);
        throw new Error(`Ошибка при загрузке файла ${i + 1}`);
      }
      
      uploadedPaths.push(up.path);
    }

    if (uploadedPaths.length === 0) {
      throw new Error("Ни один файл не был загружен");
    }

    // Сохраняем все пути в БД через запятую
    const allPathsString = uploadedPaths.join(",");
    
    await api(`/challenges/${c.id}/photo`, { 
      method: "POST", 
      body: { path: allPathsString } 
    });

    toast(`Загружено фото: ${uploadedPaths.length}`);
    tg.HapticFeedback?.notificationOccurred("success");
    closeChallengeDetails();
    await openChallenges();
  } catch (e) {
    toast(e.message || "Не удалось загрузить фото");
  } finally {
    btn.disabled = false;
    btn.textContent = original;
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
    await openChallenges();
  } catch (e) {
    toast(e.message);
  }
}

/* ---------- Просмотр фото (галерея) ---------- */

async function viewChallengePhoto() {
  if (!currentChallenge?.photo_id) return;
  const btn = $("chatViewPhotoBtn");
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Открываем…";
  
  try {
    const paths = currentChallenge.photo_id.split(",").filter(Boolean);
    const urls = [];

    // Получаем ссылки для всех фото
    for (const path of paths) {
      const res = await api(`/challenges/photo-url?path=${encodeURIComponent(path)}`);
      if (res.view_url) urls.push(res.view_url);
    }

    if (!urls.length) throw new Error("Ссылки не получены");

    if (tg.openLink && urls.length === 1) {
      tg.openLink(urls[0]);
    } else if (tg.showImages) {
      // Встроенный просмотрщик Telegram (для нескольких фото)
      tg.showImages(urls);
    } else {
      // Фолбэк: открываем каждую ссылку в новой вкладке
      urls.forEach(url => window.open(url, "_blank"));
    }
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
    if (!$("challengeChatOverlay").hidden) closeChallengeDetails();
    else if (!$("challengesOverlay").hidden) closeChallenges();
  });
} catch (e) {}

(() => {
  const btn = $("challengeBtn");
  if (btn) btn.onclick = openChallenges;

  const closeBtn = $("challengesClose");
  if (closeBtn) closeBtn.onclick = closeChallenges;

  const catSelect = $("challengeCategorySelect");
  if (catSelect) catSelect.onchange = updateIdeaSelect;

  const ideaSelect = $("challengeIdeaSelect");
  if (ideaSelect) ideaSelect.onchange = updateRewardInput;

  const createBtn = $("createChallengeBtn");
  if (createBtn) createBtn.onclick = createChallenge;

  // Кнопка переключения Архива
  const archiveBtn = $("toggleArchiveBtn");
  if (archiveBtn) {
    archiveBtn.onclick = () => {
      showArchive = !showArchive;
      openChallenges();
    };
  }

  $("challengesList").addEventListener("click", (ev) => {
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

  const backBtn = $("chatBackBtn");
  if (backBtn) backBtn.onclick = closeChallengeDetails;

  const attachBtn = $("chatAttachBtn");
  if (attachBtn) attachBtn.onclick = () => $("challengePhotoInput").click();

  const viewBtn = $("chatViewPhotoBtn");
  if (viewBtn) viewBtn.onclick = viewChallengePhoto;

  const approveBtn = $("chatApproveBtn");
  if (approveBtn) approveBtn.onclick = () => challengeAction("approve");

  const rejectBtn = $("chatRejectBtn");
  if (rejectBtn) rejectBtn.onclick = () => challengeAction("reject");

  // Кнопка удаления
  const deleteBtn = $("chatDeleteBtn");
  if (deleteBtn) {
    deleteBtn.onclick = async () => {
      if (!currentChallenge) return;
      confirmDialog("Точно удалить этот вызов?", async () => {
        try {
          await api(`/challenges/${currentChallenge.id}`, { method: "DELETE" });
          toast("Вызов удален");
          tg.HapticFeedback?.notificationOccurred("success");
          closeChallengeDetails();
          await openChallenges();
        } catch (e) {
          toast(e.message);
        }
      });
    };
  }

  // Добавление своего задания
  const customBtn = $("customChallengeBtn");
  const customInput = $("customChallengeInput");
  
  if (customBtn && customInput) {
    customBtn.onclick = () => {
      const text = customInput.value.trim();
      if (!text) return toast("Введите текст задания");
      
      const sel = $("challengeIdeaSelect");
      const newOption = document.createElement("option");
      newOption.value = text;
      newOption.textContent = text + " (Своё)";
      newOption.dataset.points = "10"; // По умолчанию 10 баллов
      newOption.selected = true;
      
      sel.appendChild(newOption);
      customInput.value = "";
      $("newChallengeReward").value = "10"; // Обновляем поле с баллами
    };
    
    customInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        customBtn.click();
      }
    });
  }

  const sendBtn = $("chatSendBtn");
  if (sendBtn) sendBtn.onclick = sendMessage;

  const msgInput = $("chatMessageInput");
  if (msgInput) {
    msgInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        sendMessage();
      }
    });
  }
})();