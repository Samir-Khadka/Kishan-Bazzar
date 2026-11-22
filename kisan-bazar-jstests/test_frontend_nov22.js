/*
 * Frontend JS tests for the 2026-11-22 daily-improve run (focus: frontend JS).
 * All exercise the REAL shipped code from kisan-bazar.html via the harness.
 *
 *   - Corrupt persisted state in localStorage used to blank the whole app
 *     on every load, with no recovery inside the app: an unknown kb:role
 *     threw in render() (TABS[role].some), an unknown kb:zone threw in
 *     viewToday() (WEATHER[zone].level), kb:offers=null threw on
 *     .filter, kb:cart=null threw in viewCart (Object.entries(null)), a
 *     numeric kb:accountName with a stored token threw in header()
 *     (.split), and a null element inside kb:offers threw on .status in
 *     viewToday/viewMine/assistantReply. render() now validates and
 *     repairs persisted state (falling back to defaults, dropping
 *     non-object offer rows) before anything reads it, and save()
 *     persists the repair — the same "corrupt data must not blank the
 *     app" rule as the 2026-11-14 offerRow fix.
 *   - A bogus kb:tab was already handled by render()'s existing guard;
 *     pinned here so the behaviour stays.
 *
 * Run: node test_frontend_nov22.js
 */
const { A, sandbox, evalInApp } = require("./harness");

let passed = 0, failed = 0;
function check(label, cond, detail) {
  if (cond) { passed++; }
  else { failed++; console.log(`FAIL  ${label}${detail ? " — " + detail : ""}`); }
}

(async () => {

console.log("=== render() — corrupt persisted role/tab repairs to defaults ===");
{
  A.S.role = "bogus"; A.S.tab = "today";
  let threw = false;
  try { A.render(); } catch (e) { threw = true; }
  check("bogus role: render() does not throw", !threw);
  check("bogus role: repaired to 'farmer'", A.S.role === "farmer");
  check("bogus role: tab reset to the role's first tab", A.S.tab === "today");
  check("bogus role: repair persisted to localStorage",
    sandbox.localStorage.getItem("kb:role") === JSON.stringify("farmer"));

  A.S.role = "buyer"; A.S.tab = "bogus";
  A.render();
  check("bogus tab: repaired to the buyer's first tab", A.S.tab === "market");

  // Valid values are left alone.
  A.S.role = "buyer"; A.S.tab = "cart";
  A.render();
  check("valid role/tab untouched", A.S.role === "buyer" && A.S.tab === "cart");
  A.S.role = "farmer"; A.S.tab = "today";
  A.render();
}

console.log("\n=== render() — corrupt persisted zone/land repair to defaults ===");
{
  A.S.zone = "bogus";
  let threw = false, today = "";
  try { A.render(); today = A.viewToday(); } catch (e) { threw = true; }
  check("bogus zone: render() does not throw", !threw);
  check("bogus zone: repaired to 'midhill'", A.S.zone === "midhill");
  check("bogus zone: Today view renders weather", today.includes("मौसम") || today.includes("Weather") || today.length > 100, today.slice(0, 80));

  A.S.land = "bogus";
  threw = false;
  try { A.render(); } catch (e) { threw = true; }
  check("bogus land: render() does not throw", !threw);
  check("bogus land: repaired to 'bari'", A.S.land === "bari");

  A.S.zone = "terai"; A.S.land = "khet";
  A.render();
  check("valid zone/land untouched", A.S.zone === "terai" && A.S.land === "khet");
  A.S.zone = "midhill"; A.S.land = "bari";
  A.render();
}

console.log("\n=== render() — corrupt persisted offers/cart repair ===");
{
  A.S.offers = null;
  let threw = false;
  try { A.render(); } catch (e) { threw = true; }
  check("null offers: render() does not throw", !threw);
  check("null offers: repaired to []", Array.isArray(A.S.offers) && A.S.offers.length === 0);

  const good = {id: 1, crop: "tomato", kg: 25, grade: "A", status: "paid", rate: 48, day: "today"};
  A.S.offers = [null, "junk", 42, good];
  threw = false;
  try { A.render(); } catch (e) { threw = true; }
  check("offers with null/non-object rows: render() does not throw", !threw);
  check("offers with null/non-object rows: only the real row survives",
    A.S.offers.length === 1 && A.S.offers[0] === good);
  let mine = "";
  try { mine = A.viewMine(); } catch (e) { threw = true; }
  check("Mine view renders after the repair", mine.includes("१,२००") || mine.includes("1200"), mine.slice(0, 80));

  A.S.cart = null;
  threw = false;
  try { A.render(); } catch (e) { threw = true; }
  check("null cart: render() does not throw", !threw);
  check("null cart: repaired to {}", A.S.cart && typeof A.S.cart === "object" && !Array.isArray(A.S.cart));
  A.S.cart = [5, 10];
  threw = false;
  try { A.render(); } catch (e) { threw = true; }
  check("array cart: render() does not throw", !threw);
  check("array cart: repaired to {}", A.S.cart && typeof A.S.cart === "object" && !Array.isArray(A.S.cart));
  A.S.cart = "not-a-cart";
  threw = false;
  try { A.render(); } catch (e) { threw = true; }
  check("string cart: render() does not throw", !threw);
  check("string cart: repaired to {}", A.S.cart && typeof A.S.cart === "object");

  // Valid state is left alone.
  A.S.offers = [good];
  A.S.cart = { "KVR-118": 10 };
  A.render();
  check("valid offers/cart untouched",
    A.S.offers.length === 1 && A.S.cart["KVR-118"] === 10);
  A.S.offers = [
    {id: 1, crop: "cauliflower", kg: 40, grade: "A", status: "lot", lot: "KVR-118", rate: 52, day: "today"},
    {id: 2, crop: "tomato", kg: 25, grade: "B", status: "paid", lot: "KVR-114", rate: 41, day: "last_week"}
  ];
  A.S.cart = {};
  A.render();
}

console.log("\n=== header() — non-string stored account name/phone ===");
{
  // The harness stub returns a fresh element per getElementById call, so
  // pin stable elements to observe what header() actually writes.
  const stable = {};
  const origGet = sandbox.document.getElementById;
  sandbox.document.getElementById = id => (stable[id] || (stable[id] = origGet(id)));

  A.account.token = "tok123";
  A.account.name = 42;
  A.account.phone = "9841000001";
  let threw = false;
  try { A.header(); } catch (e) { threw = true; }
  check("numeric stored name with a token: header() does not throw", !threw);
  // Falls back to the phone's last 4 digits instead of .split() on a number.
  check("numeric stored name: header shows phone last-4",
    typeof stable.authBtn.innerHTML === "string" && stable.authBtn.innerHTML.includes("0001"), String(stable.authBtn.innerHTML));

  A.account.name = "Sarita Tamang";
  A.header();
  check("string name still shows the first name",
    String(stable.authBtn.innerHTML).includes("Sarita"));
  sandbox.document.getElementById = origGet;
  A.account.token = null; A.account.name = ""; A.account.phone = "";
  A.render();
}

console.log(`\n${passed} passed, ${failed} failed.`);
process.exit(failed ? 1 : 0);
})();
