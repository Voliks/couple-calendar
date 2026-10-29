let allChallenges = [];
let catalogData = {};
let currentChallenge = null;
let chatPollTimer = null;

/* ---------- Список ---------- */

async function openChallenges() {
  $("challengesOverlay").hidden = false;
  tg.BackButton?.show();
  $("challengesList").innerHTML = `<div class="spinner" style="margin:20px auto;"></div>`;

  try {
    const res = await api("/challenges");
    allChallenges = res.history || [];
    catalogData = res.catalog || {};

    $("challengeBalance").textContent = res.balance ?? 0;

    const catSelect = $("challengeCategorySelect");
    const prevCat = catSelect.value;
    catSelect.innerHTML = Object.keys(catalogData).map(cat =>
      `<option value="${esc(cat)}">${esc(cat)}</option>`
    ).join("");
    if (prevCat && catalogData[prevCat]) catSelect.value = prevCat;

    updateIdeaSelect();
    renderChallenges();
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

function renderChallenges() {
  const list = $("challengesList");
  if (!allChallenges.length) {
    list.innerHTML = `<div class="empty">Пока нет вызовов. Брось первый!</div>`;
    return;
  }

  list.innerHTML = allChallenges.map(c => {
    const cls = [
      "challenge-card",
      c.is_creator ? "" : "them",
      c.status === "approved" ? "done" : "",
    ].filter(Boolean).join(" ");
    const reward = `🏆 ${c.reward} баллов`;
    return `<div class="${cls}" data-challenge-id="${c.id}">
      <div class="challenge-header">
        <span>${esc(c.category)}</span>
        <span>${statusLabel(c)}</span>
      </div>
      <div class="challenge-text">${esc(c.text)}</div>
      <div class="challenge-reward">${reward}</div>
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
  const hours = parseInt($("challengeDeadlineSelect").value, 10) || 24; // Читаем время

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

  // Исполнитель: прикрепить фото, если pending или rejected
  const isTarget = !c.is_creator;
  const canAttach = isTarget && (c.status === "pending" || c.status === "rejected");
  $("chatActionBlock").style.display = canAttach ? "block" : "none";

  // Создатель: засчитать/отклонить, если submitted
  const isCreator = c.is_creator;
  const canReview = isCreator && c.status === "submitted";
  $("chatCreatorActions").style.display = canReview ? "flex" : "none";

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

/* ---------- Загрузка фото в Supabase (signed upload URL) ---------- */

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
  const picked = ev.target.files?.[0];
  ev.target.value = "";
  if (!picked || !currentChallenge) return;
  if (picked.type && !picked.type.startsWith("image/")) return toast("Нужно изображение");

  const c = currentChallenge;
  const btn = $("chatAttachBtn");
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Загрузка…";

  try {
    const file = await prepareImage(picked);

    const up = await api("/challenges/upload-url", {
      method: "POST",
      body: { filename: file.name || "photo.jpg", challenge_id: c.id },
    });

    const form = new FormData();
    form.append("cacheControl", "3600");
    form.append("", file);
    const resp = await fetch(up.upload_url, { method: "PUT", body: form });
    if (!resp.ok) {
      console.error("Supabase upload", resp.status, await resp.text().catch(() => ""));
      throw new Error(`Хранилище отклонило загрузку (${resp.status})`);
    }

    await api(`/challenges/${c.id}/photo`, { method: "POST", body: { path: up.path } });

    toast("Фото отправлено!");
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

/* ---------- Просмотр фото ---------- */

async function viewChallengePhoto() {
  if (!currentChallenge?.photo_id) return;
  const btn = $("chatViewPhotoBtn");
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Открываем…";
  try {
    const res = await api(`/challenges/photo-url?path=${encodeURIComponent(currentChallenge.photo_id)}`);
    if (!res.view_url) throw new Error("Ссылка не получена");
    if (tg.openLink) tg.openLink(res.view_url);
    else window.open(res.view_url, "_blank");
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