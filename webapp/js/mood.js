// Настроение дня. Ленивый модуль, грузится вместе с main.js.

const MOOD_EMOJIS = ["😊","😐","😔","😡","🥰","😴","🤒","🤩","😢","😤","🥳","🤔"];
const MOOD_COLLAPSED_KEY = "mood_collapsed";

let moodState = { date: "", mine: null, partner: null, partner_id: 0 };
let moodSelected = "";
let moodLoadSeq = 0;

function moodDateStr() {
  return todayStr();
}

/* ---------- Сворачивание/разворачивание ---------- */

function apply() {}