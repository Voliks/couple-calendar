let formChecklist = [];
let formColor = "";
let formDateEnd = "";
const todoColorsFallback = ["#ef4444","#f97316","#eab308","#22c55e","#14b8a6","#3b82f6","#8b5cf6","#ec4899"];

function availableTodoColors() {
  return (state.todo_colors && state.todo_colors.length) ? state.todo_colors : todoColorsFallback;
}

function updateRemindAvailability() {
  const cat = $("fCategory").value;
  const hasTime = !!$("fTime").value;
  const isTodo = cat === "todo";
  const isPersonal = cat === "personal";
  const sel = $("fRemindBefore");
  if (sel) {
    sel.disabled = !isTodo || !hasTime;
    if (!hasTime) sel.value = "0";
  }
  const selP = $("fPersonalRemind");
  if (selP) {
    selP.disabled = !isPersonal || !hasTime;
    if (!hasTime) selP.value = "0";
  }
  const hint = $("remindHint");
  if (hint) hint.hidden = !isTodo || hasTime;
  const hintP = $("personalRemindHint");
  if (hintP) hintP.hidden = !isPersonal || hasTime;
}

function updateFormSections() {
  const cat = $("fCategory").value;
  const isShop = cat === "shop";
  const isSex = cat === "sex";
  const isTodo = cat === "todo";
  const isPersonal = cat === "personal";
  const isRest = cat === "rest";
  const isRange = !!formDateEnd;

  $("checklistSection").hidden = !(isShop && !editingEvent);
  $("sexIdeasSection").hidden = !isSex;
  $("todoSection").hidden = !isTodo;
  $("personalSection").hidden = !isPersonal;
  $("restSection").hidden = !(isRest && isRange);

  if (isShop && !editingEvent) renderFormChecklist();
  if (isTodo) renderTodoSection();
  if (isPersonal) renderPersonalSection();
  if (isRest && isRange) renderRestSection();
  updateRemindAvailability();

  if (!isSex) {
    selectedIdeas = [];
    currentCategoryTitle = "";
    renderChosen();
  }
}

function renderTodoSection() {
  const box = $("todoColors");
  if (!box) return;
  const colors = availableTodoColors();
  box.innerHTML = colors.map(c =>
    `<button type="button" class="todo-color${c === formColor ? " selected" : ""}"
             data-color="${c}" style="background:${c}" aria-label="Цвет ${c}"></button>`
  ).join("");
}

function renderPersonalSection() {
  const box = $("personalColors");
  if (!box) return;
  const colors = availableTodoColors();
  box.innerHTML = colors.map(c =>
    `<button type="button" class="todo-color${c === formColor ? " selected" : ""}"
             data-color="${c}" style="background:${c}" aria-label="Цвет ${c}"></button>`
  ).join("");
}

function renderRestSection() {
  const box = $("restColors");
  if (!box) return;
  const colors = availableTodoColors();
  box.innerHTML = colors.map(c =>
    `<button type="button" class="todo-color${c === formColor ? " selected" : ""}"
             data-color="${c}" style="background:${c}" aria-label="Цвет ${c}"></button>`
  ).join("");
}

$("todoColors").addEventListener("click", (e) => {
  const b = e.target.closest("[data-color]");
  if (!b) return;
  formColor = b.dataset.color;
  renderTodoSection();
  updateMultiColorPreview();
  tg.HapticFeedback?.selectionChanged();
});

$("personalColors").addEventListener("click", (e) => {
  const b = e.target.closest("[data-color]");
  if (!b) return;
  formColor = b.dataset.color;
  renderPersonalSection();
  updateMultiColorPreview();
  tg.HapticFeedback?.selectionChanged();
});

$("restColors").addEventListener("click", (e) => {
  const b = e.target.closest("[data-color]");
  if (!b) return;
  formColor = b.dataset.color;
  renderRestSection();
  updateMultiColorPreview();
  tg.HapticFeedback?.selectionChanged();
});

function renderFormChecklist() {
  const box = $("fChecklist");
  if (!box) return;
  if (!formChecklist.length) {
    box.innerHTML = `<div class="cl-empty">Список пока пуст</div>`;
    return;
  }
  box.innerHTML = formChecklist.map((it, idx) => `
    <div class="checklist-item${it.checked ? " checked" : ""}" data-idx="${idx}">
      <input type="checkbox" ${it.checked ? "checked" : ""} data-form-cl-toggle aria-label="${esc(it.text)}">
      <span class="cl-text">${esc(it.text)}</span>
      <button type="button" class="cl-remove" data-form-cl-remove aria-label="Удалить пункт «${esc(it.text)}»">×</button>
    </div>`).join("");
}

function openForm(event = null) {
  editingEvent = event;
  $("sheetTitle").textContent = event ? "Редактировать событие" : "Новое событие";
  $("fDate").value = event ? event.date : selected;
  $("fTime").value = event ? event.time : "";
  $("fCategory").value = event ? event.category : "rest";
  $("fDesc").value = event ? event.description || "" : "";

  selectedIdeas = [];
  currentCategoryTitle = "";
  renderChosen();

  if (event && (event.category === "todo" || event.category === "personal")) {
    formColor = event.color || availableTodoColors()[6];
    if (event.category === "todo") {
      $("fRemindBefore").value = String(event.remind_before || 0);
    } else {
      $("fPersonalRemind").value = String(event.remind_before || 0);
      $("fPersonalDateEnd").value = event.date_end && event.date_end !== event.date ? event.date_end : "";
      $("fPersonalPublic").checked = event.visibility === "shared";
    }
  } else if (event && event.category === "rest" && event.date_end && event.date_end !== event.date) {
    formColor = event.color || availableTodoColors()[3];
    $("fRemindBefore").value = "0";
    $("fPersonalRemind").value = "0";
    $("fPersonalDateEnd").value = "";
    $("fPersonalPublic").checked = false;
  } else {
    formColor = availableTodoColors()[6];
    $("fRemindBefore").value = "0";
    $("fPersonalRemind").value = "0";
    $("fPersonalDateEnd").value = "";
    $("fPersonalPublic").checked = false;
  }

  formDateEnd = event ? (event.date_end && event.date_end !== event.date ? event.date_end : "") : "";
  $("fHint").hidden = !event || (event && (event.category === "shop" || event.category === "todo" || event.category === "personal"));
  $("fError").textContent = "";
  formChecklist = [];
  updateMultiColorPreview();
  updateFormSections();
  $("overlay").hidden = false;
  tg.BackButton?.show();
}

function openFormForRange(dates, category) {
  if (!dates.length) return;
  const sorted = [...dates].sort();
  const start = sorted[0];
  const end = sorted[sorted.length - 1];

  if ((category === "sex" || category === "shop") && end !== start) {
    toast("Для этой категории диапазон не поддерживается — выберите один день");
    clearMultiSelect();
    return;
  }

  openForm();
  $("fCategory").value = category;
  $("fDate").value = start;
  formDateEnd = end !== start ? end : "";
  if (category === "personal") {
    $("fPersonalDateEnd").value = formDateEnd;
  }
  if (category === "rest" && formDateEnd) {
    if (!formColor || formColor === availableTodoColors()[6]) {
      formColor = availableTodoColors()[3];
    }
  }
  updateFormSections();

  const catLabel = CATEGORIES[category]?.label || "";
  const rangeLabel = formDateEnd
    ? `${fmtRange(start, formDateEnd)} · ${sorted.length} ${pluralDays(sorted.length)}`
    : DAY_FORMAT.format(parse(start));
  $("sheetTitle").textContent = `Новое · ${catLabel} · ${rangeLabel}`;
}

function closeForm() {
  $("overlay").hidden = true;
  tg.BackButton?.hide();
  formDateEnd = "";
  clearMultiSelect();
}

$("add").onclick = () => openForm();
$("fCancel").onclick = closeForm;
$("overlay").addEventListener("click", (e) => { if (e.target === $("overlay")) closeForm(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("overlay").hidden) closeForm(); });
try { tg.BackButton?.onClick(() => {
  if (!$("catOverlay").hidden) { closeCategoryPicker(); clearMultiSelect(); return; }
  if (!$("overlay").hidden) closeForm();
}); } catch (e) {}

$("fCategory").addEventListener("change", () => {
  if ($("fCategory").value !== "shop") formChecklist = [];
  updateFormSections();
});
$("fTime").addEventListener("change", updateRemindAvailability);
$("fTime").addEventListener("input", updateRemindAvailability);

$("fClAdd").onclick = () => {
  const input = $("fClInput");
  const text = input.value.trim();
  if (!text || formChecklist.length >= 50) return;
  formChecklist.push({ text, checked: false });
  input.value = "";
  renderFormChecklist();
  input.focus();
};
$("fClInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); $("fClAdd").click(); }
});
$("fChecklist").addEventListener("click", (ev) => {
  const idxEl = ev.target.closest("[data-idx]");
  if (!idxEl) return;
  const idx = Number(idxEl.dataset.idx);

  if (ev.target.closest("[data-form-cl-remove]")) {
    formChecklist.splice(idx, 1);
    renderFormChecklist();
    return;
  }
  const toggle = ev.target.closest("[data-form-cl-toggle]");
  if (toggle) {
    formChecklist[idx].checked = toggle.checked;
    renderFormChecklist();
  }
});

$("fSave").onclick = async () => {
  const category = $("fCategory").value;
  const date = $("fDate").value;
  if (!date) { $("fError").textContent = "Выберите дату"; return; }

  const notes = $("fDesc").value.trim();
  const ideasPart = category === "sex" && selectedIdeas.length
    ? selectedIdeas.map(t => "• " + t).join("\n")
    : "";
  const description = [ideasPart, notes].filter(Boolean).join("\n\n");

  const payload = {
    category,
    description,
    date,
    time: $("fTime").value,
  };

  if (category === "todo") {
    if (!formColor) { $("fError").textContent = "Выберите цвет"; return; }
    payload.color = formColor;
    const rb = parseInt($("fRemindBefore").value, 10) || 0;
    payload.remind_before = ($("fTime").value ? rb : 0);
    payload.visibility = "shared";
    payload.date_end = formDateEnd || "";
  }

  if (category === "personal") {
    if (!formColor) { $("fError").textContent = "Выберите цвет"; return; }
    payload.color = formColor;
    const rb = parseInt($("fPersonalRemind").value, 10) || 0;
    payload.remind_before = ($("fTime").value ? rb : 0);
    payload.visibility = $("fPersonalPublic").checked ? "shared" : "private";

    const de = $("fPersonalDateEnd").value || "";
    if (de) {
      if (de < date) { $("fError").textContent = "Дата окончания раньше даты начала"; return; }
      payload.date_end = de;
    } else {
      payload.date_end = "";
    }
  }

  if (category === "rest") {
    payload.date_end = formDateEnd || "";
    if (formDateEnd) {
      if (!formColor) { $("fError").textContent = "Выберите цвет заливки"; return; }
      payload.color = formColor;
    }
  }

  if (!editingEvent && category === "shop") payload.checklist = formChecklist;

  const btn = $("fSave");
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Сохраняем…";
  try {
    if (editingEvent) {
      await api(`/events/${editingEvent.id}`, { method: "PUT", body: payload });
      toast("Событие изменено");
    } else {
      await api("/events", { method: "POST", body: payload });
    }
    tg.HapticFeedback?.notificationOccurred("success");
    selected = date;
    const d = parse(date);
    view = { y: d.getFullYear(), m: d.getMonth() };
    closeForm();
    etag = null;
    await load();
  } catch (e) {
    $("fError").textContent = e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
};