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
    updateIdeaSelect();

    renderChallenges();
  } catch (e) {
    $("challengesList").innerHTML = `<div class="empty">Ошибка загрузки: ${e.message}</div>`;
  }
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
  const text = $("challengeIdeaSelect").value;
  const reward = parseInt($("newChallengeReward").value) || 10;

  if (!text) {
    toast("Выберите задание");
    return;
  }

  const btn = $("createChallengeBtn");
  btn.disabled = true;
  btn.textContent = "Отправка...";

  try {
    await api("/challenges", { method: "POST", body: { category: cat, text, reward } });
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