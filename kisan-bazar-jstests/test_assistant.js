const { A } = require("./harness");
let failures = 0, passed = 0;

function check(label, cond, detail) {
  if (cond) { passed++; }
  else { failures++; console.log(`FAIL  ${label}${detail ? " — " + detail : ""}`); }
}

// reset to a known state before each block — the assistant reads S.zone,
// S.land, S.offers, so tests must not leak state into each other
function resetState() {
  A.S.zone = "midhill";
  A.S.land = "khet";
  A.S.offers = [];
  A.chat.messages = [];
}

console.log("=== findCropInText() — Nepali script, English, and romanized Nepali ===");
{
  check("Nepali script 'गोलभेँडा' -> tomato", A.findCropInText("गोलभेँडा भाउ भन्नुहोस्") === "tomato");
  check("English 'tomato' -> tomato", A.findCropInText("what is the tomato price") === "tomato");
  check("romanized 'golbheda' -> tomato", A.findCropInText("golbheda ko bhau kati ho") === "tomato");
  check("romanized alias 'kauli' -> cauliflower", A.findCropInText("kauli ko rate") === "cauliflower");
  check("romanized 'aalu' -> potato", A.findCropInText("aalu bhau") === "potato");
  check("no crop mentioned -> null", A.findCropInText("mausam kasto cha") === null);
  check("ambiguous/empty text -> null", A.findCropInText("") === null);
}

console.log("\n=== scoreIntent() ===");
{
  check("'मौसम कस्तो छ' -> weather", A.scoreIntent("मौसम कस्तो छ") === "weather");
  check("'kasari bechne' -> sell", A.scoreIntent("kasari bechne") === "sell");
  check("'ke lagaune' -> plant", A.scoreIntent("ke lagaune") === "plant");
  check("'paisa kahile aaucha' -> ledger", A.scoreIntent("paisa kahile aaucha") === "ledger");
  check("plain greeting -> greet", A.scoreIntent("namaste") === "greet");
  check("gibberish -> null", A.scoreIntent("xyzabc123") === null);
}

console.log("\n=== assistantReply() — price intent, using LIVE app data (not hardcoded expectations) ===");
{
  resetState();
  const r1 = A.assistantReply("golbheda ko bhau kati ho?");
  check("romanized tomato price question returns the actual current rate",
        r1.text.includes(A.money(A.CROPS.tomato.rate)), `got: ${r1.text}`);

  const r2 = A.assistantReply("धानको भाउ?");
  check("Nepali-script paddy price question returns the actual current rate",
        r2.text.includes(A.money(A.CROPS.paddy.rate)), `got: ${r2.text}`);

  const r3 = A.assistantReply("kauli");
  check("a BARE crop name with no verb still resolves to a price answer",
        r3.text.includes(A.money(A.CROPS.cauliflower.rate)), `got: ${r3.text}`);
}

console.log("\n=== assistantReply() — weather intent reflects the CURRENT zone, not a fixed one ===");
{
  for (const zone of A.ZONES) {
    A.S.zone = zone.id;
    const r = A.assistantReply("mausam kasto cha");
    const expected = A.WEATHER[zone.id].ne;
    check(`weather question in zone=${zone.id} returns THAT zone's bulletin`,
          r.text === expected, `zone ${zone.id}: got "${r.text.slice(0,30)}..."`);
  }
}

console.log("\n=== assistantReply() — plant intent reflects CURRENT zone+land, matches viewAdvice()'s own logic ===");
{
  for (const zone of A.ZONES) {
    for (const land of A.LANDS) {
      A.S.zone = zone.id; A.S.land = land.id;
      const recs = A.RULES.filter(r => r.zones.includes(zone.id) && r.lands.includes(land.id) && r.sow.includes(A.BS_NOW));
      const reply = A.assistantReply("ke lagaune?");
      if (recs.length > 0) {
        const firstCropName = A.CROPS[recs[0].crop].ne;
        check(`plant question for ${zone.id}/${land.id} mentions a real recommended crop`,
              reply.text.includes(firstCropName), `expected "${firstCropName}" in: ${reply.text}`);
      } else {
        check(`plant question for ${zone.id}/${land.id} (no recs) gives the soil-improvement fallback, not a fabricated crop`,
              reply.text.includes("सुधार्ने") || reply.text.includes("soil"), `got: ${reply.text}`);
      }
    }
  }
}

console.log("\n=== assistantReply() — ledger intent reflects the farmer's ACTUAL pending offers ===");
{
  resetState();
  const r0 = A.assistantReply("mero paisa kati baaki cha");
  check("no offers -> 'no pending payments', not a stale/wrong number",
        r0.text.includes("बाँकी छैन") || r0.text.includes("no pending"), `got: ${r0.text}`);

  A.S.offers = [
    { crop: "cauliflower", kg: 40, grade: "A", status: "new", lot: null, rate: 55, day: "आज" },
    { crop: "tomato", kg: 25, grade: "B", status: "paid", lot: null, rate: 38, day: "गत हप्ता" }
  ];
  const expectedDue = 40 * 55; // only the unpaid offer counts
  const r1 = A.assistantReply("paisa kahile aaucha");
  check(`with one pending offer, reply includes the CORRECT due amount (${A.money(expectedDue)}), not the paid one`,
        r1.text.includes(A.money(expectedDue)), `got: ${r1.text}`);
  check("paid offer's amount does NOT leak into the 'due' figure",
        !r1.text.includes(A.money(25*38)) || A.money(expectedDue) === A.money(25*38),
        "paid offer amount should not appear as if it were still owed");
}

console.log("\n=== assistantReply() — fallback path never crashes, always offers a way forward ===");
{
  const gibberish = ["asdkjhasjkd", "🎉🎉🎉", "1234567890", ""];
  for (const g of gibberish) {
    const r = A.assistantReply(g);
    check(`gibberish input "${g.slice(0,15)}" returns a non-empty reply with chips`,
          typeof r.text === "string" && r.text.length > 0 && Array.isArray(r.chips) && r.chips.length > 0,
          JSON.stringify(r));
  }
}

console.log("\n=== renderSahayak() / chat state ===");
{
  resetState();
  A.chat.open = false;
  const closedHtml = A.renderSahayak();
  check("closed chat shows only the FAB, not the panel", !closedHtml.includes("chatPanel"));
  check("closed chat FAB shows the robot icon", closedHtml.includes("🤖"));

  A.chat.open = true;
  const openHtml = A.renderSahayak();
  check("open chat shows the panel", openHtml.includes("chatPanel"));
  check("open chat FAB shows the close icon (✕), not the robot", openHtml.includes(">✕<") || openHtml.includes("✕</button>"));

  // ensureGreeting() should have seeded exactly one bot message by now
  check("chat has at least one message after opening (greeting)", A.chat.messages.length >= 1);
  check("first message is from the bot, not fabricated as a user message", A.chat.messages[0].from === "bot");

  const beforeCount = A.chat.messages.length;
  A.sendChatMessage("धानको भाउ?");
  check("sendChatMessage adds exactly 2 messages (user question + bot answer)",
        A.chat.messages.length === beforeCount + 2, `${A.chat.messages.length - beforeCount} added`);
  check("the message just before the bot's reply is the user's own text",
        A.chat.messages[A.chat.messages.length - 2].from === "user" &&
        A.chat.messages[A.chat.messages.length - 2].text === "धानको भाउ?");

  const beforeCount2 = A.chat.messages.length;
  A.sendChatMessage("   ");
  check("sending only whitespace adds NOTHING (no empty user bubble)",
        A.chat.messages.length === beforeCount2, `expected no change, got +${A.chat.messages.length - beforeCount2}`);
}

console.log("\n=== chips only appear on the LAST message, not accumulating on every past message ===");
{
  resetState();
  A.chat.open = true;
  A.renderSahayak(); // seeds greeting
  A.sendChatMessage("namaste");
  A.sendChatMessage("mausam kasto cha");
  const html = A.renderSahayak();
  const chipRowCount = (html.match(/class="chatChips"/g) || []).length;
  check("exactly one chatChips block renders, regardless of message history length",
        chipRowCount === 1, `found ${chipRowCount} chip rows for ${A.chat.messages.length} messages`);
}

console.log(`\n${passed} passed, ${failures} failed.`);
process.exit(failures > 0 ? 1 : 0);
