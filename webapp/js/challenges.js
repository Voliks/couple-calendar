let allChallenges = [];
let catalogData = {};

async function openChallenges() {
  $("challengesOverlay").hidden = false;
  tg.BackButton?.show();
  $("challengesList").innerHTML = `<div class="spinner" style="margin: 20px auto;"></div>`;
  
  try {
    const res = await api("/challenges");
    allChallenges = res.history || [];
    catalogData = res.catalog || {};
    
    // Обновляем баланс
    $("challengeBalance").textContent = res.balance;
    
    // Заполняем категории в форме
    const catSelect = $("challengeCategorySelect");
    catSelect.innerHTML = Object.keys(catalogData).map(cat => 
      `<option value="${esc(cat)}">${esc(cat)}</option>`
    ).join("");
    
    // Заполняем список идей для первой категории
function updateIdeaSelect() {
  const cat = $("challengeCategorySelect").value;
  const ideas = catalogData[cat] || [];
  
  const select = $("challengeIdeaSelect");
  
  if (!ideas.length) {
    select.innerHTML = `<option value="">Нет заданий в этой категории</option>`;
    $("newChallengeReward").value = 0;
    return;
  }

  select.innerHTML = ideas.map(idea => {
    // Проверяем: если это объект (новый формат), берем .challenge и .points. Если строка — берем как есть.
    const text = typeof idea === 'object' ? idea.challenge : idea;
    const points = typeof idea === 'object' ? idea.points : 10;
    return `<option value="${esc(text)}" data-points="${points}">${esc(text)} (${points} б.)</option>`;
  }).join("");
  
  // Автоматически подставляем очки для первого задания
  updateRewardInput();
}

// Эту функцию тоже нужно добавить, если её нет
function updateRewardInput() {
  const sel = $("challengeIdeaSelect");
  const selectedOption = sel.options[sel.selectedIndex];
  if (selectedOption && selectedOption.dataset.points) {
    $("newChallengeReward").value = selectedOption.dataset.points;
  }
}

// Добавь привязку события, чтобы при смене задания очки менялись
$("challengeIdeaSelect").onchange = () => {
  updateRewardInput();
};
  
  // Автоматически обновляем поле с очками при выборе задания
  updateRewardInput();
}

function updateRewardInput() {
  const sel = $("challengeIdeaSelect");
  const selectedOption = sel.options[sel.selectedIndex];
  if (selectedOption && selectedOption.dataset.points) {
    $("newChallengeReward").value = selectedOption.dataset.points;
  }
}

// Добавь вызов этой функции при изменении селекта
$("challengeIdeaSelect").onchange = () => {
  updateRewardInput();
};
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
    const isTarget = !isCreator;
    
    let statusText = "";
    let actions = "";
    
    // Блок с фото
    let photoHtml = "";
    if (c.photo_id) {
      photoHtml = `<button class="btn ghost" style="margin-top:8px; font-size:12px;" onclick="showChallengePhoto('${c.photo_id}')">🖼 Посмотреть фото</button>`;
    }

    if (c.status === "pending") {
      statusText = isCreator ? "⏳ Ждёт выполнения" : "⏳ Тебе бросили вызов";
      if (isTarget) {
        actions = `<div class="challenge-actions">
          <button class="primary" onclick="pickPhotoForChallenge(${c.id})">📸 Прикрепить фото</button>
        </div>`;
      }
    } else if (c.status === "submitted") {
      statusText = "🔍 На проверке";
      if (isCreator) {
        actions = `<div class="challenge-actions">
          <button class="success" onclick="approveChallenge(${c.id})">Засчитать</button>
          <button class="danger" onclick="rejectChallenge(${c.id})">Отклонить</button>
        </div>`;
      }
    } else if (c.status === "approved") {
      statusText = "✅ Выполнено";
    } else if (c.status === "rejected") {
      statusText = "❌ Отклонено";
    }

    return `<div class="challenge-card ${c.status === 'approved' ? 'done' : ''}">
      <div class="challenge-header">
        <span>${esc(c.category)}</span>
        <span>${statusText}</span>
      </div>
      <div class="challenge-text">${esc(c.text)}</div>
      <div class="challenge-reward">🏆 ${c.reward} баллов</div>
      ${photoHtml}
      ${actions}
    </div>`;
  }).join("");
}

function updateIdeaSelect() {
  const cat = $("challengeCategorySelect").value;
  const ideas = catalogData[cat] || [];
  $("challengeIdeaSelect").innerHTML = ideas.map(idea => 
    `<option value="${esc(idea)}">${esc(idea)}</option>`
  ).join("");
}

function closeChallenges() {
  $("challengesOverlay").hidden = true;
  if (!$("overlay").hidden) tg.BackButton?.show(); else tg.BackButton?.hide();
}

function renderChallenges() {
  const list = $("challengesList");
  if (!allChallenges.length) {
    list.innerHTML = `<div class="empty">Пока нет вызовов. Брось первый!</div>`;
    return;
  }

  list.innerHTML = allChallenges.map(c => {
    const isCreator = c.is_creator;
    const isTarget = !isCreator;
    
    let statusText = "";
    let actions = "";
    
    if (c.status === "pending") {
      statusText = isCreator ? "⏳ Ждёт выполнения" : "⏳ Тебе бросили вызов";
      if (isTarget) {
        actions = `<div class="challenge-actions">
          <button class="primary" onclick="submitChallenge(${c.id})">Сдать</button>
        </div>`;
      }
    } else if (c.status === "submitted") {
      statusText = "🔍 На проверке";
      if (isCreator) {
        actions = `<div class="challenge-actions">
          <button class="success" onclick="approveChallenge(${c.id})">Засчитать</button>
          <button class="danger" onclick="rejectChallenge(${c.id})">Отклонить</button>
        </div>`;
      }
    } else if (c.status === "approved") {
      statusText = "✅ Выполнено";
    } else if (c.status === "rejected") {
      statusText = "❌ Отклонено";
    }

    return `<div class="challenge-card ${c.status === 'approved' ? 'done' : ''}">
      <div class="challenge-header">
        <span>${esc(c.category)}</span>
        <span>${statusText}</span>
      </div>
      <div class="challenge-text">${esc(c.text)}</div>
      <div class="challenge-reward">🏆 ${c.reward} баллов</div>
      ${actions}
    </div>`;
  }).join("");
}

async function createChallenge() {
  const cat = $("challengeCategorySelect").value;
  const select = $("challengeIdeaSelect");
  const selectedOption = select.options[select.selectedIndex];
  
  if (!selectedOption || !selectedOption.value) {
    toast("Выберите задание");
    return;
  }

  const text = selectedOption.value;
  const points = parseInt($("newChallengeReward").value) || parseInt(selectedOption.dataset.points) || 10;

  const btn = $("createChallengeBtn");
  btn.disabled = true;
  btn.textContent = "Отправка...";

  try {
    await api("/challenges", { method: "POST", body: { category: cat, text, reward: points } });
    toast("Вызов брошен партнёру!");
    await openChallenges(); // перезагружаем
  } catch (e) {
    toast(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Отправить партнёру";
  }
}

async function submitChallenge(id) {
  try {
    await api(`/challenges/${id}/action`, { method: "POST", body: { action: "submit" } });
    toast("Отправлено на проверку!");
    await openChallenges();
  } catch (e) { toast(e.message); }
}

async function approveChallenge(id) {
  try {
    await api(`/challenges/${id}/action`, { method: "POST", body: { action: "approve" } });
    toast("Баллы начислены!");
    await openChallenges();
  } catch (e) { toast(e.message); }
}

async function rejectChallenge(id) {
  try {
    await api(`/challenges/${id}/action`, { method: "POST", body: { action: "reject" } });
    toast("Челлендж отклонен");
    await openChallenges();
  } catch (e) { toast(e.message); }
}

// Привязка событий
$("challengeBtn").onclick = openChallenges;
$("challengesClose").onclick = closeChallenges;
$("challengeCategorySelect").onchange = updateIdeaSelect;
$("createChallengeBtn").onclick = createChallenge;

// Функция для сжатия фото в браузере (чтобы не грузить 5 МБ)
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
        canvas.toBlob((blob) => resolve(blob), "image/jpeg", 0.7); // Сжимаем на 70%
      };
    };
  });
}

// Открываем галерею при клике на кнопку "Прикрепить фото"
function pickPhotoForChallenge(challengeId) {
  $("challengePhotoInput").onchange = async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    $("challengePhotoInput").value = "";

    toast("Сжимаем и загружаем фото...");
    
    try {
      // 1. Сжимаем
      const compressedBlob = await compressImage(file);
      
      // 2. Получаем ссылку на загрузку у нашего сервера
      const res = await api("/challenges/upload-url", {
        method: "POST",
        body: { challenge_id: challengeId, filename: "photo.jpg" }
      });
      
      // 3. Грузим фото НАПРЯМУЮ в Supabase
      const uploadRes = await fetch(res.upload_url, {
        method: "PUT",
        body: compressedBlob,
        headers: { "Content-Type": "image/jpeg" }
      });
      
      if (!uploadRes.ok) throw new Error("Ошибка загрузки в облако");

      // 4. Сохраняем путь в БД (через наш API)
      await api(`/challenges/${challengeId}/photo`, {
        method: "POST",
        body: { path: res.path }
      });

      toast("✅ Фото загружено и отправлено на проверку!");
      await openChallenges();
      
    } catch (err) {
      toast(err.message);
    }
  };
  $("challengePhotoInput").click();
}

// Функция для получения ссылки на просмотр фото
async function showChallengePhoto(path) {
  try {
    const res = await api(`/challenges/photo-url?path=${encodeURIComponent(path)}`);
    // Открываем фото в новой вкладке/просмотрщике
    window.open(res.view_url, "_blank");
  } catch (e) {
    toast("Не удалось открыть фото: " + e.message);
  }
}