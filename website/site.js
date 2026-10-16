/* Shared site JS: language toggle + live API data */
const API = "";  // same origin — API lives at /v1/* alongside /site/*
let LANG = localStorage.getItem("kb-lang") || "ne";

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
}
document.addEventListener("DOMContentLoaded", ()=>{
  applyLang();
  loadStats();
  loadLotPreview();
  const ml = document.getElementById("market-lots");
  if(ml) loadMarketLots(ml);
});

async function api(path){
  const r = await fetch(API + path);
  if(!r.ok) throw new Error(r.status);
  return r.json();
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
    const [lots] = await Promise.all([api("/v1/lots").catch(()=>[])]);
    const ready = (lots||[]).filter(l=>l.status==="ready");
    set("stat-lots", ready.length);
    set("stat-farmers", "500+");
    set("stat-coops", "12+");
  }catch(e){}
}
function set(id, v){ const el = document.getElementById(id); if(el) el.textContent = v; }

async function loadLotPreview(){
  const box = document.getElementById("lot-preview");
  if(!box) return;
  try{
    const lots = await api("/v1/lots");
    const ready = (lots||[]).filter(l=>l.status==="ready").slice(0,6);
    box.innerHTML = ready.length ? ready.map(lotCard).join("")
      : `<p class="muted">${LANG==="ne"?"अहिले कुनै लट छैन।":"No lots right now."}</p>`;
  }catch(e){
    box.innerHTML = `<p class="muted">${LANG==="ne"?"जोडिन सकिएन। एपमा हेर्नुहोस्।":"Could not load. See the app."}</p>`;
  }
}

async function loadMarketLots(box){
  try{
    const lots = await api("/v1/lots");
    const ready = (lots||[]).filter(l=>l.status==="ready");
    box.innerHTML = ready.length ? ready.map(lotCard).join("")
      : `<p class="muted">${LANG==="ne"?"अहिले कुनै लट छैन।":"No lots right now."}</p>`;
  }catch(e){
    box.innerHTML = `<p class="muted">${LANG==="ne"?"लोड हुन सकेन।":"Could not load."}</p>`;
  }
}
