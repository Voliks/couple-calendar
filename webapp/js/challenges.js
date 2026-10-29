let allChallenges = [];
let catalogData = {};
let currentChallenge = null;

async function openChallenges() {
  $("challengesOverlay").hidden = false;
  tg.BackButton?.show();
  $("challengesList").innerHTML = `<div class="spinner" style="margin: 20px auto;"></div>`;
  
  try {
    const res = await api("/challenges");
    allChallenges = res.history || [];
    catalogData = res.catalog || {};
    
    $("challengeBalance").textContent = res.balance;
    
    const catSelect = $("challengeCategorySelect");
    catSelect.innerHTML = Object.keys(catalogData).map(cat => 
      `<option value="${esc(cat)}">${esc(cat)}</option>`
    ).join("");
    
    updateIdeaSelect();
    renderChallenges();
  } catch (e) {
    toast(e.message);
  }
}

function renderChallenges() {
  const list = $("challengesList");
  if (!allChallenges.length) {
    list.innerHTML = `<div class="empty">Пока нет вызовов. Брось первый!</div>`;
    return;
  }

  list.innerHTML = allChallenges.map(c => {
    const isCreator = c.is_creator;
    let statusText = "";
    
    if (c.status === "pending") statusText = isCreator ? "⏳ Ждёт выполнения" : "⏳ Тебе бросили вызов";
    else if (c.status === "submitted") statusText = "🔍 На проверке";
    else if (c.status === "approved") statusText = "✅ Выполнено";
    else if (c.status === "rejected") statusText = "❌ Отклонено";

    return `<div class="challenge-card ${c.status === 'approved' ? 'done' : ''}" onclick="openChallengeChat(${c.id})">
      <div class="challenge-header">
        <span>${esc(c.category)}</span>
        <span>${statusText}</span>
      </div>
      <div class="challenge-text">${esc(c.text)}</div>
      <div class="challenge-reward">🏆 ${c.reward} баллов</div>
    </div>`;
  }).join("");
}

function updateIdeaSelect() {
  const cat = $("challengeCategorySelect").value;
  const ideas = catalogData[cat] || [];
  $("challengeIdeaSelect").innerHTML = ideas.map(idea => {
    const text = typeof idea === 'object' ? idea.challenge : idea;
    const points = typeof idea === 'object' ? idea.points : 10;
    return `<option value="${esc(text)}" data-points="${points}">${esc(text)} (${points} б.)</option>`;
  }).join("");
  updateRewardInput();
}

function updateRewardInput() {
  const sel = $("challengeIdeaSelect");
  const selectedOption = sel.options[sel.selectedIndex];
  if (selectedOption && selectedOption.dataset.points) {
    $("newChallengeReward").value = selectedOption.dataset.points;
  }
}

$("challengeIdeaSelect").onchange = () => updateRewardInput();

async function createChallenge() {
  const cat = $("challengeCategorySelect").value;
  const select = $("challengeIdeaSelect");
  const selectedOption = select.options[select.selectedIndex];
  
  if (!selectedOption || !selectedOption.value) return toast("Выберите задание");

  const text = selectedOption.value;
  const points = parseInt($("newChallengeReward").value) || parseInt(selectedOption.dataset.points) || 10;

  const btn = $("createChallengeBtn");
  btn.disabled = true;
  btn.textContent = "Отправка...";

  try {
    await api("/challenges", { method: "POST", body: { category: cat, text, reward: points } });
    toast("Вызов брошен партнёру!");
    await openChallenges();
  } catch (e) {
    toast(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Отправить партнёру";
  }
}

// === ОТКРЫТИЕ ЧАТА ЧЕЛЛЕНДЖА ===
function openChallengeChat(id) {
  const c = allChallenges.find(x => x.id === id);
  if (!c) return;
  currentChallenge = c;
  
  $("chatCategory").textContent = c.category;
  $("chatTaskText").textContent = c.text;
  $("chatReward").textContent = `🏆 ${c.reward} баллов`;
  
  // Сбрасываем видимость
  $("chatPhotoBlock").style.display = "none";
  $("chatActionBlock").style.display = "none";
  $("chatCreatorActions").style.display = "none";
  
  // Логика отображения кнопок
  const isCreator = c.is_creator;
  
  if (c.status === "pending") {
    $("chatStatus").textContent = isCreator ? "Ожидаем выполнения партнёром" : "Прикрепите фото для выполнения";
    if (!isCreator) {
      $("chatActionBlock").style.display = "block";
      $("chatAttachBtn").onclick = () => pickPhotoForChallenge(c.id);
    }
  } 
  else if (c.status === "submitted") {
    $("chatStatus").textContent = "Партнер выполнил задание. Ожидает вашей проверки.";
    if (c.photo_id) {
      $("chatPhotoBlock").style.display = "block";
      $("chatViewPhotoBtn").onclick = () => showChallengePhoto(c.photo_id);
    }
    if (isCreator) {
      $("chatCreatorActions").style.display = "flex";
      $("chatApproveBtn").onclick = () => approveChallenge(c.id);
      $("chatRejectBtn").onclick = () => rejectChallenge(c.id);
    }
  }
  else if (c.status === "approved") {
    $("chatStatus").textContent = "Челлендж успешно выполнен!";
    if (c.photo_id) {
      $("chatPhotoBlock").style.display = "block";
      $("chatViewPhotoBtn").onclick = () => showChallengePhoto(c.photo_id);
    }
  }
  else if (c.status === "rejected") {
    $("chatStatus").textContent = "Челлендж отклонен.";
    if (c.photo_id) {
      $("chatPhotoBlock").style.display = "block";
      $("chatViewPhotoBtn").onclick = () => showChallengePhoto(c.photo_id);
    }
  }
  
  $("challengeChatOverlay").hidden = false;
  $("challengesOverlay").hidden = true; // Скрываем список
  tg.BackButton?.show();
}

function closeChallengeChat() {
  $("challengeChatOverlay").hidden = true;
  $("challengesOverlay").hidden = false;
  tg.BackButton?.show();
}

$("chatBackBtn").onclick = closeChallengeChat;

// === ФОТО ===
async function compressImage(file) {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.readAsDataURL(file);
    reader.onload = (event) => {
      const img = new Image();
      img.src = event.target.result;
      img.onload = () => {
        const canvas = document.createElement("canvas");
        const MAX_WIDTH = 1024;
        const MAX_HEIGHT = 1024;
        let width = img.width;
        let height = img.height;

        if (width > height) {
          if (width > MAX_WIDTH) { height *= MAX_WIDTH / width; width = MAX_WIDTH; }
        } else {
          if (height > MAX_HEIGHT) { width *= MAX_HEIGHT / height; height = MAX_HEIGHT; }
        }
        canvas.width = width;
        canvas.height = height;
        const ctx = canvas.getContext("2d");
        ctx.drawImage(img, 0, 0, width, height);
        canvas.toBlob((blob) => resolve(blob), "image/jpeg", 0.7);
      };
    };
  });
}

function pickPhotoForChallenge(challengeId) {
  $("challengePhotoInput").onchange = async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    $("challengePhotoInput").value = "";

    toast("Сжимаем и загружаем фото...");
    
    try {
      const compressedBlob = await compressImage(file);
      
      const res = await api("/challenges/upload-url", {
        method: "POST",
        body: { challenge_id: challengeId, filename: "photo.jpg" }
      });
      
      const uploadRes = await fetch(res.upload_url, {
        method: "PUT",
        body: compressedBlob,
        headers: { "Content-Type": "image/jpeg" }
      });
      
      if (!uploadRes.ok) throw new Error("Ошибка загрузки в облако");

      await api(`/challenges/${challengeId}/photo`, {
        method: "POST",
        body: { path: res.path }
      });

      toast("✅ Фото загружено и отправлено на проверку!");
      closeChallengeChat();
      await openChallenges();
      
    } catch (err) {
      toast(err.message);
    }
  };
  $("challengePhotoInput").click();
}

async function showChallengePhoto(path) {
  try {
    const res = await api(`/challenges/photo-url?path=${encodeURIComponent(path)}`);
    window.open(res.view_url, "_blank");
  } catch (e) {
    toast("Не удалось открыть фото: " + e.message);
  }
}

async function submitChallenge(id) {
  try {
    await api(`/challenges/${id}/action`, { method: "POST", body: { action: "submit" } });
    toast("Отправлено на проверку!");
    closeChallengeChat();
    await openChallenges();
  } catch (e) { toast(e.message); }
}

async function approveChallenge(id) {
  try {
    await api(`/challenges/${id}/action`, { method: "POST", body: { action: "approve" } });
    toast("Баллы начислены!");
    closeChallengeChat();
    await openChallenges();
  } catch (e) { toast(e.message); }
}

async function rejectChallenge(id) {
  try {
    await api(`/challenges/${id}/action`, { method: "POST", body: { action: "reject" } });
    toast("Челлендж отклонен");
    closeChallengeChat();
    await openChallenges();
  } catch (e) { toast(e.message); }
}

$("challengeBtn").onclick = openChallenges;
$("challengesClose").onclick = () => { $("challengesOverlay").hidden = true; tg.BackButton?.hide(); };
$("challengeCategorySelect").onchange = updateIdeaSelect;
$("createChallengeBtn").onclick = createChallenge;