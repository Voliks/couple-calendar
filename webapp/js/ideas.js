let selectedIdeas = [];
let currentCategoryTitle = "";

function openIdeasModal(title) {
  currentCategoryTitle = title;
  $("ideasModalTitle").textContent = title;
  hideIdeaForm();
  renderIdeas();
  $("ideasOverlay").hidden = false;
  document.body.style.overflow = "hidden";
  tg.BackButton?.show();
}

function closeIdeasModal() {
  $("ideasOverlay").hidden = true;
  document.body.style.overflow = "";
  hideIdeaForm();
  if (!$("overlay").hidden) tg.BackButton?.show(); else tg.BackButton?.hide();
}

function showIdeaForm() {
  $("ideaForm").hidden = false;
  $("addIdeaToggle").hidden = true;
  const inp = $("newIdeaInput");
  inp.value = "";
  setTimeout(() => inp.focus(), 50);
}

function hideIdeaForm() {
  $("ideaForm").hidden = true;
  $("addIdeaToggle").hidden = false;
  $("newIdeaInput").value = "";
}

function renderIdeas() {
  const listEl = $("ideasContainer");
  const filtered = (state.sex_ideas || []).filter(i => i.category_title === currentCategoryTitle);
  if (!filtered.length) {
    listEl.innerHTML = `<div class="cl-empty">Пока нет идей. Добавьте первую!</div>`;
    return;
  }
  listEl.innerHTML = filtered.map(i => {
    const on = selectedIdeas.includes(i.text);
    return `<div class="idea-item${on ? " selected" : ""}" data-idea-text="${esc(i.text)}" data-idea-id="${esc(i.id)}">
      <span class="idea-check">✓</span>
      <span class="idea-text">${esc(i.text)}</span>
      <button type="button" class="idea-del" data-idea-del aria-label="Удалить">×</button>
    </div>`;
  }).join("");
}

function toggleIdea(text) {
  const i = selectedIdeas.indexOf(text);
  if (i >= 0) selectedIdeas.splice(i, 1);
  else selectedIdeas.push(text);
  renderIdeas();
  renderChosen();
  tg.HapticFeedback?.selectionChanged();
}

function renderChosen() {
  const block = $("chosenBlock");
  const chips = $("chosenChips");
  const count = $("chosenCount");
  if (!block || !chips || !count) return;
  if (!selectedIdeas.length) {
    block.hidden = true; chips.innerHTML = ""; count.textContent = "0";
    updateTriggersCount();
    return;
  }
  block.hidden = false;
  count.textContent = String(selectedIdeas.length);
  chips.innerHTML = selectedIdeas.map((t, idx) =>
    `<span class="chip" data-idx="${idx}">
       <span class="chip-text">${esc(t)}</span>
       <button type="button" class="chip-x" data-chip-x aria-label="Убрать">×</button>
     </span>`
  ).join("");
  updateTriggersCount();
}

function updateTriggersCount() {
  document.querySelectorAll(".sex-idea-trigger").forEach(btn => {
    const title = btn.dataset.title;
    const n = selectedIdeas.filter(t => {
      const idea = (state.sex_ideas || []).find(i => i.text === t);
      return idea && idea.category_title === title;
    }).length;
    const badge = btn.querySelector("[data-count]");
    if (!badge) return;
    if (n > 0) { badge.textContent = n; badge.hidden = false; }
    else { badge.textContent = ""; badge.hidden = true; }
  });
}

document.querySelectorAll(".sex-idea-trigger").forEach((btn) => {
  btn.onclick = () => {
    openIdeasModal(btn.dataset.title);
    tg.HapticFeedback?.selectionChanged();
  };
});

$("ideasContainer").addEventListener("click", async (e) => {
  const del = e.target.closest("[data-idea-del]");
  if (del) {
    const item = del.closest("[data-idea-text]");
    const text = item.dataset.ideaText;
    const idea = (state.sex_ideas || []).find(i => i.text === text);
    if (!idea) return;
    confirmDialog("Удалить эту идею? Она пропадёт у вас обоих.", async () => {
      try {
        const res = await api(`/sex-ideas/${idea.id}`, { method: "DELETE" });
        state.sex_ideas = res.ideas;
        selectedIdeas = selectedIdeas.filter(t => t !== text);
        renderIdeas();
        renderChosen();
        tg.HapticFeedback?.notificationOccurred("success");
      } catch (err) {
        toast(err.message);
      }
    });
    return;
  }
  const item = e.target.closest("[data-idea-text]");
  if (item) toggleIdea(item.dataset.ideaText);
});

$("chosenChips").addEventListener("click", (e) => {
  const x = e.target.closest("[data-chip-x]");
  if (!x) return;
  const idx = Number(x.closest(".chip").dataset.idx);
  selectedIdeas.splice(idx, 1);
  renderIdeas();
  renderChosen();
  tg.HapticFeedback?.selectionChanged();
});

$("addIdeaToggle").onclick = () => showIdeaForm();

async function submitNewIdea() {
  const input = $("newIdeaInput");
  const text = input.value.trim();
  if (!text || !currentCategoryTitle) return;
  try {
    const res = await api("/sex-ideas", { method: "POST", body: { category_title: currentCategoryTitle, text } });
    state.sex_ideas = res.ideas;
    if (!selectedIdeas.includes(text)) selectedIdeas.push(text);
    hideIdeaForm();
    renderIdeas();
    renderChosen();
    tg.HapticFeedback?.selectionChanged();
  } catch (e) { toast(e.message); }
}
$("addIdeaBtn").onclick = submitNewIdea;
$("newIdeaInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); submitNewIdea(); }
  if (e.key === "Escape") { e.preventDefault(); hideIdeaForm(); }
});

$("ideasDone").onclick = () => closeIdeasModal();
$("ideasOverlay").addEventListener("click", (e) => {
  if (e.target === $("ideasOverlay")) closeIdeasModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("catOverlay").hidden) { closeCategoryPicker(); clearMultiSelect(); return; }
  if (!$("ideasOverlay").hidden && !$("ideaForm").hidden) { hideIdeaForm(); return; }
  if (!$("ideasOverlay").hidden) closeIdeasModal();
});