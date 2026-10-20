const { A, sandbox, evalInApp } = require("./harness");
let failures = 0, passed = 0;

function check(label, cond, detail) {
  if (cond) { passed++; }
  else { failures++; console.log(`FAIL  ${label}${detail ? " — " + detail : ""}`); }
}

console.log("=== N() / money() — Devanagari numeral formatting ===");
{
  check("N(0) is देवनागरी शून्य", A.N(0) === "०", `got ${A.N(0)}`);
  check("N(123) digit-by-digit maps correctly", A.N(123) === "१२३", `got ${A.N(123)}`);
  check("money() includes rु prefix in Nepali", A.money(100).includes("रु"), `got ${A.money(100)}`);
  check("money() rounds non-integers", A.money(99.6) === A.money(100), `${A.money(99.6)} vs ${A.money(100)}`);
  check("money() handles zero without throwing", (() => { try { A.money(0); return true; } catch { return false; } })());
}

console.log("\n=== esc() — XSS/HTML-escaping ===");
{
  check("escapes <script>", A.esc("<script>alert(1)</script>").includes("&lt;script&gt;"));
  check("escapes double quotes", A.esc('say "hi"').includes("&quot;"));
  check("escapes ampersand", A.esc("A & B").includes("&amp;"));
  check("leaves Devanagari text untouched", A.esc("किसान") === "किसान");
}

console.log("\n=== classifySoilColor() + combineLandType() (already unit-tested standalone, re-verified in situ) ===");
{
  check("near-black -> dark", A.classifySoilColor(40,32,24) === "dark");
  check("medium brown loam -> NOT red (regression check)", A.classifySoilColor(120,100,80) !== "red",
        `got ${A.classifySoilColor(120,100,80)}`);
  const r = A.combineLandType("dark","clay","khet");
  check("dark+clay+khet -> khet/good", r.code === "khet" && r.confidence === "good", JSON.stringify(r));
}

console.log("\n=== RULES data integrity ===");
{
  const validLandCodes = A.LANDS.map(l => l.id);
  const validZoneIds = A.ZONES.map(z => z.id);
  let badZones = 0, badLands = 0, badMonths = 0, missingCrop = 0;
  for (const rule of A.RULES) {
    if (!A.CROPS[rule.crop]) missingCrop++;
    for (const z of rule.zones) if (!validZoneIds.includes(z)) badZones++;
    for (const l of rule.lands) if (!validLandCodes.includes(l)) badLands++;
    for (const m of rule.sow) if (m < 0 || m > 11 || !Number.isInteger(m)) badMonths++;
  }
  check(`every rule.crop exists in CROPS (${A.RULES.length} rules checked)`, missingCrop === 0, `${missingCrop} missing`);
  check("every rule.zones value is a real zone id", badZones === 0, `${badZones} bad`);
  check("every rule.lands value is a real land id", badLands === 0, `${badLands} bad`);
  check("every rule.sow month is a valid 0-11 integer", badMonths === 0, `${badMonths} bad`);
}

console.log("\n=== viewAdvice() across all zone x land combinations (20 combos) — must never throw ===");
{
  let crashed = 0;
  const emptyCombos = [];
  for (const zone of A.ZONES) {
    for (const land of A.LANDS) {
      A.S.zone = zone.id;
      A.S.land = land.id;
      A.scan.mode = "manual"; // ensure we're testing the manual-selection path
      try {
        const html = A.viewAdvice();
        if (typeof html !== "string" || html.length === 0) crashed++;
        // recommendation cards render as class="rec ..."; the empty state
        // renders as class="card empty" — checking for generic "card" would
        // false-positive on every screen, since that word appears in CSS
        // class names used by the empty state, the intro banner, etc. too.
        if (!html.includes('class="rec')) emptyCombos.push(`${zone.id}/${land.id}`);
      } catch (e) {
        crashed++;
        console.log(`  EXCEPTION at zone=${zone.id} land=${land.id}: ${e.message}`);
      }
    }
  }
  check("no exceptions across all 20 zone x land combos", crashed === 0, `${crashed} crashed`);
  // agronomically expected to be empty: pakho isn't a primary configuration
  // in the terai/inner/midhill rule set, and khet/balaute barely exist at
  // high elevation — this list should match that pattern, not be arbitrary
  console.log(`  ${emptyCombos.length}/20 combos show no recommendation this month (expected — sparse high-elevation and pakho coverage):`);
  console.log(`    ${emptyCombos.join(", ")}`);
  const surprising = emptyCombos.filter(c => !c.includes("pakho") && !c.startsWith("highhill/") && !c.startsWith("mountain/"));
  check("no unexpected gaps outside the known-sparse pakho/high-elevation combos",
        surprising.length === 0, `unexpected empty combos: ${surprising.join(", ")}`);
}

console.log("\n=== viewToday() across all 5 zones — must never throw, must reflect the right zone's weather ===");
{
  let crashed = 0;
  for (const zone of A.ZONES) {
    A.S.zone = zone.id;
    try {
      const html = A.viewToday();
      const expected = A.WEATHER[zone.id];
      const expectedText = expected.ne; // app defaults to Nepali (L starts "ne")
      check(`viewToday() for zone=${zone.id} includes that zone's bulletin text`,
            html.includes(expectedText.slice(0, 15)), `zone ${zone.id}`);
    } catch (e) {
      crashed++;
      console.log(`  EXCEPTION at zone=${zone.id}: ${e.message}`);
    }
  }
  check("no exceptions across all 5 zones in viewToday()", crashed === 0, `${crashed} crashed`);
}

console.log("\n=== viewSell() earnings math ===");
{
  A.sell.crop = "tomato"; A.sell.kg = 25; A.sell.grade = "A";
  const html = A.viewSell();
  const rate = A.CROPS.tomato.rate; // grade A multiplier is 1
  const expectedTotal = rate * 1 * 25;
  check(`grade-A tomato 25kg shows correct co-op total (Rs ${expectedTotal})`,
        html.includes(A.money(expectedTotal)), `expected ${A.money(expectedTotal)} in output`);

  A.sell.grade = "B";
  const htmlB = A.viewSell();
  const expectedB = rate * 0.86 * 25;
  check(`grade-B applies the 0.86 multiplier correctly (Rs ${Math.round(expectedB)})`,
        htmlB.includes(A.money(expectedB)), `expected ${A.money(expectedB)} in output`);

  const middlemanEstimate = rate * 0.86 * 0.78 * 25;
  check("middleman comparison figure is strictly less than co-op figure",
        middlemanEstimate < expectedB, `${middlemanEstimate} vs ${expectedB}`);
}

console.log("\n=== offerRow() status tag rendering ===");
{
  // offers store `rate` at creation time (grade multiplier already applied
  // by the postBtn handler) — offerRow just does kg * o.rate, it does not
  // recompute the grade multiplier. Fixtures must include `rate` to match
  // the real data shape, or this test would be asserting against its own
  // wrong assumption rather than the app's actual (correct) behaviour.
  const lotOffer  = { crop: "cauliflower", kg: 40, grade: "A", status: "lot",  lot: "KVR-118", day: "आज",       rate: A.CROPS.cauliflower.rate };
  const paidOffer = { crop: "tomato",      kg: 25, grade: "B", status: "paid", lot: null,      day: "गत हप्ता",  rate: Math.round(A.CROPS.tomato.rate * 0.86) };
  const newOffer  = { crop: "onion",       kg: 10, grade: "C", status: "new",  lot: null,      day: "आज",       rate: Math.round(A.CROPS.onion.rate * 0.7) };
  check("lot status shows t-ok tag", A.offerRow(lotOffer).includes("t-ok"));
  check("paid status shows t-paid tag", A.offerRow(paidOffer).includes("t-paid"));
  check("new/waiting status shows t-wait tag", A.offerRow(newOffer).includes("t-wait"));
  check("offerRow displays kg x stored-rate correctly for the paid offer",
        A.offerRow(paidOffer).includes(A.money(25 * paidOffer.rate)),
        `expected ${A.money(25 * paidOffer.rate)} in output`);
}

console.log("\n=== Buyer/co-op views — must never throw ===");
{
  for (const [name, fn] of [["viewMarket", A.viewMarket], ["viewCart", A.viewCart], ["viewHow", A.viewHow],
                             ["viewPool", A.viewPool], ["viewRoute", A.viewRoute], ["viewBoard", A.viewBoard]]) {
    try {
      const html = fn();
      check(`${name}() returns a non-empty string`, typeof html === "string" && html.length > 0);
    } catch (e) {
      check(`${name}() does not throw`, false, e.message);
    }
  }
}

console.log("\n=== land-scan flow inside viewAdvice — scanPending must suppress stale recs ===");
{
  A.S.zone = "terai"; A.S.land = "pakho"; // deliberately mismatched from what the scan will produce
  A.scan.mode = "scan";
  A.scan.step = "texture"; // mid-flow, no result yet
  A.scan.result = null;
  const html = A.viewAdvice();
  check("mid-scan (no result yet) does not show 'अहिले लगाउने' recommendation header",
        !html.includes("अहिले लगाउन मिल्ने"),
        "recs section should be hidden while scan is pending, to avoid showing stale S.land-based results");
  A.scan.mode = "manual"; // reset for any tests that run after this one
}

(async function newTests(){
console.log("\n=== refreshOnce()/isStale() — background refresh must not re-render in a loop (2026-10-20) ===");
{
  // The old pattern was fetchX().then(()=>render()) called from INSIDE a
  // view function on every render: fetch helpers resolve from cache almost
  // immediately, so this was an infinite render->fetch->render microtask
  // loop that starved the event loop (it hung the JS integration test).
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  A.BACKEND.enabled = true;
  A.BACKEND.baseUrl = "http://127.0.0.1:9"; // nothing listens — all fetches fail fast
  evalInApp("L = 'ne'");

  // fresh cache: rendering must trigger ZERO fetches
  evalInApp("trendsCache = {data: {GOLB: [{avg_rate: 40}]}, at: Date.now()}");
  let trendsCalls = 0;
  sandbox.fetch = async (url) => { if (String(url).includes("/v1/prices/trends")) trendsCalls++; throw new Error("no server"); };
  const html1 = A.renderPriceTrends();
  check("fresh cache: renderPriceTrends() triggers no fetch", trendsCalls === 0, `got ${trendsCalls}`);
  check("fresh cache: still renders the trend section", html1.includes("भाउको ट्रेन्ड"));

  // stale cache: three renders must trigger exactly one fetch, then stop
  evalInApp("trendsCache = {data: null, at: 0}");
  trendsCalls = 0;
  sandbox.fetch = async (url) => { trendsCalls++; throw new Error("no server"); };
  A.viewToday(); A.viewToday(); A.viewToday();
  await sleep(100);
  check("stale cache: three renders trigger exactly one background fetch (no loop)", trendsCalls === 1, `got ${trendsCalls}`);
  check("isStale() is false on a just-filled cache", A.isStale({data: {x: 1}, at: Date.now()}, 120000) === false);
  check("isStale() is true when data is null", A.isStale({data: null, at: 0}, 120000) === true);

  // same guard for the rental + offers views
  let rentCalls = 0, offerCalls = 0, adminCalls = 0;
  sandbox.fetch = async (url) => {
    const u = String(url);
    if (u.includes("/v1/rentals")) rentCalls++;
    if (u.match(/\/v1\/offers(\?|$)/)) offerCalls++;
    throw new Error("no server");
  };
  evalInApp("rentalsCache = {data: null, at: 0}; myOffersCache = {data: null, at: 0}");
  Object.assign(A.account, {token: "t", role: "buyer", phone: "9841000001"});
  A.viewRental(); A.viewRental();
  A.viewMyOffers(); A.viewMyOffers();
  await sleep(100);
  check("viewRental() x2 triggers one rentals fetch, not a loop", rentCalls === 1, `got ${rentCalls}`);
  check("viewMyOffers() x2 triggers one offers fetch, not a loop", offerCalls === 1, `got ${offerCalls}`);
  A.BACKEND.enabled = false;
}

console.log("\n=== handlePostBtn() stores a canonical day marker so EN/NE toggling keeps working ===");
{
  A.BACKEND.enabled = true;
  A.BACKEND.baseUrl = "http://127.0.0.1:9";
  evalInApp("L = 'ne'; sell = {crop:'tomato', kg:20, grade:'A', posting:false}");
  Object.assign(A.account, {token: "t", phone: "9841000001"});
  sandbox.fetch = async (url) => {
    if (String(url).includes("/v1/listings")) return {ok: true, json: async () => ({listing_id: "L-1"})};
    throw new Error("no server");
  };
  await A.handlePostBtn();
  const posted = A.S.offers[0];
  check("posted offer stores day:'today' (canonical), not the localized string", posted.day === "today", `got ${JSON.stringify(posted.day)}`);
  check("posted offer is marked synced:true on server success", posted.synced === true);
  evalInApp("L = 'ne'");
  check("offerRow renders canonical 'today' as आज in Nepali", A.offerRow(posted).includes("आज"));
  evalInApp("L = 'en'");
  check("offerRow renders canonical 'today' as Today in English (no leaked Devanagari)", A.offerRow(posted).includes("Today") && !A.offerRow(posted).includes("आज"));
  check("offerRow does not crash on a corrupt crop key", (() => { try { A.offerRow({crop: "zzz", kg: 1, grade: "A", status: "new", lot: null, day: "today", rate: 1}); return true; } catch { return false; } })());
  evalInApp("L = 'ne'");
  A.BACKEND.enabled = false;
}

console.log("\n=== doPlaceOrder() keeps failed lines in the cart on partial success ===");
{
  A.BACKEND.enabled = true;
  A.BACKEND.baseUrl = "http://127.0.0.1:9";
  Object.assign(A.account, {token: "t", role: "buyer", phone: "9841000001", name: "Buyer"});
  evalInApp(`LOTS = [
    {id:'L1', crop:'tomato', kg:100, rate:50, coop:'c', coopEn:'c', farmers:1, day:'d', dayEn:'d', dist:'', distEn:''},
    {id:'L2', crop:'potato', kg:100, rate:40, coop:'c', coopEn:'c', farmers:1, day:'d', dayEn:'d', dist:'', distEn:''}
  ]`);
  A.S.cart = {L1: 10, L2: 5};
  const seen = [];
  sandbox.fetch = async (url, opts) => {
    const body = JSON.parse(opts.body);
    seen.push(body.lot_id);
    if (body.lot_id === "L1") return {ok: true, json: async () => ({})};
    return {ok: false, status: 409, json: async () => ({error: "oversold"})};
  };
  await A.doPlaceOrder();
  check("successful lot L1 leaves the cart", !("L1" in A.S.cart), JSON.stringify(A.S.cart));
  check("failed lot L2 stays in the cart for retry", A.S.cart.L2 === 5, JSON.stringify(A.S.cart));
  check("both lots were attempted", seen.includes("L1") && seen.includes("L2"), JSON.stringify(seen));
  A.BACKEND.enabled = false;
}

console.log("\n=== doSubmitOffer() sends the buyer token when logged in ===");
{
  A.BACKEND.enabled = true;
  A.BACKEND.baseUrl = "http://127.0.0.1:9";
  Object.assign(A.account, {token: "tok123", role: "buyer", phone: "9841000001", name: "Buyer"});
  evalInApp("S.offerDialog = {lotId: 'L1', rate: 55, qty: 10}");
  let authHeader = null, payload = null;
  const realGet = sandbox.document.getElementById;
  sandbox.document.getElementById = (id) =>
    id === "offerRate" ? {value: "55"} : id === "offerQty" ? {value: "10"} : realGet(id);
  sandbox.fetch = async (url, opts) => {
    authHeader = opts.headers["Authorization"] || null;
    payload = JSON.parse(opts.body);
    return {ok: true, json: async () => ({offer_id: 7})};
  };
  await A.doSubmitOffer();
  check("Authorization header carries the buyer token", authHeader === "Bearer tok123", `got ${authHeader}`);
  check("offer payload still carries buyer_phone", payload && payload.buyer_phone === "9841000001");
  check("offer dialog closes after a successful submit", evalInApp("S.offerDialog") === null);
  sandbox.document.getElementById = realGet;
  A.BACKEND.enabled = false;
}

console.log(`\n${passed} passed, ${failures} failed.`);
process.exit(failures > 0 ? 1 : 0);
})();
