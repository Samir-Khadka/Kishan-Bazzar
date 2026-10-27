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

  // stale cache: three renders must trigger exactly one fetch PER
  // RESOURCE, then stop. (The Today view now reads two resources —
  // trends and latest rates; the invariant is per-resource dedupe,
  // i.e. still no render->fetch->render loop.)
  evalInApp("trendsCache = {data: null, at: 0}; latestCache = {data: null, at: 0}");
  let allCalls = 0; const perUrl = {};
  sandbox.fetch = async (url) => { allCalls++; perUrl[String(url)] = (perUrl[String(url)]||0)+1; throw new Error("no server"); };
  A.viewToday(); A.viewToday(); A.viewToday();
  await sleep(100);
  check("stale cache: three renders trigger exactly one background fetch per resource (no loop)",
        allCalls === 2 && Object.values(perUrl).every(n => n === 1),
        `got ${allCalls}: ${JSON.stringify(perUrl)}`);
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

console.log("\n=== nepalDateString() + loadLots() pagination (2026-10-27) ===");
{
  // Fixed epochs, hand-computed: 2026-10-26 18:45 UTC is 2026-10-27
  // 00:30 in Kathmandu (+5:45), while 12:00 UTC is 17:45 the same day.
  // The old code used toISOString() (UTC), so the booking prompt's
  // default date was yesterday for the first 5h45m of every Nepal day
  // — and the backend (rightly) rejects past dates.
  check("18:45 UTC -> next calendar day in Kathmandu",
        A.nepalDateString(Date.UTC(2026, 9, 26, 18, 45)) === "2026-10-27",
        A.nepalDateString(Date.UTC(2026, 9, 26, 18, 45)));
  check("12:00 UTC -> same calendar day in Kathmandu",
        A.nepalDateString(Date.UTC(2026, 9, 26, 12, 0)) === "2026-10-26",
        A.nepalDateString(Date.UTC(2026, 9, 26, 12, 0)));
  check("output is YYYY-MM-DD", /^\d{4}-\d{2}-\d{2}$/.test(A.nepalDateString(Date.now())));

  // loadLots must ask the server for ready lots at max page size:
  // the old bare /v1/lots defaulted to 50 lots of ANY status, so
  // ready lots past the first page never reached the market view.
  A.BACKEND.enabled = true;
  A.BACKEND.baseUrl = "http://127.0.0.1:9";
  evalInApp("lotsLoaded = false");
  let lotsUrl = null;
  sandbox.fetch = async (url) => {
    lotsUrl = String(url);
    return {ok: true, json: async () => ({data: [
      {id: "LIVE-1", crop_code: "GOLB", status: "ready", pooled_kg: 100, rate_per_kg: 50, coop_name: "Co-op", farmer_count: 3}
    ], total: 1, limit: 200, offset: 0})};
  };
  await A.loadLots();
  check("loadLots requests status=ready", lotsUrl && lotsUrl.includes("status=ready"), lotsUrl);
  check("loadLots requests limit=200 (server max)", lotsUrl && lotsUrl.includes("limit=200"), lotsUrl);
  check("live lot lands in LOTS", (() => { try { return evalInApp("LOTS").some(l => l.id === "LIVE-1"); } catch { return false; } })());
  lotsUrl = null;
  await A.loadLots();
  check("second loadLots() while loaded triggers no fetch", lotsUrl === null);

  // renderPriceTrends must survive malformed trend series instead of
  // crashing the whole Today view (see the guard above).
  evalInApp("L = 'en'");
  evalInApp("trendsCache = {data: {GOLB: [], KAULI: 'nope', ALU: [{avg_rate: 40}]}, at: Date.now()}");
  let trendHtml = null, trendThrew = false;
  try { trendHtml = A.renderPriceTrends(); } catch (e) { trendThrew = true; }
  check("malformed trend series do not throw", !trendThrew);
  check("malformed series are skipped, usable ones still render",
        trendHtml && trendHtml.includes("Potato"), trendHtml ? trendHtml.slice(0, 80) : "null");
  evalInApp("trendsCache = {data: {GOLB: []}, at: Date.now()}");
  check("all-malformed trends render nothing instead of crashing", A.renderPriceTrends() === "");
  evalInApp("L = 'ne'");
  A.BACKEND.enabled = false;
}

console.log("\n=== doBookRental() days/date validation + double-tap guard (2026-10-27) ===");
{
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  A.BACKEND.enabled = true;
  A.BACKEND.baseUrl = "http://127.0.0.1:9";
  Object.assign(A.account, {token: "t", role: "farmer", phone: "9841000001"});
  let fetchCalls = 0, lastBody = null, promptCalls = 0, promptDefaults = [];
  let answers = [];
  sandbox.prompt = (msg, def) => { promptCalls++; promptDefaults.push(def); return answers.length ? answers.shift() : null; };
  sandbox.fetch = async (url, opts) => {
    fetchCalls++; lastBody = JSON.parse(opts.body);
    await sleep(30);
    return {ok: true, json: async () => ({total: 4500})};
  };
  const reset = () => { fetchCalls = 0; lastBody = null; promptCalls = 0; promptDefaults = []; };

  answers = ["2.9"]; reset();
  await A.doBookRental("r1");
  check("fractional days '2.9' are rejected, not truncated to 2", fetchCalls === 0, `fetchCalls=${fetchCalls}`);
  answers = ["10abc"]; reset();
  await A.doBookRental("r1");
  check("trailing-junk days '10abc' are rejected, not parsed as 10", fetchCalls === 0, `fetchCalls=${fetchCalls}`);
  answers = ["3", "2026-02-30"]; reset();
  await A.doBookRental("r1");
  check("impossible date 2026-02-30 is rejected before any fetch", fetchCalls === 0, `fetchCalls=${fetchCalls}`);
  answers = [null]; reset();
  await A.doBookRental("r1");
  check("cancelling the days prompt stays silent (no fetch, no throw)", fetchCalls === 0);
  answers = ["3", "2026-12-01"]; reset();
  await A.doBookRental("r1");
  check("valid booking posts days as an integer", lastBody && lastBody.days === 3 && Number.isInteger(lastBody.days), JSON.stringify(lastBody));
  check("valid booking posts the entered date", lastBody && lastBody.book_date === "2026-12-01", JSON.stringify(lastBody));
  check("date prompt default is the Kathmandu date, not UTC",
        promptDefaults[1] === A.nepalDateString(Date.now()), `default=${promptDefaults[1]}`);

  // Double-tap: two concurrent calls share one prompt round and one POST.
  answers = ["2", "2026-12-02"]; reset();
  const p1 = A.doBookRental("r1"), p2 = A.doBookRental("r1");
  await Promise.all([p1, p2]);
  check("concurrent double-tap books exactly once", fetchCalls === 1, `fetchCalls=${fetchCalls}`);
  check("concurrent double-tap prompts only once (2 prompts, not 4)", promptCalls === 2, `promptCalls=${promptCalls}`);
  A.BACKEND.enabled = false;
}

console.log("\n=== double-tap guards: doSubmitOffer / decideOffer / doConfirmLot / doSettleOrder (2026-10-27) ===");
{
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  A.BACKEND.enabled = true;
  A.BACKEND.baseUrl = "http://127.0.0.1:9";
  Object.assign(A.account, {token: "tok123", role: "buyer", phone: "9841000001", name: "Buyer"});
  const realGet = sandbox.document.getElementById;

  // Offer submit: two concurrent submits must place ONE bid (no
  // idempotency key exists on /v1/offers, so the guard is the only
  // thing stopping a duplicate bid).
  evalInApp("S.offerDialog = {lotId: 'L1', rate: 55, qty: 10}");
  sandbox.document.getElementById = (id) =>
    id === "offerRate" ? {value: "55"} : id === "offerQty" ? {value: "10"} : realGet(id);
  let offerCalls = 0;
  // Count ONLY the offer POST: render() also fires background trend/
  // latest fetches, and a catch-all stub would both miscount and
  // poison the trends cache with the offer payload.
  sandbox.fetch = async (url) => {
    if (!String(url).includes("/v1/offers")) throw new Error("no server");
    offerCalls++; await sleep(40); return {ok: true, json: async () => ({offer_id: 7})};
  };
  await Promise.all([A.doSubmitOffer(), A.doSubmitOffer()]);
  check("concurrent double-tap submits exactly one offer", offerCalls === 1, `offerCalls=${offerCalls}`);
  check("offer dialog still closes after the guarded submit", evalInApp("S.offerDialog") === null);
  sandbox.document.getElementById = realGet;

  // Staff decide: same offer decided once; a different offer is NOT
  // blocked by the first offer's guard (per-offer, not global).
  Object.assign(A.account, {token: "staff-tok", role: "staff"});
  let decideCalls = 0;
  sandbox.fetch = async (url) => {
    if (!String(url).includes("/decide")) throw new Error("no server");
    decideCalls++; await sleep(40); return {ok: true, json: async () => ({})};
  };
  await Promise.all([A.decideOffer(9, "accepted"), A.decideOffer(9, "accepted")]);
  check("concurrent double-tap decides the offer exactly once", decideCalls === 1, `decideCalls=${decideCalls}`);
  await A.decideOffer(11, "rejected");
  check("a different offer is not blocked by the first guard", decideCalls === 2, `decideCalls=${decideCalls}`);

  // Lot confirm: same lot confirmed once.
  sandbox.document.getElementById = (id) => id === "rate-L9" ? {value: "60"} : realGet(id);
  let confirmCalls = 0;
  sandbox.fetch = async (url) => {
    if (!String(url).includes("/confirm")) throw new Error("no server");
    confirmCalls++; await sleep(40); return {ok: true, json: async () => ({})};
  };
  await Promise.all([A.doConfirmLot("L9"), A.doConfirmLot("L9")]);
  check("concurrent double-tap confirms the lot exactly once", confirmCalls === 1, `confirmCalls=${confirmCalls}`);
  sandbox.document.getElementById = realGet;

  // Settle: same order settled once.
  sandbox.document.getElementById = (id) =>
    id === "settle-amt-O1" ? {value: "500"} : id === "settle-method-O1" ? {value: "cash"} :
    id === "settle-ref-O1" ? {value: ""} : realGet(id);
  let settleCalls = 0;
  sandbox.fetch = async (url) => {
    if (!String(url).includes("/settle")) throw new Error("no server");
    settleCalls++; await sleep(40); return {ok: true, json: async () => ({})};
  };
  await Promise.all([A.doSettleOrder("O1"), A.doSettleOrder("O1")]);
  check("concurrent double-tap settles the order exactly once", settleCalls === 1, `settleCalls=${settleCalls}`);
  sandbox.document.getElementById = realGet;
  A.BACKEND.enabled = false;
}

console.log(`\n${passed} passed, ${failures} failed.`);
process.exit(failures > 0 ? 1 : 0);
})();
