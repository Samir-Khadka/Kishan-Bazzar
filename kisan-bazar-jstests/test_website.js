/*
 * Website login/account flow tests (2026-10-23, website/app separation):
 *   - website login success lands on account.html, NEVER on "/" (the app);
 *   - the website header chrome flips Login -> Account with a session;
 *   - account.html renders the signed-in identity + real listings/orders
 *     from the OTP-token endpoints, shows the staff notice for staff,
 *     and its Log out clears the session and returns to the website home;
 *   - account.html with no session bounces to the website login.
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
      storage: { "kb:accountToken": JSON.stringify("tok-1") }
    });
    run(loggedIn, "updateAuthNav()");
    check("logged in: entry point becomes Account -> account.html",
          a2.href === "account.html" && a2.textContent === "खाता" && a2.attrs["data-en"] === "Account",
          `${a2.href} ${a2.textContent} ${JSON.stringify(a2.attrs)}`);
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
    check("verify stores the shared session token",
          ctx.storageData["kb:accountToken"] === JSON.stringify("tok-website"),
          ctx.storageData["kb:accountToken"]);
    check("verify lands on account.html — NOT the app",
          ctx.sandbox.window.location.href === "account.html",
          ctx.sandbox.window.location.href);
    check("profile snapshot stored for the shared session",
          ctx.storageData["kb:accountName"] === JSON.stringify("Sarita Tamang"));
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
      storage: { "kb:accountToken": JSON.stringify("tok-website"), "kb:accountPhone": JSON.stringify("9841000001") }
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
    check("website Log out clears the session keys",
          !("kb:accountToken" in ctx.storageData) && !("kb:accountPhone" in ctx.storageData),
          JSON.stringify(Object.keys(ctx.storageData)));
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
      storage: { "kb:accountToken": JSON.stringify("tok-2"), "kb:accountPhone": JSON.stringify("9841000002") }
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
      storage: { "kb:accountToken": JSON.stringify("tok-staff"), "kb:accountPhone": JSON.stringify("9801100000") }
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

  console.log(`\n${passed} passed, ${failed} failed.`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
