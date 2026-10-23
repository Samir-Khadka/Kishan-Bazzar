/*
 * View tests for the A+ residuals (2026-10-23, second pass):
 *   - viewRoute renders the REAL server-computed route for staff
 *     (no demo badge) and keeps the badged sample for everyone else;
 *   - viewAdminOrders renders payment status from the admin overview
 *     payload and doSettleOrder posts the settlement the staff typed.
 *
 * Run: node test_views_aplus.js
 */
const { A, sandbox, evalInApp } = require("./harness");

let passed = 0, failed = 0;
function check(name, cond, extra = "") {
  if (cond) { passed++; console.log(`  PASS — ${name}`); }
  else { failed++; console.log(`  FAIL — ${name} ${extra}`); }
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async function run() {
  console.log("\n=== viewRoute: real route for staff, badged demo otherwise ===");
  A.BACKEND.enabled = true;
  A.BACKEND.baseUrl = "http://127.0.0.1:9"; // fetches are stubbed below
  evalInApp("L = 'en'");

  // Non-staff: sample content, WITH the demo badge, zero route fetches.
  let routeCalls = 0;
  sandbox.fetch = async (url) => { if (String(url).includes("/v1/coop/route")) routeCalls++; throw new Error("no server"); };
  Object.assign(A.account, { token: "t", isStaff: false, role: "farmer", phone: "9841000001" });
  evalInApp("routeCache = {data:null, at:0, err:null}");
  const demoHtml = A.viewRoute();
  check("non-staff route view is the badged demo", demoHtml.includes("नमुना डेटा") && routeCalls === 0,
        `badge missing or ${routeCalls} route fetches`);

  // Staff: stub the endpoint with a real-shaped payload.
  const payload = {
    coop: { id: "kavre-veg", name: "Kavre Vegetable Co-op", lat: 27.6215, lon: 85.5442 },
    stops: [
      { kind: "pickup", seq: 1, phone: "9841000001", name: "Sarita Tamang", lat: 27.6215, lon: 85.5442, kg: 80, lots: ["KAV-106"], crops: ["KAULI"], leg_km: 0 },
      { kind: "pickup", seq: 2, phone: "9841000002", name: "Ram Bahadur K.C.", lat: 27.587, lon: 85.5192, kg: 50, lots: ["KAV-106"], crops: ["KAULI"], leg_km: 7.1 },
      { kind: "delivery", seq: 3, phone: "9841000003", name: "Mina Shrestha", lat: 27.632, lon: 85.522, kg: 40, orders: 1, leg_km: 5.2 },
    ],
    unlocated: { deliveries: [{ phone: "9841000999", name: "Ghost Buyer", kg: 10, orders: 1 }] },
    totals: { pickup_kg: 130, delivery_kg: 50, stops: 3, est_distance_km: 19.4, return_leg_km: 7.1 },
    distance_basis: "haversine straight-line estimates", source: "live data",
  };
  sandbox.fetch = async (url) => {
    if (String(url).includes("/v1/coop/route")) { routeCalls++; return { ok: true, json: async () => payload }; }
    throw new Error("no server");
  };
  Object.assign(A.account, { token: "t", isStaff: true, role: "staff", phone: "9801100000" });
  evalInApp("routeCache = {data:null, at:0, err:null}");
  A.viewRoute();           // first render: loading + background fetch
  await sleep(50);         // fetch resolves; refreshOnce re-renders (stubbed DOM)
  const realHtml = A.viewRoute();
  check("staff route view renders the real stops",
        realHtml.includes("Sarita Tamang") && realHtml.includes("Ram Bahadur K.C.") && realHtml.includes("Mina Shrestha"));
  check("staff route view carries NO demo badge",
        !realHtml.includes("Demo data") && !realHtml.includes("नमुना डेटा"));
  check("distances are labelled as straight-line estimates",
        realHtml.includes("Estimated straight-line distance"));
  check("unlocated buyer is listed honestly, not mapped",
        realHtml.includes("Ghost Buyer") && realHtml.includes("no registered location"));
  check("route fetched exactly once for two renders", routeCalls === 1, `got ${routeCalls}`);

  console.log("\n=== viewAdminOrders + doSettleOrder ===");
  evalInApp(`adminCache = {data: {
    farmers: [], listings: [], lots: [], crops: [],
    counts: {farmers: 0, listings: 0, lots: 0, orders: 2, total_kg: 0, pooled_kg: 0},
    orders: [
      {id: 7, buyer_name: "Hotel Buyer", buyer_phone: "9841000999", lot_id: "KAV-106",
       quantity_kg: 100, unit_rate: 45, created_at: "2026-10-23 10:00:00",
       payment_status: "unpaid", settled_amount: null, settlement_method: null},
      {id: 8, buyer_name: "Settled Buyer", buyer_phone: "9841000003", lot_id: "KAV-106",
       quantity_kg: 10, unit_rate: 45, created_at: "2026-10-23 09:00:00",
       payment_status: "settled", settled_amount: 450, settlement_method: "esewa",
       settlement_reference: "ESW-9", settled_by: "9801100000", settled_at: "2026-10-23 11:00:00"}
    ]}, at: Date.now(), err: null}`);
  const ordersHtml = A.viewAdminOrders();
  check("unpaid order renders a settle form", ordersHtml.includes('data-settle-order="7"') && ordersHtml.includes('id="settle-amt-7"'));
  check("unpaid order pre-fills the full total (100 x 45)", ordersHtml.includes('value="4500"'));
  check("settle form is honest: records a method, not an online payment",
        ordersHtml.includes("not an online payment"));
  check("settled order shows status + method, no form",
        ordersHtml.includes("ESW-9") && !ordersHtml.includes('data-settle-order="8"'));

  // doSettleOrder posts exactly what staff entered.
  let posted = null;
  sandbox.fetch = async (url, opts) => {
    if (String(url).includes("/settle")) { posted = { url: String(url), body: JSON.parse(opts.body), auth: opts.headers.Authorization }; return { ok: true, json: async () => ({ payment_status: "settled" }) }; }
    throw new Error("no server");
  };
  const realDoc = sandbox.document;
  sandbox.document = { ...realDoc, getElementById: (id) => {
    if (id === "settle-amt-7") return { value: "4500" };
    if (id === "settle-method-7") return { value: "khalti" };
    if (id === "settle-ref-7") return { value: "KH-77" };
    return realDoc.getElementById(id);
  }};
  await A.doSettleOrder("7");
  sandbox.document = realDoc;
  check("settle posts amount/method/reference with the staff token",
        posted && posted.url.endsWith("/v1/orders/7/settle")
        && posted.body.amount === 4500 && posted.body.method === "khalti"
        && posted.body.reference === "KH-77" && posted.auth === "Bearer t",
        JSON.stringify(posted));

  console.log("\n=== session expiry: 401 routes to login (no dead-end Retry); admin logout reachable ===");
  // `account` is REASSIGNED by the expiry/logout flows, so the
  // load-time A.account snapshot goes stale — read/write live state
  // through evalInApp from here on.
  const live = () => evalInApp("account");
  const setStaff = () => evalInApp(`account = {phone:"9801100000", name:"Admin", role:"staff",
    isStaff:true, token:"dead-token", loginStep:"idle", regRole:"farmer", codeDraft:"", busy:false, lastError:null}`);

  // — a 401 from the admin overview (the live bug: dead token, admin
  // panel showing "server 401" + Retry forever) —
  setStaff();
  evalInApp("S.role='admin'; S.tab='admin_overview'; adminCache={data:null, at:0, err:null}");
  sandbox.fetch = async () => ({ ok: false, status: 401, json: async () => ({ error: "invalid or expired token" }) });
  let expiryErr = null;
  try { await A.fetchAdminOverview(); } catch (e) { expiryErr = e; }
  check("overview 401 throws a session-expired error", !!expiryErr && expiryErr.sessionExpired === true, String(expiryErr));
  check("401 clears the stored session (token + staff flag)", live().token === null && live().isStaff === false);
  check("401 routes to the login screen with the bilingual notice",
        evalInApp("S.role") === "farmer" && evalInApp("S.tab") === "mine"
        && (live().lastError || "").includes("Session expired") && live().showAuth === true,
        `tab=${evalInApp("S.tab")} err=${live().lastError}`);
  check("401 leaves NO dead-end admin error state behind", evalInApp("adminCache.err") === null);
  check("after expiry the admin view is the login guard, not a Retry card",
        A.viewAdminOverview().includes("gotoLoginBtn") && !A.viewAdminOverview().includes("adminRetryBtn"));

  // — a transient 500 must NOT log anyone out: session + Retry stay —
  setStaff();
  evalInApp("adminCache={data:null, at:0, err:null}");
  sandbox.fetch = async () => ({ ok: false, status: 500, json: async () => ({}) });
  let err500 = null;
  try { await A.fetchAdminOverview(); } catch (e) { err500 = e; }
  check("overview 500 still throws 'server 500' (Retry territory)",
        !!err500 && String(err500.message).includes("server 500"), String(err500));
  check("500 does NOT clear the session", live().token === "dead-token" && live().isStaff === true);

  // — logout is rendered inside the admin panel itself —
  setStaff();
  evalInApp(`adminCache = {data: {farmers: [], listings: [], lots: [], crops: [], orders: [],
    counts: {farmers: 0, listings: 0, lots: 0, orders: 0, total_kg: 0, pooled_kg: 0}}, at: Date.now(), err: null}`);
  check("admin overview renders a logout button", A.viewAdminOverview().includes('id="adminLogoutBtn"'));
  check("admin orders renders a logout button", A.viewAdminOrders().includes('id="adminLogoutBtn"'));
  check("admin farmers renders a logout button", A.viewAdminFarmers().includes('id="adminLogoutBtn"'));

  // — doLogout clears everything and lands back home —
  A.doLogout();
  check("logout clears token/staff and returns to farmer home",
        live().token === null && live().isStaff === false
        && evalInApp("S.role") === "farmer" && evalInApp("S.tab") === "today");
  check("logout clears the admin cache", evalInApp("adminCache.data") === null);

  console.log("\n=== persistent header logout + admin loading/error states are never dead ends ===");
  // The header is static markup rendered in EVERY app state (all tabs,
  // loading, error) — a logout control there can never be trapped
  // inside an unreachable view the way the panel buttons were.
  const fs2 = require("fs"), path2 = require("path");
  const htmlSrc = fs2.readFileSync(path2.join(__dirname, "..", "kisan-bazar.html"), "utf8");
  const headerBlock = (htmlSrc.match(/<header class="top">[\s\S]*?<\/header>/) || [""])[0];
  check("always-rendered app header contains a logout control",
        headerBlock.includes('id="headerLogoutBtn"'));

  // header() toggles that control with the session. Drive it with a
  // per-id element registry (the harness stub returns a fresh fake
  // element per call, which can't be read back).
  const regEls = {};
  const regEl = () => ({ style: {}, dataset: {}, hidden: false, innerHTML: "",
    classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
    setAttribute(){}, getAttribute(){ return null; } });
  const origDoc = sandbox.document;
  sandbox.document = { ...origDoc, getElementById: (id) => (regEls[id] || (regEls[id] = regEl())) };
  A.header(); // session was cleared by the doLogout checks above
  check("header logout is hidden when logged out", regEls.headerLogoutBtn.hidden === true);
  setStaff();
  A.header();
  check("header logout is visible when a session exists", regEls.headerLogoutBtn.hidden === false);
  check("header logout carries the Log out label",
        (regEls.headerLogoutBtn.innerHTML || "").includes("Log out"),
        regEls.headerLogoutBtn.innerHTML);
  sandbox.document = origDoc;

  // Defense in depth: the admin panel's own loading/error states (the
  // "server 401 / Retry" card this bug was reported from) also render
  // the panel bar with its logout button.
  evalInApp("adminCache = {data:null, at:0, err:'server 401'}");
  check("admin ERROR state renders a logout button",
        A.viewAdminOverview().includes('id="adminLogoutBtn"'));
  evalInApp("adminCache = {data:null, at:0, err:null}");
  sandbox.fetch = () => new Promise(() => {}); // never resolves: stays in the loading state
  check("admin LOADING state renders a logout button",
        A.viewAdminOverview().includes('id="adminLogoutBtn"'));

  A.BACKEND.enabled = false;
  console.log(`\n${passed} passed, ${failed} failed.`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
