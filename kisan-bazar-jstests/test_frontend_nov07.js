/*
 * Frontend JS tests for the 2026-11-07 daily-improve run (focus: frontend JS).
 * All exercise the REAL shipped code from kisan-bazar.html via the harness.
 *
 * One bug class, two symptoms:
 *   1. Typed input was wiped by the app's full-DOM re-render. render()
 *      rebuilds every input from state, but several inputs kept their
 *      value only in the DOM — so any render while the user was typing
 *      silently discarded what they typed:
 *        - crop-doctor symptoms: gone when a photo was added (the photo
 *          handler re-renders) or when a crop button was tapped;
 *        - Mine-tab register name: gone on a farmer/buyer role toggle;
 *        - auth modal phone / OTP code / register name: gone on any
 *          re-render (role toggle, failed attempt);
 *        - admin login username + password: gone on EVERY failed attempt
 *          (the inputs had no value attribute at all);
 *        - offer dialog rate/qty, admin counter rate, settle amount and
 *          reference, lot confirm rate: gone when a background refresh
 *          landed or the language was toggled mid-typing.
 *      Fix: one delegated `input` listener (syncDraftInput) keeps the
 *      state copy live, and every render function reads its value back
 *      from that state.
 *   2. fetchAdminOverview() had no in-flight guard: adminLoadState()
 *      fires on EVERY render while the overview is loading, so each
 *      render issued its own parallel /v1/admin/overview fetch — the
 *      same duplicate-fetch class as the account-page fix (2026-10-28).
 *      Fix: concurrent callers share one in-flight promise.
 *   3. New users could never finish registering. renderAccountPanel()
 *      checked the token before loginStep, so a verified phone with no
 *      profile yet (loginStep "register", token already set) was shown
 *      a false "Connected" and the register form was unreachable; and
 *      doAuthVerifyCode() swallowed the /v1/me 404, leaving a new
 *      modal user "logged in" with no server profile and no register
 *      form at all. Fix: register branch first in the panel; the modal
 *      routes a 404 to its register step.
 *
 * Run: node test_frontend_nov07.js
 */
const { A, sandbox, evalInApp } = require("./harness");

let passed = 0, failed = 0;
function check(label, cond, detail) {
  if (cond) { passed++; }
  else { failed++; console.log(`FAIL  ${label}${detail ? " — " + detail : ""}`); }
}

// A symptom string that appears NOWHERE in the shipped copy (the
// placeholder mentions "कालो दाग", so it must not be the probe).
const SYMPTOM = "पातको किनार खैरो-रातो हुँदैछ";

(async () => {

console.log("=== syncDraftInput(): typed values reach state ===");
{
  // --- crop-doctor symptoms ---
  evalInApp(`doctor.symptoms = "";`);
  check("pre-sync: viewDoctor has no trace of typed symptoms",
    !A.viewDoctor().includes(SYMPTOM));
  A.syncDraftInput({target: {id: "doctorSymptoms", value: SYMPTOM}});
  check("input event syncs symptoms into state", A.doctor.symptoms === SYMPTOM);
  check("re-render keeps the typed symptoms (photo-add / crop-tap path)",
    A.viewDoctor().includes(SYMPTOM));

  // --- Mine-tab register name ---
  evalInApp(`account.regName = "";`);
  A.syncDraftInput({target: {id: "regName", value: "Sarita Tamang"}});
  check("input event syncs register name into state", A.account.regName === "Sarita Tamang");
  A.BACKEND.enabled = true;
  evalInApp(`account.token = "tok"; account.loginStep = "register"; account.regRole = "farmer";`);
  check("re-render keeps the typed register name (role-toggle path)",
    A.renderAccountPanel().includes('value="Sarita Tamang"'));

  // --- auth modal: phone, OTP code, register name ---
  evalInApp(`account.showAuth = true; account.phone = ""; account.codeDraft = ""; account.name = ""; account.lastError = null;`);
  A.syncDraftInput({target: {id: "auth-phone", value: "9841000001"}});
  evalInApp(`account.loginStep = "phone";`);
  check("typed phone survives a modal re-render",
    A.renderAuthModal().includes('value="9841000001"'));
  A.syncDraftInput({target: {id: "auth-code", value: "123456"}});
  evalInApp(`account.loginStep = "code_sent"; account.phone = "9841000001";`);
  check("typed OTP code survives a modal re-render",
    A.renderAuthModal().includes('value="123456"'));
  A.syncDraftInput({target: {id: "auth-name", value: "Ram Bahadur"}});
  evalInApp(`account.loginStep = "register";`);
  check("typed register name survives a modal re-render",
    A.renderAuthModal().includes('value="Ram Bahadur"'));

  // --- admin login: username + password (previously had NO value attr) ---
  evalInApp(`account.adminUser = ""; account.adminPass = ""; account.loginStep = "admin";`);
  check("pre-fix shape: admin inputs carry a value attribute now",
    A.renderAuthModal().includes('id="admin-user"') && A.renderAuthModal().includes('id="admin-pass"'));
  A.syncDraftInput({target: {id: "admin-user", value: "admin"}});
  A.syncDraftInput({target: {id: "admin-pass", value: "kisan123"}});
  const adminModal = A.renderAuthModal();
  check("typed admin username survives a failed-login re-render",
    adminModal.includes('value="admin"'));
  check("typed admin password survives a failed-login re-render",
    adminModal.includes('value="kisan123"'));

  // --- offer dialog rate/qty ---
  evalInApp(`S.offerDialog = {lotId:"L1", rate:55, qty:10};`);
  evalInApp(`LOTS.push({id:"L1", crop:"tomato", kg:100, rate:55, coop:"Co-op", coopEn:"Co-op"})`);
  A.syncDraftInput({target: {id: "offerRate", value: "62"}});
  A.syncDraftInput({target: {id: "offerQty", value: "25"}});
  const dlg = A.renderOfferDialog();
  check("typed offer rate survives a re-render", dlg.includes('value="62"'));
  check("typed offer qty survives a re-render", dlg.includes('value="25"'));
  evalInApp(`LOTS = LOTS.filter(x=>x.id!=="L1"); S.offerDialog = null;`);

  // --- unrelated inputs are ignored ---
  evalInApp(`doctor.symptoms = "kept";`);
  A.syncDraftInput({target: {id: "chatInput", value: "hello"}});
  A.syncDraftInput({target: {}});
  A.syncDraftInput(null);
  check("unknown / malformed input events are ignored, no throw",
    A.doctor.symptoms === "kept");

  // --- reset modal/account state used above ---
  evalInApp(`account.showAuth = false; account.loginStep = "idle"; account.token = null;` +
            ` account.phone = ""; account.name = ""; account.codeDraft = "";` +
            ` account.regName = ""; account.adminUser = ""; account.adminPass = "";` +
            ` account.lastError = null; doctor.symptoms = "";`);
  A.BACKEND.enabled = false;
}

console.log("\n=== admin drafts: counter / settle / confirm inputs ===");
{
  A.BACKEND.enabled = true;
  Object.assign(A.account, {token: "tok", role: "staff", isStaff: true});

  // --- counter rate on the admin offers tab ---
  evalInApp(`adminOffersCache = {data:[{id:"OF1", buyer_name:"Hari", buyer_phone:"9841000003",` +
            ` lot_id:"MST-012", bid_rate_per_kg:100, quantity_kg:20, status:"pending"}], at: Date.now()};`);
  A.syncDraftInput({target: {id: "counter-OF1", value: "110"}});
  check("counter draft is stored", evalInApp(`S.adminDrafts["counter-OF1"]`) === "110");
  check("offers tab re-renders the typed counter rate",
    A.viewAdminOffers().includes('value="110"'));

  // --- settle amount + reference on the admin orders tab ---
  evalInApp(`adminCache = {data:{counts:{farmers:1,lots:0}, lots:[], farmers:[], crops:[],` +
            ` orders:[{id:"OR1", buyer_name:"Hari", buyer_phone:"9841000003", lot_id:"MST-012",` +
            ` quantity_kg:10, unit_rate:100, payment_status:"pending"},` +
            ` {id:"OR2", buyer_name:"Mina", buyer_phone:"9841000004", lot_id:"KVR-118",` +
            ` quantity_kg:5, unit_rate:80, payment_status:"pending"}]}, at: Date.now(), err:null};`);
  A.syncDraftInput({target: {id: "settle-amt-OR1", value: "950"}});
  A.syncDraftInput({target: {id: "settle-ref-OR1", value: "esewa-8821"}});
  const ordersHtml = A.viewAdminOrders();
  check("orders tab re-renders the typed settle amount", ordersHtml.includes('value="950"'));
  check("orders tab re-renders the typed settle reference", ordersHtml.includes('value="esewa-8821"'));
  check("untouched settle amount still defaults to the computed total (5kg x Rs 80)",
    ordersHtml.includes('value="400"'));

  // --- lot confirm rate ---
  evalInApp(`adminCache.data.lots = [{id:"MST-012", crop_code:"SYAU", pooled_kg:540, status:"forming"}];`);
  A.syncDraftInput({target: {id: "rate-MST-012", value: "138"}});
  check("lots tab re-renders the typed confirm rate",
    A.viewAdminLots().includes('value="138"'));

  // --- successful submit clears its draft ---
  const realFetch = sandbox.fetch;
  const realGet = sandbox.document.getElementById;
  sandbox.fetch = async () => ({ok: true, status: 200, json: async () => ({})});
  evalInApp(`adminCache = {data:null, at:0, err:null};`); // settle path invalidates it anyway
  sandbox.document.getElementById = (id) =>
    id === "settle-amt-OR1" ? {value: "950"} :
    id === "settle-method-OR1" ? {value: "esewa"} :
    id === "settle-ref-OR1" ? {value: "esewa-8821"} : realGet(id);
  await A.doSettleOrder("OR1");
  check("settle success clears the amount + reference drafts",
    evalInApp(`S.adminDrafts["settle-amt-OR1"]`) === undefined &&
    evalInApp(`S.adminDrafts["settle-ref-OR1"]`) === undefined);
  sandbox.document.getElementById = (id) => id === "rate-MST-012" ? {value: "138"} : realGet(id);
  await A.doConfirmLot("MST-012");
  check("confirm success clears the rate draft",
    evalInApp(`S.adminDrafts["rate-MST-012"]`) === undefined);

  sandbox.document.getElementById = realGet;
  sandbox.fetch = realFetch;
  evalInApp(`adminOffersCache = {data:null, at:0}; adminCache = {data:null, at:0, err:null};` +
            ` Object.assign(account, {token:null, role:"farmer", isStaff:false});`);
  A.BACKEND.enabled = false;
}

console.log("\n=== registration is reachable for a verified new phone ===");
{
  const realFetch = sandbox.fetch;
  const realGet = sandbox.document.getElementById;
  A.BACKEND.enabled = true;

  // --- Mine tab: token + loginStep "register" must show the form, not "Connected" ---
  evalInApp(`account.token = "tok"; account.loginStep = "register"; account.regRole = "farmer";` +
            ` account.phone = "9841000009"; account.lastError = null;`);
  const panelReg = A.renderAccountPanel();
  check("Mine tab shows the register form for a verified new phone",
    panelReg.includes('id="regName"'), panelReg.slice(0, 120));
  check("Mine tab does NOT claim Connected while registration is pending",
    !panelReg.includes("Connected") && !panelReg.includes("जोडिएको"));
  evalInApp(`account.loginStep = "idle";`);
  const panelDone = A.renderAccountPanel();
  check("Mine tab still shows Connected once registered (loginStep idle)",
    panelDone.includes("Connected") || panelDone.includes("जोडिएको"));
  check("Mine tab hides the register form once registered",
    !panelDone.includes('id="regName"'));

  // --- Mine-tab OTP flow: /v1/me 404 routes to the register form ---
  evalInApp(`account.phone = "9841000009"; account.loginStep = "code_sent";` +
            ` account.token = null; account.lastError = null; account.busy = false;` +
            ` account.name = ""; account.role = "farmer";`);
  sandbox.fetch = async (url) => {
    url = String(url);
    if (url.includes("/v1/auth/verify")) return {ok: true, status: 200, json: async () => ({token: "tokNEW"})};
    if (url.includes("/v1/me")) return {ok: false, status: 404, json: async () => ({error: "no profile"})};
    if (url.includes("/v1/farmers/")) return {ok: false, status: 404, json: async () => ({})};
    throw new Error("unexpected " + url);
  };
  await A.doVerifyCode("123456");
  check("Mine-tab verify: 404 on /v1/me sets loginStep to register",
    A.account.loginStep === "register", A.account.loginStep);
  check("Mine-tab verify: register form renders after the 404",
    A.renderAccountPanel().includes('id="regName"'));

  // --- Modal OTP flow: /v1/me 404 must open the modal register step ---
  evalInApp(`account.phone = "9841000008"; account.loginStep = "code_sent"; account.showAuth = true;` +
            ` account.token = null; account.lastError = null; account.busy = false;` +
            ` account.name = ""; account.role = "farmer"; account.pendingRole = "farmer";`);
  sandbox.document.getElementById = (id) => id === "auth-code" ? {value: "123456"} : realGet(id);
  sandbox.fetch = async (url) => {
    url = String(url);
    if (url.includes("/v1/auth/verify")) return {ok: true, status: 200, json: async () => ({token: "tokNEW2"})};
    if (url.includes("/v1/me")) return {ok: false, status: 404, json: async () => ({error: "no profile"})};
    throw new Error("unexpected " + url);
  };
  await A.doAuthVerifyCode();
  check("modal verify: 404 on /v1/me sets loginStep to register",
    A.account.loginStep === "register", A.account.loginStep);
  check("modal verify: modal stays open on the register step",
    A.account.showAuth === true);
  check("modal verify: register form renders with the name input",
    A.renderAuthModal().includes('id="auth-name"'));

  // --- ... and the register submit completes the journey ---
  sandbox.document.getElementById = (id) => id === "auth-name" ? {value: "New Farmer"} : realGet(id);
  sandbox.fetch = async (url) => {
    url = String(url);
    if (url.includes("/v1/auth/signup")) return {ok: true, status: 200, json: async () => ({ok: true})};
    throw new Error("unexpected " + url);
  };
  await A.doAuthRegister();
  check("modal register: profile name saved", A.account.name === "New Farmer", A.account.name);
  check("modal register: modal closes, loginStep back to idle",
    A.account.showAuth === false && A.account.loginStep === "idle");

  // --- existing user through the modal is unchanged ---
  evalInApp(`account.phone = "9841000001"; account.loginStep = "code_sent"; account.showAuth = true;` +
            ` account.token = null; account.lastError = null; account.busy = false;` +
            ` account.name = ""; account.role = "farmer";`);
  sandbox.fetch = async (url) => {
    url = String(url);
    if (url.includes("/v1/auth/verify")) return {ok: true, status: 200, json: async () => ({token: "tokOLD"})};
    if (url.includes("/v1/me")) return {ok: true, status: 200, json: async () => ({name: "Ram Bahadur", role: "buyer", isStaff: false})};
    throw new Error("unexpected " + url);
  };
  sandbox.document.getElementById = (id) => id === "auth-code" ? {value: "123456"} : realGet(id);
  await A.doAuthVerifyCode();
  check("modal verify: existing user is NOT sent to register",
    A.account.loginStep === "idle" && A.account.showAuth === false);
  check("modal verify: existing user role hydrated", A.account.role === "buyer");

  sandbox.document.getElementById = realGet;
  sandbox.fetch = realFetch;
  evalInApp(`Object.assign(account, {token:null, phone:"", name:"", role:"farmer",` +
            ` isStaff:false, loginStep:"idle", lastError:null, busy:false, showAuth:false});` +
            ` S.role = "farmer"; S.tab = "today";`);
  A.BACKEND.enabled = false;
}

console.log("\n=== fetchAdminOverview(): one in-flight fetch ===");
{
  const realFetch = sandbox.fetch;
  let calls = 0;
  const payload = {counts: {farmers: 1, lots: 0}, lots: [], orders: [], farmers: [], crops: []};
  sandbox.fetch = async () => {
    calls++;
    await new Promise(r => setTimeout(r, 30));
    return {ok: true, status: 200, json: async () => payload};
  };
  A.BACKEND.enabled = true;
  Object.assign(A.account, {token: "tok", isStaff: true});
  evalInApp(`adminCache = {data:null, at:0, err:null};`);

  // Two renders firing while the first fetch is still in flight.
  const p1 = A.fetchAdminOverview();
  const p2 = A.fetchAdminOverview();
  const [d1, d2] = await Promise.all([p1, p2]);
  check("two concurrent calls issue exactly ONE fetch", calls === 1, `calls=${calls}`);
  check("both callers get the same data", d1 === payload && d2 === payload);
  check("result is cached in adminCache", evalInApp(`adminCache.data === null`) === false);

  // Cached: a third call does not fetch again.
  await A.fetchAdminOverview();
  check("cached call issues no further fetch", calls === 1, `calls=${calls}`);

  // After the cache is cleared, a fresh call fetches again (no stuck promise).
  evalInApp(`adminCache = {data:null, at:0, err:null};`);
  await A.fetchAdminOverview();
  check("after cache clear, a new call fetches again", calls === 2, `calls=${calls}`);

  // A failed fetch releases the guard so the next call retries.
  evalInApp(`adminCache = {data:null, at:0, err:null};`);
  sandbox.fetch = async () => { calls++; return {ok: false, status: 500, json: async () => ({})}; };
  let threw = false;
  try { await A.fetchAdminOverview(); } catch (e) { threw = true; }
  check("failed fetch still rejects to the caller", threw);
  sandbox.fetch = async () => { calls++; return {ok: true, status: 200, json: async () => payload}; };
  await A.fetchAdminOverview();
  check("retry after failure fetches again", calls === 4, `calls=${calls}`);

  sandbox.fetch = realFetch;
  evalInApp(`adminCache = {data:null, at:0, err:null};` +
            ` Object.assign(account, {token:null, isStaff:false});`);
  A.BACKEND.enabled = false;
}

console.log(`\n${passed} passed, ${failed} failed.`);
process.exit(failed > 0 ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
