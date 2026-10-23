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

  A.BACKEND.enabled = false;
  console.log(`\n${passed} passed, ${failed} failed.`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
