const tg = window.Telegram?.WebApp || {
  initData: "",
  ready: () => {},
  expand: () => {},
  openTelegramLink: (url) => window.open(url, "_blank")
};
try { tg.ready(); tg.expand(); } catch (e) { console.log("Запуск вне Telegram"); }

const $ = (id) => document.getElementById(id);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const CATEGORIES = {
  rest: { label: "Отдых",   icon: "🌿" },
  sex:  { label: "Секс",    icon: "🔥" },
  shop: { label: "Магазин", icon: "🛒" },
  todo: { label: "Дела",    icon: "📌" },
  personal: { label: "Личное", icon: "📝" },
};
const STATUS_TEXT = {
  pending: "⏳ Ждёт ответа", accepted: "✓ Принято", declined: "✕ Отклонено", expired: "Просрочено", done: "✅ Выполнено",
};
const MONTHS_FORMAT = new Intl.DateTimeFormat("ru", { month: "long", year: "numeric" });
const DAY_FORMAT = new Intl.DateTimeFormat("ru", { day: "numeric", month: "long" });
const RANGE_FORMAT = new Intl.DateTimeFormat("ru", { day: "numeric", month: "short" });

const pad = (n) => String(n).padStart(2, "0");
const fmt = (y, m, d) => `${y}-${pad(m + 1)}-${pad(d)}`;
const todayStr = () => { const t = new Date(); return fmt(t.getFullYear(), t.getMonth(), t.getDate()); };
const parse = (s) => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
const ESC_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC_MAP[c]);
const TZ = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch { return ""; } })();
function hexToRgb(hex) {
  const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex || "");
  return m ? `${parseInt(m[1],16)}, ${parseInt(m[2],16)}, ${parseInt(m[3],16)}` : "139, 92, 246";
}
function humanizeMinutes(m) {
  if (m < 60) return `${m} мин`;
  if (m < 1440) { const h = m / 60; return h % 1 === 0 ? `${h} ч` : `${h.toFixed(1)} ч`; }
  const d = m / 1440; return d % 1 === 0 ? `${d} дн` : `${d.toFixed(1)} дн`;
}
function fmtRange(start, end) {
  if (!end || end === start) return DAY_FORMAT.format(parse(start));
  return RANGE_FORMAT.format(parse(start)) + " – " + RANGE_FORMAT.format(parse(end));
}

let state = {
  has_partner: false, invite_link: "", events: [], sex_ideas: [],
  stats: null, todo_colors: [], allowed_reactions: [], allowed_remind_before: [],
  multi_select: new Set(), my_id: 0, balance: 0, moods: null,
};
let selected = todayStr();
let view = (() => { const t = new Date(); return { y: t.getFullYear(), m: t.getMonth() }; })();
let editingEvent = null;
let loaded = false;
const monthKey = () => `${view.y}-${pad(view.m + 1)}`;
const monthCache = {};

// Фронтенд и API раздаются с одного домена, поэтому используем относительные пути (без CORS)
const API_BASE = "";

class HttpError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

async function request(path, { method = "GET", body, etag } = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 60000);
  try {
    const headers = { "X-Init-Data": tg.initData || "", "X-TZ": TZ };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (etag) headers["If-None-Match"] = etag;
    const res = await fetch(API_BASE + "/api" + path, {
      method, headers, signal: ctrl.signal, cache: "no-store",
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    if (res.status === 304) return { status: 304 };
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) throw new HttpError("Сессия истекла. Закройте и снова откройте приложение через бота.", 401);
    if (!res.ok) throw new HttpError(data.error || "Что-то пошло не так", res.status);
    return { status: res.status, data, etag: res.headers.get("ETag") };
  } catch (e) {
    if (e instanceof HttpError) throw e;
    throw new HttpError(e.name === "AbortError" ? "Сервер не отвечает." : "Нет соединения с сервером.", 0);
  } finally {
    clearTimeout(timer);
  }
}
const api = async (path, opts) => (await request(path, opts)).data;

// Загрузка и кэширование статики (цвета, реакции, время напоминаний)
// Идеи для секса приходят в /api/state и всегда актуальны.
async function loadConfig() {
  const CONFIG_KEY = "config_v2";
  const CACHE_TTL = 60 * 60 * 1000; // 1 час

  try {
    const saved = localStorage.getItem(CONFIG_KEY);
    if (saved) {
      const parsed = JSON.parse(saved);
      if (Date.now() - parsed.ts < CACHE_TTL) {
        state.todo_colors = parsed.data.todo_colors;
        state.allowed_reactions = parsed.data.allowed_reactions;
        state.allowed_remind_before = parsed.data.allowed_remind_before;
        return;
      }
    }
  } catch (e) {}

  try {
    const data = await api("/config");
    state.todo_colors = data.todo_colors || [];
    state.allowed_reactions = data.allowed_reactions || [];
    state.allowed_remind_before = data.allowed_remind_before || [];
    
    localStorage.setItem(CONFIG_KEY, JSON.stringify({ ts: Date.now(), data }));
  } catch (e) {
    console.warn("Не удалось загрузить конфиг", e);
  }
}

function toast(text) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = text;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), 2800);
}

function confirmDialog(message, onYes) {
  try {
    if (tg.showConfirm) { tg.showConfirm(message, (ok) => { if (ok) onYes(); }); return; }
  } catch (e) {}
  if (confirm(message)) onYes();
}

function openCommentDialog(initial, initialDate = "", initialTime = "", onConfirm) {
  const overlay = document.createElement("div");
  overlay.className = "overlay";
  overlay.innerHTML = `
    <div class="sheet" role="dialog" aria-modal="true">
      <h2>Отклонить событие</h2>
      <label for="commentText">Комментарий (необязательно)</label>
      <textarea id="commentText" maxlength="500" placeholder="Например: не смогу в этот день..."></textarea>
      
      <label for="altDate">Предложить другую дату (необязательно)</label>
      <input type="date" id="altDate" value="${esc(initialDate)}">
      
      <label for="altTime">Время</label>
      <input type="time" id="altTime" value="${esc(initialTime)}">
      
      <div class="row">
        <button class="btn ghost" id="commentCancel">Отмена</button>
        <button class="btn primary" id="commentOk">Предложить</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  const ta = overlay.querySelector("#commentText");
  ta.value = initial || "";
  const close = () => overlay.remove();
  overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
  overlay.querySelector("#commentCancel").onclick = close;
  overlay.querySelector("#commentOk").onclick = () => {
    const comment = ta.value.trim().slice(0, 500);
    const altDate = overlay.querySelector("#altDate").value;
    const altTime = overlay.querySelector("#altTime").value;
    close();
    onConfirm(comment, altDate, altTime);
  };
  setTimeout(() => ta.focus(), 50);
}

let etag = null, etagKey = "";

// Все модальные окна приложения; используется для блокировки жестов, опроса сервера и FAB-кнопок
const OVERLAY_IDS = ["overlay", "ideasOverlay", "archiveOverlay", "catOverlay", "challengesOverlay", "shopOverlay", "buyDateOverlay",
  "challengeChatOverlay", "photoViewerOverlay"];
const anyOverlayOpen = () => OVERLAY_IDS.some((id) => { const el = $(id); return el && !el.hidden; });