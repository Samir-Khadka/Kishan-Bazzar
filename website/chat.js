/* Kisan Bazar website AI assistant widget — calls /v1/assistant/chat */
(function(){
  if(document.getElementById("kb-chat-fab")) return;

  const css = `
  #kb-chat-fab{position:fixed;bottom:22px;right:22px;width:56px;height:56px;border-radius:50%;
    background:#2E7D32;color:#fff;border:none;font-size:26px;cursor:pointer;z-index:9998;
    box-shadow:0 4px 16px rgba(0,0,0,.25);display:flex;align-items:center;justify-content:center}
  #kb-chat-fab:hover{background:#1B5E20}
  #kb-chat-panel{position:fixed;bottom:90px;right:22px;width:360px;max-width:calc(100vw - 44px);
    height:480px;max-height:calc(100vh - 120px);background:#fff;border-radius:14px;z-index:9999;
    box-shadow:0 8px 32px rgba(0,0,0,.25);display:none;flex-direction:column;overflow:hidden;
    border:1px solid #e0e0e0}
  #kb-chat-panel.open{display:flex}
  #kb-chat-head{background:#2E7D32;color:#fff;padding:12px 16px;font-weight:700;display:flex;
    align-items:center;justify-content:space-between}
  #kb-chat-head button{background:none;border:none;color:#fff;font-size:20px;cursor:pointer}
  #kb-chat-msgs{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px;
    background:#FAF7F0}
  .kb-msg{max-width:85%;padding:10px 13px;border-radius:12px;font-size:14px;line-height:1.5}
  .kb-msg.bot{background:#fff;border:1px solid #e0e0e0;align-self:flex-start;border-bottom-left-radius:4px}
  .kb-msg.user{background:#2E7D32;color:#fff;align-self:flex-end;border-bottom-right-radius:4px}
  .kb-msg.typing{opacity:.6}
  #kb-chat-input{display:flex;gap:8px;padding:10px;border-top:1px solid #e0e0e0;background:#fff}
  #kb-chat-input input{flex:1;padding:10px 12px;border:1px solid #ccc;border-radius:20px;font-size:14px;outline:none}
  #kb-chat-input input:focus{border-color:#2E7D32}
  #kb-chat-input button{background:#2E7D32;color:#fff;border:none;border-radius:50%;width:40px;height:40px;
    font-size:18px;cursor:pointer;flex-shrink:0}
  #kb-chat-input button:hover{background:#1B5E20}
  .kb-chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
  .kb-chip{background:#E8F5E9;border:1px solid #A5D6A7;color:#1B5E20;border-radius:16px;
    padding:6px 12px;font-size:12px;cursor:pointer}
  .kb-chip:hover{background:#C8E6C9}
  .kb-card{background:#fff;border:1px solid #e0e0e0;border-radius:10px;padding:10px 12px;
    margin-top:8px;display:flex;gap:10px;align-items:center;font-size:13px}
  .kb-card .ic{font-size:24px}
  .kb-card b{display:block}
  @media(max-width:480px){#kb-chat-panel{right:12px;bottom:82px}}`;

  const style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);

  const fab = document.createElement("button");
  fab.id = "kb-chat-fab";
  fab.innerHTML = "💬";
  fab.setAttribute("aria-label", "Chat with assistant");
  document.body.appendChild(fab);

  const panel = document.createElement("div");
  panel.id = "kb-chat-panel";
  panel.innerHTML = `
    <div id="kb-chat-head"><span>🤖 <span data-ne="सहायक" data-en="Assistant">सहायक</span></span><button id="kb-chat-close">×</button></div>
    <div id="kb-chat-msgs"></div>
    <div id="kb-chat-input"><input id="kb-chat-text" data-ph-ne="सन्देश लेख्नुहोस्…" data-ph-en="Type a message…" placeholder="सन्देश लेख्नुहोस्…" maxlength="500"><button id="kb-chat-send">➤</button></div>`;
  document.body.appendChild(panel);

  // Translate the chat input placeholder when language toggles.
  const chatInput = panel.querySelector("#kb-chat-text");
  const syncChatLang = () => {
    const lang = document.documentElement.lang === "en" ? "en" : "ne";
    chatInput.placeholder = chatInput.getAttribute(lang === "en" ? "data-ph-en" : "data-ph-ne") || chatInput.placeholder;
  };
  new MutationObserver(syncChatLang).observe(document.documentElement, {attributes: true, attributeFilter: ["lang"]});
  syncChatLang();

  const msgs = panel.querySelector("#kb-chat-msgs");
  const input = panel.querySelector("#kb-chat-text");
  let sessionId = "web-" + Math.random().toString(36).slice(2, 10);
  let greeted = false;

  function addMsg(text, who){
    const d = document.createElement("div");
    d.className = "kb-msg " + who;
    d.textContent = text;
    msgs.appendChild(d);
    msgs.scrollTop = msgs.scrollHeight;
    return d;
  }
  function greet(){
    if(greeted) return; greeted = true;
    const ne = (typeof LANG !== "undefined" ? LANG : "ne") === "ne";
    addMsg(ne ? "नमस्ते! 🙏 म किसान बजार सहायक हुँ। भाउ, बाली, वा अर्डरबारे सोध्नुहोस्।"
              : "Hello! 🙏 I'm the Kisan Bazar assistant. Ask me about prices, crops, or orders.", "bot");
  }
  function renderCard(card){
    if(!card) return null;
    const d = document.createElement("div");
    d.className = "kb-card";
    if(card.type === "price"){
      const arrow = card.change > 0 ? "📈" : card.change < 0 ? "📉" : "➡️";
      d.innerHTML = `<span class="ic">${card.icon || "🌾"}</span>
        <div><b></b><span>रू ${card.rate}/kg ${arrow}</span></div>`;
      d.querySelector("b").textContent = card.name || "";
    }else if(card.type === "weather"){
      d.innerHTML = `<span class="ic">🌤️</span><div><b></b><span></span></div>`;
      d.querySelector("b").textContent = card.today || "";
      d.querySelector("span:last-child").textContent = card.advice || "";
    }else return null;
    return d;
  }
  function renderChips(chips){
    if(!chips || !chips.length) return;
    const wrap = document.createElement("div");
    wrap.className = "kb-chips";
    chips.forEach(c=>{
      const b = document.createElement("button");
      b.className = "kb-chip";
      b.textContent = c;
      b.onclick = ()=>{ input.value = c; send(); };
      wrap.appendChild(b);
    });
    msgs.appendChild(wrap);
    msgs.scrollTop = msgs.scrollHeight;
  }
  async function send(){
    const text = input.value.trim();
    if(!text) return;
    input.value = "";
    addMsg(text, "user");
    const typing = addMsg("…", "bot typing");
    try{
      const r = await fetch(API + "/v1/assistant/chat", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({message: text, session_id: sessionId})
      });
      const d = await r.json();
      typing.remove();
      // Surface server errors (e.g. rate limit) instead of a bare "…"
      addMsg(d.reply || d.error || "…", "bot");
      (d.cards || []).forEach(c=>{ const el = renderCard(c); if(el) msgs.appendChild(el); });
      renderChips(d.chips);
      msgs.scrollTop = msgs.scrollHeight;
      if(d.session_id) sessionId = d.session_id;
    }catch(e){
      typing.remove();
      const ne = (typeof LANG !== "undefined" ? LANG : "ne") === "ne";
      addMsg(ne ? "माफ गर्नुहोस्, जडान हुन सकेन।" : "Sorry, could not connect.", "bot");
    }
  }

  fab.onclick = ()=>{ panel.classList.toggle("open"); greet(); if(panel.classList.contains("open")) input.focus(); };
  panel.querySelector("#kb-chat-close").onclick = ()=> panel.classList.remove("open");
  panel.querySelector("#kb-chat-send").onclick = send;
  input.addEventListener("keypress", e=>{ if(e.key === "Enter") send(); });
})();
