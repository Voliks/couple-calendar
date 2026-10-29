let allChallenges = [];
let catalogData = {};

// Открытие окна со списком
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

// Отрисовка карточек
function renderChallenges() {
  const list = $("challengesList");
  if (!allChallenges.length) {
    list.innerHTML = `<div class="empty">Пока нет вызовов. Брось первый!</div>`;
    return;
  }

  list.innerHTML = allChallenges.map(c => {
    const isCreator = c.is_creator;
    let statusText = "";
    
    if (c.status === "pending") statusText = isCreator ? "⏳ Ждёт" : "⏳ Тебе бросили";
    else if (c.status === "submitted") statusText = "🔍 На проверке";
    else if (c.status === "approved") statusText = "✅ Выполнено";
    else if (c.status === "rejected") statusText = "❌ Отклонено";

    return `<div class="challenge-card ${c.status === 'approved' ? 'done' : ''}" onclick="openChallengeDetails(${c.id})">
      <div class="challenge-header">
        <span>${esc(c.category)}</span>
        <span>${statusText}</span>
      </div>
      <div class="challenge-text">${esc(c.text)}</div>
      <div class="challenge-reward">🏆 ${c.reward} баллов</div>
    </div>`;
  }).join("");
}

// Открытие деталей челленджа (просто скроллим к нему или показываем статус)
// Пока что оставим просто открытие окна с заданием
function openChallengeDetails(id) {
  const c = allChallenges.find(x => x.id === id);
  if (!c) return;
  
  // Просто показываем детали (для теста)
  alert(`Задание: ${c.text}\nБаллы: ${c.reward}\nСтатус: ${c.status}`);
}

// Обновление селектов
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

// Создание челленджа
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
    toast("Вызов брошен!");
    await openChallenges();
  } catch (e) {
    toast(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Отправить партнёру";
  }
}

// Закрытие
function closeChallenges() {
  $("challengesOverlay").hidden = true;
  tg.BackButton?.hide();
}

// Привязка событий
document.addEventListener("DOMContentLoaded", () => {
  const btn = $("challengeBtn");
  if (btn) btn.onclick = openChallenges;
  
  const closeBtn = $("challengesClose");
  if (closeBtn) closeBtn.onclick = closeChallenges;
  
  const catSelect = $("challengeCategorySelect");
  if (catSelect) catSelect.onchange = updateIdeaSelect;
  
  const createBtn = $("createChallengeBtn");
  if (createBtn) createBtn.onclick = createChallenge;
});