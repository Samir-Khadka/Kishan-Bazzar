const { A } = require("./harness");
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

console.log(`\n${passed} passed, ${failures} failed.`);
process.exit(failures > 0 ? 1 : 0);
