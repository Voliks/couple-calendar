let selectedShopItem = null;
let currentBalance = 0;

function openShop() {
  $("shopOverlay").hidden = false;
  tg.BackButton?.show();
  $("shopList").innerHTML = `<div class="spinner" style="margin:20px auto;"></div>`;

  try {
    // Подтягиваем баланс из state (он обновляется через load())
    currentBalance = state.balance || 0;
    $("shopBalance").textContent = currentBalance;

    // Запрашиваем список товаров
    api("/shop").then(res => {
      let items = res.items || [];
      
      // Сортируем по возрастанию цены (от дешевых к дорогим)
      items.sort((a, b) => a.cost - b.cost);

      if (!items.length) {
        $("shopList").innerHTML = `<div class="empty">Магазин пока пуст.</div>`;
        return;
      }

      $("shopList").innerHTML = items.map(item => {
        const afford = currentBalance >= item.cost;
        return `<div class="challenge-card" style="border-left-color: #8b5cf6; margin-bottom: 8px;" data-item-id="${item.id}" data-item-title="${esc(item.title)}" data-item-cost="${item.cost}" data-item-emoji="${esc(item.emoji)}">
          <div class="challenge-header" style="align-items: center;">
            <span>${esc(item.emoji)} ${esc(item.title)}</span>
            <span style="color: ${afford ? '#eab308' : 'var(--declined)'}; font-weight: bold;">🏆 ${item.cost}</span>
          </div>
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
    
    // Обновляем календарь
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