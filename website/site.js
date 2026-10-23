/* Shared site JS: language toggle + live API data */
const API = "";  // same origin — API lives at /v1/* alongside /site/*
let LANG = localStorage.getItem("kb-lang") || "ne";

/* ---------------- website session (fully separate from the app) ----
   The website and the app are separate surfaces with separate
   sessions, even though they share one account on the server: a
   website login must NOT sign the app in, and an app login must NOT
   sign the website in. The app keeps its session under its own
   "kb:account*" localStorage keys; the website NEVER reads or writes
   those keys. The website session lives under its own "kbw:account*"
   namespace, written only by the website login (login.html) and
   cleared only by a website logout. Leftover "kb:" keys (app session,
   or written by the pre-separation website flow) are simply ignored
   here — never adopted as a website session. */
const WEB_KEY = "kbw:";
function webToken(){
  try{
    const raw = localStorage.getItem(WEB_KEY + "accountToken");
    if(!raw) return null;
    try{ return JSON.parse(raw); }catch(_e){ return raw; }
  }catch(_e){ return null; }
}
function webPhone(){
  try{
    const raw = localStorage.getItem(WEB_KEY + "accountPhone");
    if(!raw) return "";
    try{ return JSON.parse(raw) || ""; }catch(_e){ return raw; }
  }catch(_e){ return ""; }
}
function clearWebSession(){
  ["accountToken","accountPhone","accountName","accountRole","accountIsStaff"]
    .forEach(k=>{ try{ localStorage.removeItem(WEB_KEY + k); }catch(_e){} });
}
/* Header/footer chrome reflects session state: every Login entry
   point (href login.html / account.html) becomes "Account" while a
   session exists, and reverts to "Login" once it is gone. */
function updateAuthNav(){
  const logged = !!webToken();
  document.querySelectorAll('a[href="login.html"], a[href="account.html"]').forEach(a=>{
    if(logged){
      a.href = "account.html";
      a.setAttribute("data-ne", "खाता");
      a.setAttribute("data-en", "Account");
      a.textContent = LANG === "ne" ? "खाता" : "Account";
    }else{
      a.href = "login.html";
      a.setAttribute("data-ne", "लगइन");
      a.setAttribute("data-en", "Login");
      a.textContent = LANG === "ne" ? "लगइन" : "Login";
    }
  });
}
/* Authenticated GET for website account pages. A 401 means the
   website session died (expired token): clear it and bounce to the
   website login — never leave a dead session rendered as a broken
   account. Only the website (kbw:) session is touched. */
async function apiAuthed(path){
  const token = webToken();
  const r = await fetch(API + path, {headers: {"Authorization": "Bearer " + token}});
  if(r.status === 401){
    clearWebSession();
    window.location.href = "login.html";
    throw new Error("401");
  }
  if(!r.ok) throw new Error(r.status);
  return r.json();
}

function toggleLang(){
  LANG = LANG === "ne" ? "en" : "ne";
  localStorage.setItem("kb-lang", LANG);
  applyLang();
}
function applyLang(){
  document.documentElement.lang = LANG;
  document.querySelectorAll("[data-ne]").forEach(el=>{
    el.textContent = el.dataset[LANG === "ne" ? "ne" : "en"];
  });
  document.querySelectorAll("[data-aria-ne]").forEach(el=>{
    el.setAttribute("aria-label", el.dataset[LANG === "ne" ? "ariaNe" : "ariaEn"]);
  });
  // Session-aware chrome runs AFTER the generic swap above, so the
  // Login/Account entry point ends in the right language + target.
  updateAuthNav();
  // Re-render dynamic lot cards so they follow the language toggle
  const ml = document.getElementById("market-lots");
  if(ml) loadMarketLots(ml);
  const lp = document.getElementById("lot-preview");
  if(lp) loadLotPreview();
  // lot.html renders its detail card from an inline script — re-render it too
  if(typeof renderLotDetail === "function") renderLotDetail();
  // account.html renders its data from an inline script — re-render it too
  if(typeof loadAccount === "function") loadAccount();
}
document.addEventListener("DOMContentLoaded", ()=>{
  applyLang();
  loadStats();
  // Keep the mobile menu button's aria-expanded in sync with the nav
  // it controls (the inline onclick toggles .open; this mirrors state).
  const menuBtn = document.querySelector(".menu-btn");
  const mainNav = document.getElementById("main-nav");
  if(menuBtn && mainNav){
    menuBtn.addEventListener("click", ()=>{
      menuBtn.setAttribute("aria-expanded", mainNav.classList.contains("open") ? "true" : "false");
    });
  }
});

async function api(path){
  const r = await fetch(API + path);
  if(!r.ok) throw new Error(r.status);
  const payload = await r.json();
  // Unwrap paginated responses: {data, total, ...} -> data array
  return Array.isArray(payload) ? payload : (payload.data !== undefined ? payload.data : payload);
}

const CROP_ICON = {GOLB:"🍅", KAULI:"🥦", ALU:"🥔", PYAJ:"🧅", ADUWA:"🫚", ALAICHI:"🌰",
  JUNAR:"🍊", SYAU:"🍎", MAKAI:"🌽", DHAN:"🌾", GAHU:"🌾", MUSURO:"🫘", TORI:"🌻",
  MULA:"🥕", KERAU:"🫛", FAPAR:"🌾"};
const CROP_NE = {GOLB:"गोलभेँडा", KAULI:"काउली", ALU:"आलु", PYAJ:"प्याज", ADUWA:"अदुवा",
  ALAICHI:"अलैँची", JUNAR:"जुनार", SYAU:"स्याउ", MAKAI:"मकै", DHAN:"धान", GAHU:"गहुँ",
  MUSURO:"मुसुरो", TORI:"तोरी", MULA:"मुला", KERAU:"केराउ", FAPAR:"फापर"};
const CROP_EN = {GOLB:"Tomato", KAULI:"Cauliflower", ALU:"Potato", PYAJ:"Onion", ADUWA:"Ginger",
  ALAICHI:"Cardamom", JUNAR:"Junar", SYAU:"Apple", MAKAI:"Maize", DHAN:"Paddy", GAHU:"Wheat",
  MUSURO:"Lentil", TORI:"Mustard", MULA:"Radish", KERAU:"Pea", FAPAR:"Buckwheat"};

function lotCard(l){
  const ne = LANG === "ne";
  const name = ne ? (CROP_NE[l.crop_code]||l.crop_code) : (CROP_EN[l.crop_code]||l.crop_code);
  return `<a href="lot.html?id=${encodeURIComponent(l.id)}" class="lot-card" style="text-decoration:none;color:inherit">
    <div class="em">${CROP_ICON[l.crop_code]||"🌾"}</div>
    <div class="bd"><b>${name}</b>
      <p>${l.pooled_kg} kg · ${l.status==="ready" ? (ne?"तयार":"Ready") : l.status}</p></div>
    <div class="money">रू ${l.rate_per_kg||"—"}<small>${ne?"प्रति के.जी.":"per kg"}</small></div>
  </a>`;
}

async function loadStats(){
  try{
    const s = await api("/v1/stats").catch(()=>null);
    if(!s) return;
    // Show real numbers only; hide a stat rather than show a sad "0"
    const show = (id, v) => {
      const el = document.getElementById(id);
      if(!el) return;
      if(v > 0) el.textContent = v;
      else el.parentElement.style.display = "none";
    };
    show("stat-lots", s.active_lots);
    show("stat-farmers", s.farmers);
    show("stat-coops", s.coops);
  }catch(e){}
}
function set(id, v){ const el = document.getElementById(id); if(el) el.textContent = v; }

async function loadLotPreview(){
  const box = document.getElementById("lot-preview");
  if(!box) return;
  try{
    // /v1/market/lots is the buyer-facing list of ready lots (flat array,
    // no pagination trap — unlike /v1/lots, which only returns page 1).
    const lots = await api("/v1/market/lots");
    const ready = (lots||[]).slice(0,6);
    box.innerHTML = ready.length ? ready.map(lotCard).join("")
      : `<p class="muted">${LANG==="ne"?"अहिले कुनै लट छैन।":"No lots right now."}</p>`;
  }catch(e){
    box.innerHTML = `<p class="muted">${LANG==="ne"?"जोडिन सकिएन। एपमा हेर्नुहोस्।":"Could not load. See the app."}</p>`;
  }
}

async function loadMarketLots(box){
  try{
    // Buyer-facing ready-lots list (flat array, no pagination trap).
    const lots = await api("/v1/market/lots");
    const ready = (lots||[]).filter(l=>l.status==="ready");
    box.innerHTML = ready.length ? ready.map(lotCard).join("")
      : `<div class="empty">
          <p>${LANG==="ne"?"अहिले कुनै लट छैन। सहकारीले नयाँ लट बनाउँदा यहाँ देखिन्छ।":"No lots right now. New lots appear here as co-ops form them."}</p>
          <a href="/app" class="btn-primary" data-ne="एपमा हेर्नुहोस्" data-en="Check the app">${LANG==="ne"?"एपमा हेर्नुहोस्":"Check the app"}</a>
        </div>`;
  }catch(e){
    box.innerHTML = `<p class="muted">${LANG==="ne"?"लोड हुन सकेन।":"Could not load."}</p>`;
  }
}
