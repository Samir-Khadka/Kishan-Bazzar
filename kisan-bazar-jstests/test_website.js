/*
 * Website login/account flow tests (2026-10-23, website/app separation):
 *   - website login success lands on account.html, NEVER on "/" (the app);
 *   - the website header chrome flips Login -> Account with a session;
 *   - account.html renders the signed-in identity + real listings/orders
 *     from the OTP-token endpoints, shows the staff notice for staff,
 *     and its Log out clears the session and returns to the website home;
 *   - account.html with no session bounces to the website login.
 *
 * Session separation (2026-10-23, second round): website and app
 * sessions are FULLY separate. The website session lives under its own
 * "kbw:account*" localStorage keys; website code must never read or
 * write the app's "kb:account*" keys:
 *   - website login writes ONLY kbw: keys (app keys stay absent);
 *   - an app-only session leaves the website logged OUT (chrome shows
 *     Login, account.html bounces to login);
 *   - a website-only session shows Account in the website chrome;
 *   - website logout clears kbw: keys and leaves app keys untouched.
 *
 * Method: the REAL website/site.js and the REAL inline scripts from
 * login.html / account.html run unmodified in a Node vm context with a
 * stubbed DOM/localStorage/fetch — the same technique as harness.js
 * for the app. Run: node test_website.js
 */
const vm = require("vm");
const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const WEBSITE = path.join(ROOT, "website");
const siteJs = fs.readFileSync(path.join(WEBSITE, "site.js"), "utf8");
const loginHtml = fs.readFileSync(path.join(WEBSITE, "login.html"), "utf8");
const accountHtml = fs.readFileSync(path.join(WEBSITE, "account.html"), "utf8");

function inlineScripts(html) {
  return [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
}

let passed = 0, failed = 0;
function check(name, cond, extra = "") {
  if (cond) { passed++; console.log(`  PASS — ${name}`); }
  else { failed++; console.log(`  FAIL — ${name} ${extra}`); }
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

function fakeEl() {
  const store = {};
  return new Proxy(store, {
    get(target, prop) {
      if (prop in target) return target[prop];
      if (prop === "style" || prop === "dataset") return (target[prop] = {});
      if (prop === "classList") return { add() {}, remove() {}, toggle() {}, contains() { return false; } };
      if (typeof prop === "string") {
        return (...args) => {
          if (prop === "querySelectorAll") return [];
          if (prop === "getAttribute" || prop === "querySelector" || prop === "closest") return null;
          return undefined;
        };
      }
      return undefined;
    },
    set(target, prop, value) { target[prop] = value; return true; }
  });
}

function makeContext({ storage = {}, fetchImpl, anchors = [] } = {}) {
  const data = { ...storage };
  const localStorage = {
    getItem: k => (k in data ? data[k] : null),
    setItem: (k, v) => { data[k] = String(v); },
    removeItem: k => { delete data[k]; }
  };
  const els = {};
  const elFor = id => (els[id] || (els[id] = fakeEl()));
  const sandbox = {
    console, Math, Date, JSON, Array, Object, String, Number, RegExp, Boolean,
    parseInt, parseFloat, isNaN, setTimeout, clearTimeout,
    localStorage,
    document: {
      getElementById: elFor,
      addEventListener: () => {},
      createElement: () => fakeEl(),
      querySelector: () => fakeEl(),
      querySelectorAll: sel =>
        (String(sel).includes("login.html") || String(sel).includes("account.html")) ? anchors : [],
      documentElement: fakeEl()
    },
    window: { location: { href: "" }, addEventListener: () => {}, scrollTo: () => {} },
    navigator: {},
    fetch: fetchImpl || (async () => { throw new Error("no fetch stub"); }),
    URLSearchParams, encodeURIComponent, decodeURIComponent
  };
  sandbox.window.localStorage = localStorage;
  vm.createContext(sandbox);
  vm.runInContext(siteJs, sandbox, { filename: "site.js" });
  return { sandbox, els, storageData: data };
}
const run = (ctx, code) => vm.runInContext(code, ctx.sandbox);

(async function main() {
  console.log("\n=== source: the forced website->app handoff is gone ===");
  check("login.html no longer redirects to \"/\" on success",
        !loginHtml.includes('window.location.href = "/"'));
  check("login.html redirects to account.html on success",
        loginHtml.includes('window.location.href = "account.html"'));
  check("account.html has a website Log out button",
        accountHtml.includes('id="web-logout"') && accountHtml.includes("doWebLogout()"));
  check("account.html keeps the explicit Open app entry (at /app, the app's own address)",
        accountHtml.includes('href="/app"') && accountHtml.includes("Open app"));

  console.log("\n=== source: website code never touches the app's session keys ===");
  {
    const webSources = { "site.js": siteJs, "login.html": loginHtml, "account.html": accountHtml };
    for (const [name, src] of Object.entries(webSources)) {
      const stripped = src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, "");
      check(`${name}: no reads/writes of the app's kb:account* keys`,
            !/localStorage\.(get|set|remove)Item\(\s*"kb:/.test(stripped) &&
            !/localStorage\.removeItem\(\s*"kb:/.test(stripped) &&
            !/"kb:"\s*\+/.test(stripped),
            "found a kb: localStorage access in executable code");
      check(`${name}: website session uses the kbw: namespace`,
            src.includes("kbw:"));
    }
  }

  console.log("\n=== website chrome: Login becomes Account with a session ===");
  {
    const mkAnchor = () => ({
      href: "login.html", textContent: "लगइन",
      attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }
    });
    const a1 = mkAnchor();
    const loggedOut = makeContext({ anchors: [a1] });
    run(loggedOut, "updateAuthNav()");
    check("logged out: entry point stays Login -> login.html",
          a1.href === "login.html" && a1.textContent === "लगइन", `${a1.href} ${a1.textContent}`);

    const a2 = mkAnchor();
    const loggedIn = makeContext({
      anchors: [a2],
      storage: { "kbw:accountToken": JSON.stringify("tok-1") }
    });
    run(loggedIn, "updateAuthNav()");
    check("logged in (website session): entry point becomes Account -> account.html",
          a2.href === "account.html" && a2.textContent === "खाता" && a2.attrs["data-en"] === "Account",
          `${a2.href} ${a2.textContent} ${JSON.stringify(a2.attrs)}`);

    const a3 = mkAnchor();
    const appOnly = makeContext({
      anchors: [a3],
      storage: { "kb:accountToken": JSON.stringify("tok-app"), "kb:accountPhone": JSON.stringify("9841000001") }
    });
    run(appOnly, "updateAuthNav()");
    check("app-only session: website chrome still shows Login (sessions are separate)",
          a3.href === "login.html" && a3.textContent === "लगइन", `${a3.href} ${a3.textContent}`);
    check("app-only session: webToken() ignores the app's token",
          run(appOnly, "webToken()") === null);
  }

  console.log("\n=== website login flow: OTP verify lands on account.html ===");
  {
    const fetchImpl = async (url, opts = {}) => {
      const u = String(url);
      if (u.includes("/v1/auth/request-code")) return { ok: true, status: 200, json: async () => ({ message: "sent" }) };
      if (u.includes("/v1/dev/latest-code/")) return { ok: true, status: 200, json: async () => ({ code: "123456" }) };
      if (u.includes("/v1/auth/verify")) return { ok: true, status: 200, json: async () => ({ token: "tok-website" }) };
      if (u.includes("/v1/me")) return { ok: true, status: 200, json: async () => ({ phone: "9841000001", name: "Sarita Tamang", role: "farmer", coop_id: "kavre-veg", isStaff: false }) };
      throw new Error("unexpected fetch " + u);
    };
    const ctx = makeContext({ fetchImpl });
    for (const s of inlineScripts(loginHtml)) vm.runInContext(s, ctx.sandbox, { filename: "login-inline.js" });
    ctx.els["login-phone"].value = "9841000001";
    await run(ctx, "sendOTP()");
    check("OTP step revealed after request-code",
          ctx.els["step-otp"].style.display === "block");
    check("demo code still surfaced on the website login (demo mode)",
          String(ctx.els["login-msg"].textContent || "").includes("123456"),
          String(ctx.els["login-msg"].textContent));
    ctx.els["login-otp"].value = "123456";
    await run(ctx, "verifyOTP()");
    check("verify stores the WEBSITE session token (kbw: namespace)",
          ctx.storageData["kbw:accountToken"] === JSON.stringify("tok-website"),
          ctx.storageData["kbw:accountToken"]);
    check("verify does NOT write the app's session keys (app stays signed out)",
          !("kb:accountToken" in ctx.storageData) && !("kb:accountPhone" in ctx.storageData) &&
          !("kb:accountName" in ctx.storageData) && !("kb:accountRole" in ctx.storageData) &&
          !("kb:accountIsStaff" in ctx.storageData),
          JSON.stringify(Object.keys(ctx.storageData)));
    check("verify lands on account.html — NOT the app",
          ctx.sandbox.window.location.href === "account.html",
          ctx.sandbox.window.location.href);
    check("profile snapshot stored in the WEBSITE session",
          ctx.storageData["kbw:accountName"] === JSON.stringify("Sarita Tamang"));
  }

  console.log("\n=== account page: identity + real listings/orders + logout ===");
  {
    const fetchImpl = async (url, opts = {}) => {
      const u = String(url);
      const auth = (opts.headers || {}).Authorization;
      if (auth !== "Bearer tok-website") return { ok: false, status: 401, json: async () => ({ error: "unauthorized" }) };
      if (u.includes("/v1/me")) return { ok: true, status: 200, json: async () => ({ phone: "9841000001", name: "Sarita Tamang", role: "farmer", coop_id: "kavre-veg", isStaff: false }) };
      if (u.includes("/v1/listings/mine")) return { ok: true, status: 200, json: async () => ([{ id: 3, crop_code: "KAULI", grade: "A", quantity_kg: 80, status: "open", created_at: "2026-10-23 09:00:00" }]) };
      if (u.includes("/v1/orders/mine")) return { ok: true, status: 200, json: async () => ([{ id: 5, lot_id: "KAV-106", quantity_kg: 10, unit_rate: 45, payment_status: "unpaid", created_at: "2026-10-23 10:00:00" }]) };
      throw new Error("unexpected fetch " + u);
    };
    const ctx = makeContext({
      fetchImpl,
      storage: {
        "kbw:accountToken": JSON.stringify("tok-website"), "kbw:accountPhone": JSON.stringify("9841000001"),
        // A coexisting APP session (e.g. the user signed into the app
        // separately) must survive a website logout untouched.
        "kb:accountToken": JSON.stringify("tok-app"), "kb:accountPhone": JSON.stringify("9841000001")
      }
    });
    for (const s of inlineScripts(accountHtml)) vm.runInContext(s, ctx.sandbox, { filename: "account-inline.js" });
    await sleep(30); // loadAccount() runs on script load
    const identity = String(ctx.els["account-identity"].innerHTML || "");
    check("identity renders name + phone + role from /v1/me",
          identity.includes("Sarita Tamang") && identity.includes("9841000001") && identity.includes("किसान"),
          identity);
    const listings = String(ctx.els["my-listings"].innerHTML || "");
    check("listings render the farmer's real listing",
          listings.includes("काउली") && listings.includes("80"), listings);
    const orders = String(ctx.els["my-orders"].innerHTML || "");
    check("orders render the buyer's real order with its total",
          orders.includes("KAV-106") && orders.includes("450"), orders);
    check("staff notice stays hidden for a farmer",
          ctx.els["staff-notice"].style.display !== "block");

    run(ctx, "doWebLogout()");
    check("website Log out clears the WEBSITE session keys",
          !("kbw:accountToken" in ctx.storageData) && !("kbw:accountPhone" in ctx.storageData),
          JSON.stringify(Object.keys(ctx.storageData)));
    check("website Log out leaves the APP session untouched",
          ctx.storageData["kb:accountToken"] === JSON.stringify("tok-app") &&
          ctx.storageData["kb:accountPhone"] === JSON.stringify("9841000001"),
          JSON.stringify(ctx.storageData));
    check("website Log out returns to the website home",
          ctx.sandbox.window.location.href === "index.html",
          ctx.sandbox.window.location.href);
  }

  console.log("\n=== account page: empty states are honest ===");
  {
    const fetchImpl = async (url) => {
      const u = String(url);
      if (u.includes("/v1/me")) return { ok: true, status: 200, json: async () => ({ phone: "9841000002", name: "New Buyer", role: "buyer", coop_id: "kavre-veg", isStaff: false }) };
      if (u.includes("/v1/listings/mine")) return { ok: true, status: 200, json: async () => ([]) };
      if (u.includes("/v1/orders/mine")) return { ok: true, status: 200, json: async () => ([]) };
      throw new Error("unexpected fetch " + u);
    };
    const ctx = makeContext({
      fetchImpl,
      storage: { "kbw:accountToken": JSON.stringify("tok-2"), "kbw:accountPhone": JSON.stringify("9841000002") }
    });
    for (const s of inlineScripts(accountHtml)) vm.runInContext(s, ctx.sandbox, { filename: "account-inline.js" });
    await sleep(30);
    check("no listings -> honest empty state",
          String(ctx.els["my-listings"].innerHTML || "").includes("कुनै लिस्टिङ छैन"),
          String(ctx.els["my-listings"].innerHTML));
    check("no orders -> honest empty state",
          String(ctx.els["my-orders"].innerHTML || "").includes("कुनै अर्डर छैन"),
          String(ctx.els["my-orders"].innerHTML));
  }

  console.log("\n=== account page: staff sees the app pointer, not a second panel ===");
  {
    const fetchImpl = async (url) => {
      const u = String(url);
      if (u.includes("/v1/me")) return { ok: true, status: 200, json: async () => ({ phone: "9801100000", name: "Co-op Admin", role: "staff", coop_id: "kavre-veg", isStaff: true }) };
      if (u.includes("/v1/listings/mine")) return { ok: true, status: 200, json: async () => ([]) };
      if (u.includes("/v1/orders/mine")) return { ok: true, status: 200, json: async () => ([]) };
      throw new Error("unexpected fetch " + u);
    };
    const ctx = makeContext({
      fetchImpl,
      storage: { "kbw:accountToken": JSON.stringify("tok-staff"), "kbw:accountPhone": JSON.stringify("9801100000") }
    });
    for (const s of inlineScripts(accountHtml)) vm.runInContext(s, ctx.sandbox, { filename: "account-inline.js" });
    await sleep(30);
    check("staff notice is shown for a staff token",
          ctx.els["staff-notice"].style.display === "block",
          String(ctx.els["staff-notice"].style.display));
  }

  console.log("\n=== account page: logged out bounces to the website login ===");
  {
    const ctx = makeContext({});
    for (const s of inlineScripts(accountHtml)) vm.runInContext(s, ctx.sandbox, { filename: "account-inline.js" });
    await sleep(10);
    check("no session -> redirect to login.html",
          ctx.sandbox.window.location.href === "login.html",
          ctx.sandbox.window.location.href);
  }
  {
    // Only an APP session exists (leftover/shared keys included): the
    // website must treat the user as logged out — the app session is
    // never adopted as a website session.
    const ctx = makeContext({
      storage: {
        "kb:accountToken": JSON.stringify("tok-app"), "kb:accountPhone": JSON.stringify("9841000001"),
        "kb:accountName": JSON.stringify("App User"), "kb:accountRole": JSON.stringify("farmer"),
        "kb:accountIsStaff": JSON.stringify(false)
      }
    });
    for (const s of inlineScripts(accountHtml)) vm.runInContext(s, ctx.sandbox, { filename: "account-inline.js" });
    await sleep(10);
    check("app-only session -> account.html still bounces to the website login",
          ctx.sandbox.window.location.href === "login.html",
          ctx.sandbox.window.location.href);
  }

  console.log("\n=== account page: concurrent loadAccount() calls share one load (2026-10-28) ===");
  {
    const counts = { me: 0, listings: 0, orders: 0 };
    const fetchImpl = async (url, opts = {}) => {
      const u = String(url);
      if (u.includes("/v1/me")) { counts.me++; return { ok: true, status: 200, json: async () => ({ phone: "9841000001", name: "Sarita Tamang", role: "farmer", coop_id: "kavre-veg", isStaff: false }) }; }
      if (u.includes("/v1/listings/mine")) { counts.listings++; return { ok: true, status: 200, json: async () => ([]) }; }
      if (u.includes("/v1/orders/mine")) { counts.orders++; return { ok: true, status: 200, json: async () => ([]) }; }
      throw new Error("unexpected fetch " + u);
    };
    const ctx = makeContext({
      fetchImpl,
      storage: { "kbw:accountToken": JSON.stringify("tok-website"), "kbw:accountPhone": JSON.stringify("9841000001") }
    });
    for (const s of inlineScripts(accountHtml)) vm.runInContext(s, ctx.sandbox, { filename: "account-inline.js" });
    // The DOMContentLoaded second call: site.js applyLang() invokes
    // loadAccount() again while the script-load call is still in flight.
    run(ctx, "loadAccount()");
    await sleep(30);
    check("script-load + DOMContentLoaded calls fetch /v1/me exactly once",
          counts.me === 1, JSON.stringify(counts));
    check("script-load + DOMContentLoaded calls fetch /v1/listings/mine exactly once",
          counts.listings === 1, JSON.stringify(counts));
    check("script-load + DOMContentLoaded calls fetch /v1/orders/mine exactly once",
          counts.orders === 1, JSON.stringify(counts));
    // After the load finished, a later call (language toggle) re-runs.
    run(ctx, "loadAccount()");
    await sleep(30);
    check("a later loadAccount() after completion re-runs (toggle re-render works)",
          counts.me === 2 && counts.listings === 2 && counts.orders === 2, JSON.stringify(counts));
  }

  console.log("\n=== source: website chat sends the website session, lot detail escapes (2026-10-28) ===");
  {
    const chatJs = fs.readFileSync(path.join(WEBSITE, "chat.js"), "utf8");
    const lotHtml = fs.readFileSync(path.join(WEBSITE, "lot.html"), "utf8");
    check("chat.js sends the website token (webToken) with an Authorization header to /v1/assistant/chat",
          chatJs.includes("webToken()") && chatJs.includes("Authorization") && chatJs.includes("/v1/assistant/chat"));
    check("chat.js never touches the app's kb: session keys",
          !/localStorage\.(get|set|remove)Item\(\s*"kb:/.test(chatJs.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, "")) &&
          !/"kb:"\s*\+/.test(chatJs));
    check("lot.html escapes server strings in the detail card",
          lotHtml.includes("function escHtml") && lotHtml.includes("escHtml(l.coop_name") && lotHtml.includes("escHtml(l.id)"));
    check("lot.html keeps a failed load failed (no eternal Loading on toggle)",
          lotHtml.includes("lotFailed") && lotHtml.includes("lotFailed = true"));
    check("account.html dedupes concurrent loads via a shared promise",
          accountHtml.includes("_accountLoadPromise") && accountHtml.includes("loadAccountInner"));
  }

  console.log("\n=== account page: mid-page 401 aborts the rest of the load (2026-11-04) ===");
  {
    // The token dies AFTER /v1/me succeeded (mid-page expiry): before
    // the fix, apiAuthed's bounce on /v1/listings/mine was swallowed
    // and the orders section fired one more authenticated request with
    // the dead token ("Bearer null") — a wasted fetch and a second
    // navigation to the same login page.
    const counts = { me: 0, listings: 0, orders: 0 };
    const fetchImpl = async (url, opts = {}) => {
      const u = String(url);
      if (u.includes("/v1/me")) { counts.me++; return { ok: true, status: 200, json: async () => ({ phone: "9841000001", name: "Sarita Tamang", role: "farmer", coop_id: "kavre-veg", isStaff: false }) }; }
      if (u.includes("/v1/listings/mine")) { counts.listings++; return { ok: false, status: 401, json: async () => ({ error: "unauthorized" }) }; }
      if (u.includes("/v1/orders/mine")) { counts.orders++; return { ok: false, status: 401, json: async () => ({ error: "unauthorized" }) }; }
      throw new Error("unexpected fetch " + u);
    };
    const ctx = makeContext({
      fetchImpl,
      storage: { "kbw:accountToken": JSON.stringify("tok-expired"), "kbw:accountPhone": JSON.stringify("9841000001") }
    });
    let bounceTo = null, bounceCount = 0;
    Object.defineProperty(ctx.sandbox.window.location, "href", {
      get() { return bounceTo; }, set(v) { bounceCount++; bounceTo = v; }, configurable: true
    });
    for (const s of inlineScripts(accountHtml)) vm.runInContext(s, ctx.sandbox, { filename: "account-inline.js" });
    await sleep(30);
    check("dead token mid-page: listings fetched once, orders never fetched",
          counts.listings === 1 && counts.orders === 0, JSON.stringify(counts));
    check("dead token mid-page: single bounce to login.html (no double navigation)",
          bounceCount === 1 && bounceTo === "login.html", `${bounceCount} -> ${bounceTo}`);
    check("dead token mid-page: the dead website session is cleared",
          !("kbw:accountToken" in ctx.storageData), JSON.stringify(Object.keys(ctx.storageData)));
    check("account.html source aborts the load after a mid-page 401 bounce",
          /String\(e\.message\) === "401"\) return;/.test(accountHtml));
  }

  console.log("\n=== account page: language toggle mid-load re-renders in the new language (2026-11-04) ===");
  {
    // The 2026-10-28 dedupe guard made a language toggle DURING the
    // load a no-op: the dynamic cards rendered in the pre-toggle
    // language while all static text followed the toggle.
    const counts = { me: 0, listings: 0, orders: 0 };
    let releaseMe, firstMe = true;
    const fetchImpl = async (url) => {
      const u = String(url);
      if (u.includes("/v1/me")) {
        counts.me++;
        if (firstMe) { firstMe = false; await new Promise(res => { releaseMe = res; }); }
        return { ok: true, status: 200, json: async () => ({ phone: "9841000001", name: "Sarita Tamang", role: "farmer", coop_id: "kavre-veg", isStaff: false }) };
      }
      if (u.includes("/v1/listings/mine")) { counts.listings++; return { ok: true, status: 200, json: async () => ([]) }; }
      if (u.includes("/v1/orders/mine")) { counts.orders++; return { ok: true, status: 200, json: async () => ([]) }; }
      throw new Error("unexpected fetch " + u);
    };
    const ctx = makeContext({
      fetchImpl,
      storage: { "kbw:accountToken": JSON.stringify("tok-website"), "kbw:accountPhone": JSON.stringify("9841000001") }
    });
    for (const s of inlineScripts(accountHtml)) vm.runInContext(s, ctx.sandbox, { filename: "account-inline.js" });
    await sleep(10); // script-load call is now held inside /v1/me
    run(ctx, "LANG = 'en'");  // user toggles NE -> EN mid-load ...
    run(ctx, "loadAccount()"); // ... and applyLang() re-renders while in flight
    releaseMe();               // the first load's /v1/me resolves
    await sleep(60);
    const listings = String(ctx.els["my-listings"].innerHTML || "");
    check("toggle mid-load re-runs the load in the new language",
          counts.me === 2 && counts.listings === 2 && counts.orders === 2, JSON.stringify(counts));
    check("dynamic cards render in the toggled language (EN, not NE)",
          listings.includes("No listings yet.") && !listings.includes("कुनै लिस्टिङ छैन"), listings);
  }

  console.log("\n=== source: chat.js drops duplicate sends while one is in flight (2026-11-04) ===");
  {
    const chatJs = fs.readFileSync(path.join(WEBSITE, "chat.js"), "utf8");
    check("chat.js guards send() with an in-flight flag",
          chatJs.includes("let sending = false;") &&
          /if\s*\(!text\s*\|\|\s*sending\)\s*return;/.test(chatJs) &&
          /finally\s*\{\s*sending = false;/.test(chatJs));
    const catchIdx = chatJs.indexOf("}catch(e){");
    const restoreIdx = chatJs.indexOf("input.value = text;");
    const sorryIdx = chatJs.indexOf("Sorry, could not connect.");
    check("chat.js restores the typed message when a send fails (retry without retyping)",
          catchIdx !== -1 && restoreIdx > catchIdx && restoreIdx !== -1 && sorryIdx !== -1 && restoreIdx < sorryIdx);
  }

  console.log("\n=== website login: double-tap on the OTP steps fires only once (2026-11-08) ===");
  {
    // request-code mints a new code and RETIRES the previous one, so a
    // double-tapped "Send OTP" left the user typing a code that no
    // longer verifies (401) — and burned 2 of the 3-per-10-min
    // allowance. A double-tapped verify fired a second POST whose 401
    // landed after the redirect. Both steps now drop extra taps while
    // one request is in flight; a failed attempt stays retryable.
    const counts = { code: 0, verify: 0, dev: 0 };
    let releaseCode, releaseVerify, holdCode = true, holdVerify = true;
    const fetchImpl = async (url, opts = {}) => {
      const u = String(url);
      if (u.includes("/v1/auth/request-code")) {
        counts.code++;
        if (holdCode) await new Promise(res => { releaseCode = res; });
        return { ok: true, status: 200, json: async () => ({ message: "sent" }) };
      }
      if (u.includes("/v1/dev/latest-code/")) { counts.dev++; return { ok: true, status: 200, json: async () => ({ code: "123456" }) }; }
      if (u.includes("/v1/auth/verify")) {
        counts.verify++;
        if (holdVerify) await new Promise(res => { releaseVerify = res; });
        return { ok: true, status: 200, json: async () => ({ token: "tok-website" }) };
      }
      if (u.includes("/v1/me")) return { ok: true, status: 200, json: async () => ({ phone: "9841000001", name: "Sarita Tamang", role: "farmer", coop_id: "kavre-veg", isStaff: false }) };
      throw new Error("unexpected fetch " + u);
    };
    const ctx = makeContext({ fetchImpl });
    for (const s of inlineScripts(loginHtml)) vm.runInContext(s, ctx.sandbox, { filename: "login-inline.js" });
    ctx.els["login-phone"].value = "9841000001";
    run(ctx, "sendOTP()");
    run(ctx, "sendOTP()"); // double-tap while the first request is in flight
    await sleep(10);
    check("double-tap on Send OTP mints exactly one code", counts.code === 1, JSON.stringify(counts));
    releaseCode(); holdCode = false;
    await sleep(30);
    check("OTP step revealed after the single mint",
          ctx.els["step-otp"].style.display === "block");
    check("demo code shown is the one live code (123456)",
          String(ctx.els["login-msg"].textContent || "").includes("123456"),
          String(ctx.els["login-msg"].textContent));
    // A later send (after the first settled) still works — the guard
    // is an in-flight guard, not a one-shot lock.
    run(ctx, "sendOTP()");
    await sleep(30);
    check("a later Send OTP after settle mints again (retryable)",
          counts.code === 2, JSON.stringify(counts));
    ctx.els["login-otp"].value = "123456";
    run(ctx, "verifyOTP()");
    run(ctx, "verifyOTP()"); // double-tap while the first verify is in flight
    await sleep(10);
    check("double-tap on Log in verifies exactly once", counts.verify === 1, JSON.stringify(counts));
    releaseVerify(); holdVerify = false;
    await sleep(30);
    check("verify still lands on account.html",
          ctx.sandbox.window.location.href === "account.html",
          ctx.sandbox.window.location.href);
    // Going back to the phone step clears the typed code: it belongs
    // to the previous number and can only 401 against a new one.
    ctx.els["login-otp"].value = "123456";
    run(ctx, "backToPhone()");
    check("back to phone clears the typed OTP code",
          ctx.els["login-otp"].value === "", JSON.stringify(ctx.els["login-otp"].value));
  }

  console.log("\n=== site.js: market lots fetch shared across concurrent callers (2026-11-08) ===");
  {
    // applyLang() re-renders the market list / home preview on every
    // NE<->EN toggle, so rapid toggling fired a parallel
    // /v1/market/lots fetch per toggle — and a failed fetch resolving
    // after a good one overwrote the cards with an error. Concurrent
    // callers now share one in-flight fetch; a later call after it
    // settles fetches fresh data, and a failure stays retryable.
    const lots = [{ id: "KAV-101", crop_code: "GOLB", pooled_kg: 120, status: "ready", rate_per_kg: 45 }];
    const counts = { lots: 0 };
    let releaseLots, holdLots = true;
    const fetchImpl = async (url) => {
      if (String(url).includes("/v1/market/lots")) {
        counts.lots++;
        if (holdLots) await new Promise(res => { releaseLots = res; });
        return { ok: true, status: 200, json: async () => lots };
      }
      throw new Error("unexpected fetch " + url);
    };
    const ctx = makeContext({ fetchImpl });
    run(ctx, "loadMarketLots(document.getElementById('market-lots'))");
    run(ctx, "loadLotPreview()");
    await sleep(10);
    check("concurrent market + preview calls share one /v1/market/lots fetch",
          counts.lots === 1, JSON.stringify(counts));
    releaseLots(); holdLots = false;
    await sleep(30);
    check("market list renders the shared lots",
          String(ctx.els["market-lots"].innerHTML || "").includes("गोलभेँडा"),
          String(ctx.els["market-lots"].innerHTML || ""));
    check("home preview renders the same shared lots",
          String(ctx.els["lot-preview"].innerHTML || "").includes("गोलभेँडा"),
          String(ctx.els["lot-preview"].innerHTML || ""));
    run(ctx, "loadMarketLots(document.getElementById('market-lots'))");
    await sleep(30);
    check("a later call after settle fetches fresh data",
          counts.lots === 2, JSON.stringify(counts));
  }
  {
    // A failed fetch must not poison the shared promise: the next
    // call retries instead of replaying the error.
    const counts = { lots: 0 };
    let failFirst = true;
    const fetchImpl = async (url) => {
      if (String(url).includes("/v1/market/lots")) {
        counts.lots++;
        if (failFirst) { failFirst = false; throw new Error("network down"); }
        return { ok: true, status: 200, json: async () => ([{ id: "KAV-101", crop_code: "GOLB", pooled_kg: 120, status: "ready", rate_per_kg: 45 }]) };
      }
      throw new Error("unexpected fetch " + url);
    };
    const ctx = makeContext({ fetchImpl });
    run(ctx, "loadMarketLots(document.getElementById('market-lots'))");
    await sleep(30);
    check("failed market fetch renders the load error, not a crash",
          String(ctx.els["market-lots"].innerHTML || "").includes("लोड हुन सकेन"),
          String(ctx.els["market-lots"].innerHTML || ""));
    run(ctx, "loadMarketLots(document.getElementById('market-lots'))");
    await sleep(30);
    check("a call after a failure retries the fetch (no cached error)",
          counts.lots === 2, JSON.stringify(counts));
    check("the retry renders the lots",
          String(ctx.els["market-lots"].innerHTML || "").includes("गोलभेँडा"),
          String(ctx.els["market-lots"].innerHTML || ""));
  }

  console.log(`\n${passed} passed, ${failed} failed.`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
