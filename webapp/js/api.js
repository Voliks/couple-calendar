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
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
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
  multi_select: new Set(),
};
let selected = todayStr();
let view = (() => { const t = new Date(); return { y: t.getFullYear(), m: t.getMonth() }; })();
let editingEvent = null;
let loaded = false;
const monthKey = () => `${view.y}-${pad(view.m + 1)}`;
const monthCache = {};

const API_BASE = (document.querySelector('meta[name="api-base"]')?.content || "").replace(/\/$/, "");

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

function openCommentDialog(initial, onConfirm) {
  const overlay = document.createElement("div");
  overlay.className = "overlay";
  overlay.innerHTML = `
    <div class="sheet" role="dialog" aria-modal="true">
      <h2>Комментарий</h2>
      <label for="commentText">Текст комментария</label>
      <textarea id="commentText" maxlength="500" placeholder="Например: не смогу в этот день..."></textarea>
      <div class="row">
        <button class="btn ghost" id="commentCancel">Отмена</button>
        <button class="btn primary" id="commentOk">Сохранить</button>
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
    close();
    onConfirm(comment);
  };
  setTimeout(() => ta.focus(), 50);
}

let etag = null, etagKey = "";
