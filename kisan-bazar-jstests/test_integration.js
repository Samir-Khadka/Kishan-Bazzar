/*
 * REAL integration test. Not a mock, not a stub:
 *
 *   - starts the actual Flask API (same routes, auth, aggregation engine)
 *     as a subprocess on a local port;
 *   - runs the app's OWN shipped JavaScript (login + listing code from
 *     kisan-bazar.html) with Node's real fetch, over real HTTP;
 *   - then independently queries the API to confirm the server-side state
 *     matches what the app believes happened.
 *
 * What this proves: the app's client code and the API are compatible —
 * same crop vocabulary, same auth flow, same payload shapes — and the
 * aggregation engine really is reachable from the app's code path.
 *
 * What it does NOT prove: that a browser loading the published artifact
 * can reach a server. Node's fetch has no CORS enforcement and this API
 * runs on localhost inside this sandbox, not at a public URL. That last
 * hop is a deployment step; see section "CORS" below for the part of it
 * that CAN be checked here (the headers a browser would require).
 */
const { spawn } = require("child_process");
const fs = require("fs");
const http = require("http");
const os = require("os");
const path = require("path");
const { A, evalInApp } = require("./harness");

const PORT = 5071;
const BASE = `http://127.0.0.1:${PORT}`;
const OUTBOX = path.join(os.tmpdir(), "kisan_test_outbox.jsonl");
const API_DIR = path.join(__dirname, "..", "kisan-bazar-api");

let passed = 0, failures = 0;
function check(label, cond, detail) {
  if (cond) { passed++; console.log(`  ok    ${label}`); }
  else { failures++; console.log(`  FAIL  ${label}${detail ? " — " + detail : ""}`); }
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

function latestCode(phone) {
  const lines = fs.readFileSync(OUTBOX, "utf8").trim().split("\n").filter(Boolean);
  const mine = lines.map(l => JSON.parse(l)).filter(m => m.phone === phone);
  return mine.length ? mine[mine.length - 1].code : null;
}

async function waitForServer() {
  for (let i = 0; i < 50; i++) {
    try { const r = await fetch(`${BASE}/v1/market/lots`); if (r.ok) return true; } catch (e) {}
    await sleep(200);
  }
  return false;
}

// log in through the APP's own functions (not by calling the API directly)
async function loginViaApp(phone) {
  Object.assign(A.account, { phone: "", token: null, loginStep: "idle", codeDraft: "", busy: false, lastError: null });
  await A.doRequestCode(phone);
  const code = latestCode(phone);
  await A.doVerifyCode(code);
  return A.account.token;
}

async function listViaApp(crop, kg, grade) {
  evalInApp(`sell.crop=${JSON.stringify(crop)}; sell.kg=${kg}; sell.grade=${JSON.stringify(grade)}; sell.posting=false;`);
  await A.handlePostBtn();
  return A.S.offers[0];
}

function rawRequest(method, urlPath, headers) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: "127.0.0.1", port: PORT, path: urlPath, method, headers }, res => {
      let body = ""; res.on("data", d => body += d); res.on("end", () => resolve({ status: res.statusCode, headers: res.headers, body }));
    });
    req.on("error", reject); req.end();
  });
}

async function main() {
  const pyCmd = process.platform === "win32" ? "python" : "python3";
  const server = spawn(pyCmd, ["tests/run_test_server.py", String(PORT)], {
    cwd: API_DIR, env: { ...process.env, KISAN_TEST_OUTBOX: OUTBOX }, stdio: ["ignore", "pipe", "pipe"]
  });
  let serverLog = "";
  server.stdout.on("data", d => serverLog += d); server.stderr.on("data", d => serverLog += d);

  try {
    check("real Flask API started and is answering requests", await waitForServer(), serverLog.slice(-400));
    if (!await waitForServer()) throw new Error("server never came up");

    console.log("\n[1] Default state: backend disabled, nothing changes for a normal visitor");
    check("BACKEND.enabled is false by default", A.BACKEND.enabled === false);
    check("renderAccountPanel() renders nothing while disabled", A.renderAccountPanel() === "");
    check("viewMine() has no login UI while disabled", !A.viewMine().includes("phoneInput"));

    A.BACKEND.enabled = true;
    A.BACKEND.baseUrl = BASE;

    console.log("\n[2] Login screen appears once backend is enabled");
    check("phone input shown before login", A.renderAccountPanel().includes("phoneInput"));

    console.log("\n[3] Login error handling, using the app's own functions");
    await A.doRequestCode("123");
    check("too-short phone is rejected locally with an error message", A.account.lastError && A.account.loginStep === "idle");
    A.account.lastError = null;
    await A.doRequestCode("9841000001");
    check("valid phone -> real server accepted it, step is code_sent", A.account.loginStep === "code_sent", A.account.lastError);
    check("code input now shown", A.renderAccountPanel().includes("codeInput"));
    check("the code was 'SMS'd' (appeared in outbox), never in an HTTP response", !!latestCode("9841000001"));
    await A.doVerifyCode("12");
    check("5-digit code rejected locally", !!A.account.lastError && !A.account.token);
    await A.doVerifyCode("000000");
    check("wrong 6-digit code rejected by the REAL server, no token granted", !A.account.token && !!A.account.lastError, A.account.lastError);

    console.log("\n[4] Real login -> real token -> real listing");
    await A.doVerifyCode(latestCode("9841000001"));
    check("correct code -> app holds a real signed token", typeof A.account.token === "string" && A.account.token.includes("."));
    check("panel now shows connected state", A.renderAccountPanel().includes("logoutBtn"));

    const offer = await listViaApp("tomato", 25, "A");
    check("listing through app code is marked synced:true", offer.synced === true, JSON.stringify(offer));
    check("app recorded the server-assigned listing id", Number.isInteger(offer.remoteId), String(offer.remoteId));

    const mine = await (await fetch(`${BASE}/v1/listings/mine`, { headers: { Authorization: `Bearer ${A.account.token}` } })).json();
    const row = mine.find(l => l.id === offer.remoteId);
    check("server independently has that listing", !!row);
    check("server stored the shared crop code GOLB for tomato", row && row.crop_code === "GOLB", row && row.crop_code);
    check("server stored quantity 25 and grade A", row && row.quantity_kg === 25 && row.grade === "A");
    check("server attributes it to the logged-in farmer", row && row.farmer_phone === "9841000001");

    console.log("\n[5] Shared crop vocabulary: every crop the app offers is accepted by the API");
    let sweepFailures = [];
    for (const [crop, code] of Object.entries(A.CROP_API_CODE)) {
      try {
        await A.apiCreateListing(A.account.token, { farmer_phone: "9841000001", coop_id: A.BACKEND.coopId, crop_code: code, grade: "C", quantity_kg: 1 });
      } catch (e) { sweepFailures.push(`${crop}/${code}: ${e.message}`); }
    }
    check(`all ${Object.keys(A.CROP_API_CODE).length} app crops map to codes the API accepts`, sweepFailures.length === 0, sweepFailures.join("; "));

    console.log("\n[6] The aggregation engine, driven from app code");
    const o1 = await listViaApp("cauliflower", 38, "A");
    check("farmer 1: 38kg cauliflower -> synced, no lot yet", o1.synced === true && o1.status === "new");
    await loginViaApp("9841000002");
    const o2 = await listViaApp("cauliflower", 52, "A");
    check("farmer 2: 52kg (90kg total) -> still no lot", o2.synced === true && o2.status === "new");
    await loginViaApp("9841000005");
    const o5 = await listViaApp("cauliflower", 100, "A");
    check("farmer 5 (~50km away): 100kg -> synced but must NOT trigger a lot", o5.synced === true && o5.status === "new", JSON.stringify(o5));
    await loginViaApp("9841000004");
    const o4 = await listViaApp("cauliflower", 44, "A");
    check("farmer 4: 44kg tips the in-radius pool past 130kg -> app shows a formed lot", o4.status === "lot" && typeof o4.lot === "string", JSON.stringify(o4));

    // /v1/lots is paginated ({data, total, limit, offset}) — unwrap like the app's loadLots() does
    const lotsPayload = await (await fetch(`${BASE}/v1/lots?coop_id=kavre-veg`)).json();
    const lots = Array.isArray(lotsPayload) ? lotsPayload : (lotsPayload.data || []);
    const lot = lots.find(l => l.id === o4.lot);
    check("server confirms that lot exists, for cauliflower KAULI", lot && lot.crop_code === "KAULI", JSON.stringify(lots));
    check("server pooled exactly the 130kg target (not 134, not 234)", lot && lot.pooled_kg === 130, lot && String(lot.pooled_kg));

    console.log("\n[7] Authorization boundaries hold when driven from the app side");
    const farmerToken = A.account.token;
    let r = await fetch(`${BASE}/v1/lots/${o4.lot}/confirm`, { method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${farmerToken}` }, body: JSON.stringify({ rate_per_kg: 58 }) });
    check("a farmer's token cannot price a lot (403)", r.status === 403, String(r.status));
    const staffToken = await loginViaApp("9801100000");
    r = await fetch(`${BASE}/v1/lots/${o4.lot}/confirm`, { method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${staffToken}` }, body: JSON.stringify({ rate_per_kg: 58 }) });
    check("co-op staff (logged in via the same app flow) can price it (200)", r.status === 200, String(r.status));
    const market = await (await fetch(`${BASE}/v1/market/lots`)).json();
    check("priced lot now visible to buyers on the market endpoint", market.some(l => l.id === o4.lot));

    console.log("\n[8] Failure handling: the app degrades to local-only, never throws or lies");
    await loginViaApp("9841000003");
    const goodBase = A.BACKEND.baseUrl;
    A.BACKEND.baseUrl = "http://127.0.0.1:1"; // nothing listens here
    const offUnreach = await listViaApp("potato", 10, "B");
    check("unreachable server -> offer kept locally, flagged synced:false", offUnreach.synced === false, JSON.stringify(offUnreach));
    A.BACKEND.baseUrl = goodBase;
    const realToken = A.account.token;
    A.account.token = "not.a.real.token";
    const offBadTok = await listViaApp("potato", 10, "B");
    check("server rejects a bad token (401) -> offer kept locally, flagged synced:false", offBadTok.synced === false);
    A.account.token = realToken;
    const offOk = await listViaApp("potato", 10, "B");
    check("restored token works again", offOk.synced === true);

    A.account.token = null;
    A.BACKEND.baseUrl = "http://127.0.0.1:1";
    const offAnon = await listViaApp("onion", 5, "A");
    check("backend enabled but not logged in -> original local path, no network attempt, no badge",
          offAnon.synced === undefined && offAnon.status === "new");
    A.BACKEND.baseUrl = goodBase;

    console.log("\n[9] CORS: the headers a real browser would insist on");
    let pre = await rawRequest("OPTIONS", "/v1/listings", { Origin: "http://localhost:8080", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization, content-type" });
    check("preflight from an allowed origin gets Access-Control-Allow-Origin echoing it", pre.headers["access-control-allow-origin"] === "http://localhost:8080", JSON.stringify(pre.headers));
    check("preflight permits the Authorization header", (pre.headers["access-control-allow-headers"] || "").includes("Authorization"));
    pre = await rawRequest("OPTIONS", "/v1/listings", { Origin: "https://evil.example", "Access-Control-Request-Method": "POST" });
    check("preflight from an unlisted origin gets NO allow-origin header", !pre.headers["access-control-allow-origin"]);
    const noOrigin = await rawRequest("GET", "/v1/market/lots", {});
    check("normal same-origin/no-Origin request is unaffected", noOrigin.status === 200 && !noOrigin.headers["access-control-allow-origin"]);

    console.log("\n[10] Land-scan API wiring");
    // Re-login as a plain farmer to get a fresh farmer token.
    const farmerToken10 = await loginViaApp("9841000001");
    const phone10 = "9841000001";

    // 1. Save a scan via the app's new helper function directly.
    const saved = await A.apiSaveLandScan(farmerToken10, phone10, {
      land_type: "khet", confidence: "good",
      color_key: "dark", texture: "clay", drainage: "khet", had_photo: true
    });
    check("apiSaveLandScan: server accepted the scan and returned land_type khet",
          saved && saved.land_type === "khet", JSON.stringify(saved));
    check("apiSaveLandScan: color_key signal stored",
          saved && saved.color_key === "dark", saved && saved.color_key);
    check("apiSaveLandScan: had_photo stored as truthy (1)",
          saved && saved.had_photo === 1, saved && String(saved.had_photo));

    // 2. Read it back.
    const fetched = await A.apiGetLandScan(farmerToken10, phone10);
    check("apiGetLandScan: returns the scan just saved",
          fetched && fetched.land_type === "khet", JSON.stringify(fetched));
    check("apiGetLandScan: confidence round-trips correctly",
          fetched && fetched.confidence === "good", fetched && fetched.confidence);

    // 3. Save a second scan (bari) — GET should now return bari, not khet.
    await A.apiSaveLandScan(farmerToken10, phone10, {
      land_type: "bari", confidence: "ok",
      color_key: "mixed", texture: "mixed", drainage: "sometimes", had_photo: false
    });
    const fetched2 = await A.apiGetLandScan(farmerToken10, phone10);
    check("apiGetLandScan: most recent scan wins — now returns bari after second save",
          fetched2 && fetched2.land_type === "bari", fetched2 && fetched2.land_type);

    // 4. apiGetLandScan returns null (not an error) for a farmer with no scan yet.
    const farmerToken3 = await loginViaApp("9841000003");
    const noScan = await A.apiGetLandScan(farmerToken3, "9841000003");
    check("apiGetLandScan: returns null (not an error) when no scan on file for this farmer",
          noScan === null, JSON.stringify(noScan));

    // 5. Auth guard: forged token is rejected with 401.
    let saveRejected = false;
    try {
      await A.apiSaveLandScan("not.a.real.token", phone10, { land_type: "bari", confidence: "ok" });
    } catch (e) { saveRejected = true; }
    check("apiSaveLandScan: forged token is rejected by the server", saveRejected);

    // 6. Cross-farmer guard: farmer A's token cannot write farmer B's scan.
    let crossRejected = false;
    try {
      await A.apiSaveLandScan(farmerToken3, phone10, { land_type: "pakho", confidence: "ok" });
    } catch (e) { crossRejected = true; }
    check("apiSaveLandScan: farmer A's token cannot write farmer B's land scan (403)", crossRejected);

    // 7. doVerifyCode: after login the app restores S.land from the server
    //    when a scan is on file. Force a different starting value first.
    evalInApp("S.land = 'pakho';");
    Object.assign(A.account, { phone: "", token: null, loginStep: "idle", codeDraft: "", busy: false, lastError: null });
    await A.doRequestCode("9841000001");
    await A.doVerifyCode(latestCode("9841000001"));
    const landAfterLogin = evalInApp("S.land");
    check("doVerifyCode: S.land is hydrated from the server's latest scan (bari) after login",
          landAfterLogin === "bari", `got: ${landAfterLogin}`);


  } catch (e) {
    failures++; console.log("  FAIL  unexpected error:", e.message);
  } finally {
    server.kill("SIGTERM");
  }

  console.log(`\n${passed} passed, ${failures} failed.`);
  if (failures) console.log("--- server log tail ---\n" + serverLog.slice(-1200));
  process.exit(failures ? 1 : 0);
}
main();
