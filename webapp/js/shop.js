let selectedShopItem = null;
let currentBalance = 0;

async function openShop() {
  $("shopOverlay").hidden = false;
  tg.BackButton?.show();
  $("shopList").innerHTML = `<div class="spinner" style="margin:20px auto;"></div>`;

  currentBalance = state.balance || 0;
  $("shopBalance").textContent = currentBalance;

  try {
    const res = await api("/shop");
    const items = (res.items || []).slice().sort((a, b) => a.cost - b.cost);

    if (!items.length) {
      $("shopList").innerHTML = `<div class="empty">Магазин пока пуст.</div>`;
      return;
    }

    $("shopList").innerHTML = items.map(item => {
      const afford = currentBalance >= item.cost;
      
      // Дизайн карточки подстроен под челленджи:
      // padding 10px 12px, border-left 3px (фиолетовый), цвета и шрифты такие же
      return `<div class="challenge-card" style="padding: 10px 12px; margin-bottom: 0; border-radius: 12px; border-left: 3px solid #8b5cf6; display: flex; justify-content: space-between; align-items: center; cursor: pointer; gap: 10px;" data-item-id="${item.id}" data-item-title="${esc(item.title)}" data-item-cost="${item.cost}" data-item-emoji="${esc(item.emoji)}">
        
        <div style="flex: 1; display: flex; flex-direction: column; gap: 4px;">
          <div style="font-size: 14px; font-weight: 600; line-height: 1.3; word-break: break-word;">${esc(item.emoji)} ${esc(item.title)}</div>
        </div>
        
        <div style="font-size: 13px; font-weight: bold; white-space: nowrap; color: ${afford ? '#eab308' : 'var(--hint)'};">
          🏆 ${item.cost}
        </div>
        
      </div>`;
    }).join("");
  } catch (e) {
    $("shopList").innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    toast(e.message);
  }
}

function closeShop() {
  $("shopOverlay").hidden = true;
  if (!$("overlay").hidden || !$("buyDateOverlay").hidden) tg.BackButton?.show();
  else tg.BackButton?.hide();
}

function openBuyDatePicker(title, itemId) {
  selectedShopItem = itemId;
  $("buyTitle").textContent = `Купить «${title}»?`;
  $("buyDateInput").value = todayStr();
  $("buyDateOverlay").hidden = false;
}

function closeBuyDatePicker() {
  $("buyDateOverlay").hidden = true;
  selectedShopItem = null;
  if (!$("shopOverlay").hidden) tg.BackButton?.show();
  else tg.BackButton?.hide();
}

async function confirmBuy() {
  const date = $("buyDateInput").value;
  if (!date) return toast("Выберите дату");

  const btn = $("buyConfirm");
  btn.disabled = true;
  btn.textContent = "Покупка...";

  try {
    await api("/shop/buy", { 
      method: "POST", 
      body: { item_id: selectedShopItem, date } 
    });
    toast("Желание куплено!");
    tg.HapticFeedback?.notificationOccurred("success");
    
    closeBuyDatePicker();
    closeShop();
    
    etag = null;
    await load();
  } catch (e) {
    toast(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Купить";
  }
}

// Привязка событий
(() => {
  const shopBtn = $("shopBtn");
  if (shopBtn) shopBtn.onclick = openShop;
  
  const closeBtn = $("shopClose");
  if (closeBtn) closeBtn.onclick = closeShop;
  
  const cancelBtn = $("buyCancel");
  if (cancelBtn) cancelBtn.onclick = closeBuyDatePicker;
  
  const confirmBtn = $("buyConfirm");
  if (confirmBtn) confirmBtn.onclick = confirmBuy;

  // Закрытие по клику на фон магазина
  $("shopOverlay").addEventListener("click", (e) => {
    if (e.target === $("shopOverlay")) closeShop();
  });

  // Закрытие по клику на фон выбора даты
  $("buyDateOverlay").addEventListener("click", (e) => {
    if (e.target === $("buyDateOverlay")) closeBuyDatePicker();
  });

  $("shopList").addEventListener("click", (e) => {
    const card = e.target.closest("[data-item-id]");
    if (!card) return;
    
    const id = Number(card.dataset.itemId);
    const title = card.dataset.itemTitle;
    const cost = Number(card.dataset.itemCost);
    
    if (currentBalance < cost) {
      return toast("Недостаточно баллов для покупки");
    }

    openBuyDatePicker(title, id);
  });
})();