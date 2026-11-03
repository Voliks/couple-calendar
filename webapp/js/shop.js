let selectedShopItem = null;
let currentBalance = 0;

function openShop() {
  $("shopOverlay").hidden = false;
  tg.BackButton?.show();
  $("shopList").innerHTML = `<div class="spinner" style="margin:20px auto;"></div>`;

  try {
    currentBalance = state.balance || 0;
    $("shopBalance").textContent = currentBalance;

    api("/shop").then(res => {
      let items = res.items || [];
      
      // Сортируем по возрастанию цены
      items.sort((a, b) => a.cost - b.cost);

      if (!items.length) {
        $("shopList").innerHTML = `<div class="empty">Магазин пока пуст.</div>`;
        return;
      }

      $("shopList").innerHTML = items.map(item => {
        const afford = currentBalance >= item.cost;
        
        return `<div class="card accepted" style="margin-bottom: 8px; display: flex; justify-content: space-between; align-items: center; padding: 12px; border-left: 4px solid #8b5cf6; cursor: pointer;" data-item-id="${item.id}" data-item-title="${esc(item.title)}" data-item-cost="${item.cost}" data-item-emoji="${esc(item.emoji)}">
          <div style="font-size: 16px; font-weight: 600; word-break: break-word; margin-right: 10px;">${esc(item.emoji)} ${esc(item.title)}</div>
          <div style="font-size: 16px; font-weight: bold; white-space: nowrap; color: ${afford ? '#eab308' : 'var(--declined)'};">🏆 ${item.cost}</div>
        </div>`;
      }).join("");
    });
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
  $("buyDateInput").value = new Date().toISOString().split('T')[0];
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