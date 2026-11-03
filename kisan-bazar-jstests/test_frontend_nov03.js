/*
 * Frontend JS tests for the 2026-11-03 daily-improve run (focus: frontend JS).
 * All exercise the REAL shipped code from kisan-bazar.html via the harness.
 *
 *   - cartStep(): the market +5/-5 buttons used to read l.kg on a lot that
 *     no longer exists in LOTS (stale button after loadLots() swapped the
 *     data), throwing a TypeError that killed the whole click handler.
 *   - doVerifyCode(): a non-404 /v1/me failure (500 / dropped connection)
 *     used to fall through to the "Connected" toast with no profile —
 *     silent success while the role tabs stayed wrong.
 *   - Strict Number() parsing on money inputs: parseFloat("80abc") silently
 *     bid/settled/confirmed 80 (same class as the rental-days fix).
 *
 * Run: node test_frontend_nov03.js
 */
const { A, sandbox, evalInApp } = require("./harness");

let passed = 0, failed = 0;
function check(label, cond, detail) {
  if (cond) { passed++; }
  else { failed++; console.log(`FAIL  ${label}${detail ? " — " + detail : ""}`); }
}

(async () => {

console.log("=== cartStep() — stale-lot guard ===");
{
  evalInApp(`LOTS.push({id:"TESTCART-1", crop:"tomato", kg:100, rate:50})`);
  evalInApp(`delete S.cart["TESTCART-1"]; delete S.cart["NOPE-1"]; delete S.cart["NOKG-1"];`);

  check("valid lot: +5 steps the cart up",
    A.cartStep("TESTCART-1", 5) === true && evalInApp(`S.cart["TESTCART-1"]`) === 5);
  check("valid lot: -5 steps the cart down",
    A.cartStep("TESTCART-1", -5) === true && evalInApp(`S.cart["TESTCART-1"]`) === 0);

  evalInApp(`S.cart["TESTCART-1"] = 98`);
  A.cartStep("TESTCART-1", 5);
  check("clamps at the lot's available kg", evalInApp(`S.cart["TESTCART-1"]`) === 100);

  evalInApp(`S.cart["TESTCART-1"] = 3`);
  A.cartStep("TESTCART-1", -5);
  check("clamps at zero, never negative", evalInApp(`S.cart["TESTCART-1"]`) === 0);

  check("stale id: returns false instead of throwing",
    A.cartStep("NOPE-1", 5) === false);
  check("stale id: cart untouched", evalInApp(`S.cart["NOPE-1"]`) === undefined);

  evalInApp(`LOTS.push({id:"NOKG-1", crop:"tomato"})`);  // kg missing
  check("lot with no kg: returns false instead of zeroing",
    A.cartStep("NOKG-1", 5) === false);
  check("lot with no kg: cart untouched", evalInApp(`S.cart["NOKG-1"]`) === undefined);

  evalInApp(`LOTS = LOTS.filter(x=>x.id!=="TESTCART-1" && x.id!=="NOKG-1");` +
           `delete S.cart["TESTCART-1"];`);
}

console.log("\n=== doVerifyCode(): /v1/me failure is no longer a silent success ===");
{
  const realFetch = sandbox.fetch;
  A.BACKEND.enabled = true;
  evalInApp(`account.phone = "9841000001"; account.loginStep = "code_sent";` +
            `account.token = null; account.lastError = null; account.busy = false;`);

  // --- 500 on /v1/me right after a good OTP verify ---
  sandbox.fetch = async (url) => {
    url = String(url);
    if (url.includes("/v1/auth/verify")) return {ok:true, status:200, json: async()=>({token:"tok123"})};
    if (url.includes("/v1/me")) return {ok:false, status:500, json: async()=>({error:"boom"})};
    if (url.includes("/v1/farmers/")) return {ok:false, status:404, json: async()=>({})};
    throw new Error("unexpected " + url);
  };
  await A.doVerifyCode("123456");
  check("token kept (the OTP verify itself succeeded)", A.account.token === "tok123");
  check("failure is surfaced in lastError, not swallowed",
    !!A.account.lastError && A.account.lastError.includes("500"), A.account.lastError);
  check("loginStep stays idle (session alive, retry is logout/login)", A.account.loginStep === "idle");
  check("role NOT silently defaulted into buyer tabs", A.account.role === "farmer");

  // --- the happy path still behaves exactly as before ---
  evalInApp(`account.token = null; account.lastError = null; account.loginStep = "code_sent";` +
            `account.name = ""; account.role = "farmer";`);
  sandbox.fetch = async (url) => {
    url = String(url);
    if (url.includes("/v1/auth/verify")) return {ok:true, status:200, json: async()=>({token:"tokABC"})};
    if (url.includes("/v1/me")) return {ok:true, status:200, json: async()=>({name:"Ram Bahadur", role:"buyer", isStaff:false})};
    if (url.includes("/v1/farmers/")) return {ok:false, status:404, json: async()=>({})};
    throw new Error("unexpected " + url);
  };
  await A.doVerifyCode("654321");
  check("happy path: no error surfaced", A.account.lastError === null, String(A.account.lastError));
  check("happy path: profile hydrated", A.account.role === "buyer" && A.account.name === "Ram Bahadur");
  check("happy path: role tabs follow the account", evalInApp("S.role") === "buyer");

  sandbox.fetch = realFetch;
  A.BACKEND.enabled = false;
  evalInApp(`Object.assign(account, {token:null, phone:"", name:"", role:"farmer",` +
            ` isStaff:false, loginStep:"idle", lastError:null, busy:false});` +
            ` S.role = "farmer"; S.tab = "today";`);
}

console.log("\n=== strict Number() parsing on money inputs ===");
{
  const realGet = sandbox.document.getElementById;
  const realFetch = sandbox.fetch;
  let calls = [];
  sandbox.fetch = async (url) => {
    calls.push(String(url));
    return {ok:true, status:200, json: async()=>({})};
  };
  A.BACKEND.enabled = true;
  Object.assign(A.account, {token:"tok123", role:"buyer", phone:"9841000001", name:"Buyer"});

  // --- doSubmitOffer ---
  evalInApp(`S.offerDialog = {lotId:"L1", rate:55, qty:10}`);
  sandbox.document.getElementById = (id) =>
    id === "offerRate" ? {value:"80abc"} : id === "offerQty" ? {value:"10"} : realGet(id);
  calls = [];
  await A.doSubmitOffer();
  check("offer with trailing-garbage rate is rejected (no POST)",
    !calls.some(u => u.includes("/v1/offers")));
  check("offer dialog stays open after rejection", evalInApp("S.offerDialog !== null"));
  sandbox.document.getElementById = (id) =>
    id === "offerRate" ? {value:"80"} : id === "offerQty" ? {value:"10"} : realGet(id);
  await A.doSubmitOffer();
  check("clean rate still submits the offer", calls.some(u => u.includes("/v1/offers")));

  // --- doConfirmLot ---
  sandbox.document.getElementById = (id) => id === "rate-L9" ? {value:"50x"} : realGet(id);
  calls = [];
  await A.doConfirmLot("L9");
  check("confirm with trailing-garbage rate is rejected (no POST)",
    !calls.some(u => u.includes("/confirm")));
  sandbox.document.getElementById = (id) => id === "rate-L9" ? {value:"50"} : realGet(id);
  await A.doConfirmLot("L9");
  check("clean rate still confirms the lot", calls.some(u => u.includes("/v1/lots/L9/confirm")));

  // --- doSettleOrder ---
  sandbox.document.getElementById = (id) =>
    id === "settle-amt-O9" ? {value:"100abc"} :
    id === "settle-method-O9" ? {value:"cash"} :
    id === "settle-ref-O9" ? {value:""} : realGet(id);
  calls = [];
  await A.doSettleOrder("O9");
  check("settle with trailing-garbage amount is rejected (no POST)",
    !calls.some(u => u.includes("/settle")));
  sandbox.document.getElementById = (id) =>
    id === "settle-amt-O9" ? {value:"100"} :
    id === "settle-method-O9" ? {value:"cash"} :
    id === "settle-ref-O9" ? {value:""} : realGet(id);
  await A.doSettleOrder("O9");
  check("clean amount still settles the order", calls.some(u => u.includes("/v1/orders/O9/settle")));

  sandbox.document.getElementById = realGet;
  sandbox.fetch = realFetch;
  A.BACKEND.enabled = false;
  evalInApp(`Object.assign(account, {token:null, role:"farmer"}); S.offerDialog = null;`);
}

console.log(`\n${passed} passed, ${failed} failed.`);
process.exit(failed > 0 ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
