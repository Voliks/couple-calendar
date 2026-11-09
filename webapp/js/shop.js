let selectedShopItem = null;
let currentBalance = 0;
let shopItems = null; // каталог статичен: грузим один раз за сессию

async function openShop() {
  $("shopOverlay").hidden = false;
  tg.BackButton?.show();
  if (!shopItems) $("shopList").innerHTML = `<div class="spinner" style="margin:20px auto;"></div>`;

  currentBalance = state.balance || 0;
  $("shopBalance").textContent = currentBalance;

  try {
    if (!shopItems) {
      const res = await api("/shop");
      shopItems = (res.items || []).slice().sort((a, b) => a.cost - b.cost);
    }
    const items = shopItems;

    if (!items.length) {
      $("shopList").innerHTML = `<div class="empty">Магазин пока пуст.</div>`;
      return;
    }

    $("shopList").innerHTML = items.map(item => {
      const afford = currentBalance >= item.cost;
      return `<div class="shop-item" data-item-id="${item.id}" data-item-title="${esc(item.title)}" data-item-cost="${item.cost}">
        <div class="shop-title">${esc(item.emoji)} ${esc(item.title)}</div>
        <div class="shop-cost${afford ? "" : " poor"}">🏆 ${item.cost}</div>
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