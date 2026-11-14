/*
 * Frontend JS tests for the 2026-11-14 daily-improve run (focus: frontend JS).
 * All exercise the REAL shipped code from kisan-bazar.html via the harness.
 *
 *   - offerAmount() / offerRow() / viewMine() / assistantReply(): one
 *     corrupt stored offer (missing or non-numeric kg/rate — e.g. written
 *     by an older build) used to poison the whole money sum with NaN, so
 *     the Mine tab showed "रु NaN" and the assistant claimed "no pending
 *     payments" while real offers were still pending. A corrupt row now
 *     counts as 0 instead of blanking the ledger.
 *   - offerRow() day: offers stored before the 2026-10-20 fix carry a
 *     localized day ("आज"/"Today") that used to leak raw into the other
 *     language's UI. Only the canonical "today"/"last_week" markers
 *     localize now; anything else renders no day at all.
 *
 * Run: node test_frontend_nov14.js
 */
const { A, sandbox, evalInApp } = require("./harness");

let passed = 0, failed = 0;
function check(label, cond, detail) {
  if (cond) { passed++; }
  else { failed++; console.log(`FAIL  ${label}${detail ? " — " + detail : ""}`); }
}

(async () => {

console.log("=== offerAmount() — corrupt rows count as 0, never NaN ===");
{
  check("good offer: 25kg x 48 = 1200",
    A.offerAmount({kg:25, rate:48}) === 1200);
  check("missing kg counts as 0",
    A.offerAmount({rate:48}) === 0);
  check("missing rate counts as 0",
    A.offerAmount({kg:40}) === 0);
  check("non-numeric kg counts as 0",
    A.offerAmount({kg:"abc", rate:48}) === 0);
  check("string numbers still compute (\"25\" x \"48\")",
    A.offerAmount({kg:"25", rate:"48"}) === 1200);
  check("null/undefined fields count as 0",
    A.offerAmount({kg:null, rate:undefined}) === 0);
}

console.log("\n=== offerRow() — corrupt rows render 0, never NaN/undefined ===");
{
  const row = A.offerRow({id:2, crop:"cauliflower", grade:"A", status:"new", day:"today"});
  check("missing kg/rate: no 'NaN' anywhere", !row.includes("NaN"), row);
  check("missing kg/rate: no 'undefined' anywhere", !row.includes("undefined"), row);
  check("missing kg/rate: shows 0 kg and Rs 0",
    row.includes(A.money(0)) && row.includes(A.N(0)), row);

  const good = A.offerRow({id:1, crop:"tomato", kg:25, grade:"A", status:"paid", rate:48, day:"today"});
  check("good offer unchanged: 25 kg, Rs 1,200",
    good.includes("२५") && good.includes("१,२००"), good);
  check("good offer: canonical day still localizes (NE 'आज')",
    good.includes("आज"), good);

  const noGrade = A.offerRow({id:4, crop:"potato", kg:10, status:"new", rate:38, day:"last_week"});
  check("missing grade renders '—', not 'undefined'",
    !noGrade.includes("undefined") && noGrade.includes("—"), noGrade);
  check("canonical 'last_week' still localizes (NE 'गत हप्ता')",
    noGrade.includes("गत हप्ता"), noGrade);
}

console.log("\n=== offerRow() — pre-2026-10-20 localized day no longer leaks ===");
{
  evalInApp(`L = "en";`);
  const en = A.offerRow({id:3, crop:"potato", kg:10, grade:"B", status:"new", rate:38, day:"आज"});
  check("EN mode: stored 'आज' does NOT leak raw into the English UI",
    !en.includes("आज"), en);
  check("EN mode: no day fragment at all for an unknown day",
    !en.includes("Today"), en);
  const enToday = A.offerRow({id:5, crop:"potato", kg:10, grade:"B", status:"new", rate:38, day:"today"});
  check("EN mode: canonical 'today' still renders 'Today'",
    enToday.includes("Today"), enToday);
  const enLast = A.offerRow({id:6, crop:"potato", kg:10, grade:"B", status:"new", rate:38, day:"last_week"});
  check("EN mode: canonical 'last_week' still renders 'Last week'",
    enLast.includes("Last week"), enLast);
  evalInApp(`L = "ne";`);
}

console.log("\n=== viewMine() — one corrupt offer no longer NaNs the KPIs ===");
{
  evalInApp(`S.offers = [
    {id:1, crop:"tomato", kg:25, grade:"A", status:"paid", rate:48, day:"today"},
    {id:2, crop:"cauliflower", kg:40, grade:"A", status:"new", day:"today"},
    {id:3, crop:"potato", kg:10, grade:"B", status:"new", rate:38, day:"आज"}
  ];`);
  const mine = A.viewMine();
  check("Pending KPI is not NaN", !mine.includes("NaN"), mine.slice(0, 300));
  check("Pending KPI counts only the good pending offer (10x38 = 380)",
    mine.includes(A.money(380)), mine.slice(0, 300));
  check("Received KPI still counts the paid offer (25x48 = 1200)",
    mine.includes(A.money(1200)), mine.slice(0, 300));
  check("no 'undefined' anywhere in the Mine tab", !mine.includes("undefined"));

  evalInApp(`S.offers = [
    {id:1, crop:"tomato", kg:25, grade:"A", status:"paid", rate:48, day:"today"}
  ];`);
  const mineClean = A.viewMine();
  check("all-paid ledger unchanged: Received 1200, Pending 0",
    mineClean.includes(A.money(1200)) && mineClean.includes(A.money(0)), mineClean.slice(0, 300));

  evalInApp(`S.offers = [];`);
}

console.log("\n=== assistantReply() ledger — corrupt row no longer hides real dues ===");
{
  evalInApp(`S.offers = [
    {id:2, crop:"cauliflower", kg:40, grade:"A", status:"new", day:"today"},
    {id:3, crop:"potato", kg:10, grade:"B", status:"new", rate:38, day:"today"}
  ];`);
  const reply = A.assistantReply("mero hisab herna");
  check("ledger does NOT claim 'no pending payments' while a good offer is pending",
    !reply.text.includes("बाँकी छैन"), reply.text);
  check("ledger states the real pending amount (380)",
    reply.text.includes(A.money(380)), reply.text);

  evalInApp(`S.offers = [{id:2, crop:"cauliflower", status:"new", day:"today"}];`);
  const onlyCorrupt = A.assistantReply("mero hisab herna");
  check("only-corrupt ledger: no NaN in the reply", !onlyCorrupt.text.includes("NaN"), onlyCorrupt.text);
  check("only-corrupt ledger: honestly reports nothing pending (0 counts as 0)",
    onlyCorrupt.text.includes("बाँकी छैन"), onlyCorrupt.text);

  evalInApp(`S.offers = [];`);
}

console.log(`\n${passed} passed, ${failed} failed.`);
process.exit(failed > 0 ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
