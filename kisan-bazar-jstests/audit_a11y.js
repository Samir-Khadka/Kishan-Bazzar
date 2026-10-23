/*
 * Accessibility + bilingual audit (Oct-2026 A+ pass, design residual).
 *
 * Method: jsdom renders the real DOM + the pages' own CSS; axe-core
 * 4.14 audits it. Headless Chrome cannot run on the dev VM, so this
 * is a DOM-level audit: it proves structure, labels, roles, alt text,
 * focusability and (via the explicit pair checks below) colour
 * contrast computed from the actual CSS custom-property values. It
 * does NOT prove visual layout, pixel rendering or screen-reader
 * behaviour — those still need a human pass on a real device.
 *
 * Coverage: all 10 public website pages; the app shell plus every
 * main view rendered by the app's own view functions (guest, farmer,
 * buyer, staff incl. the real-data route and the admin orders tab),
 * in the default language (Nepali) with spot checks in English.
 *
 * Gate: ZERO serious/critical axe violations; every declared
 * text/background contrast pair >= 4.5:1; every audited DOM carries
 * both Devanagari and Latin text. Exit 1 on any failure.
 *
 * Run: npm install (once, in this folder) && node audit_a11y.js
 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");
const axeSource = require("axe-core").source;

const ROOT = path.join(__dirname, "..");
const WEBSITE = path.join(ROOT, "website");
const sleep = ms => new Promise(r => setTimeout(r, ms));

let failures = 0;
const fail = msg => { failures++; console.log("  FAIL — " + msg); };
const pass = msg => console.log("  PASS — " + msg);

/* ---------------------------------------------------------- axe */
async function auditDom(dom, label) {
  const { window } = dom;
  await new Promise(res => {
    if (window.document.readyState === "complete") res();
    else window.addEventListener("load", res);
  });
  await sleep(30);
  window.eval(axeSource);
  const results = await window.axe.run(window.document, {
    resultTypes: ["violations"],
  });
  const bad = results.violations.filter(v =>
    v.impact === "serious" || v.impact === "critical");
  if (bad.length === 0) {
    pass(`${label}: 0 serious/critical axe violations ` +
         `(${results.violations.length} minor/moderate noted)`);
    for (const v of results.violations)
      console.log(`    note [${v.impact}] ${v.id}: ${v.help}`);
  } else {
    for (const v of bad) {
      fail(`${label}: [${v.impact}] ${v.id} — ${v.help}`);
      for (const n of v.nodes.slice(0, 4))
        console.log(`      ${n.target.join(" ")} :: ${(n.failureSummary || "").split("\n")[1] || ""}`);
    }
  }
  const text = window.document.body.textContent || "";
  const hasDeva = /[\u0900-\u097F]/.test(text);
  const hasLatin = /[A-Za-z]/.test(text);
  // The website's bilingual mechanism is data-ne/data-en attributes
  // swapped by site.js; English in data-en counts as present.
  const enAttrs = [...window.document.querySelectorAll("[data-en]")]
    .some(el => /[A-Za-z]/.test(el.getAttribute("data-en") || ""));
  if (hasDeva && (hasLatin || enAttrs))
    pass(`${label}: bilingual DOM (Devanagari visible; English ${hasLatin ? "visible" : "via data-en toggle"})`);
  else fail(`${label}: bilingual check — Devanagari=${hasDeva} Latin=${hasLatin} data-en=${enAttrs}`);
  const lang = window.document.documentElement.getAttribute("lang");
  if (lang) pass(`${label}: <html lang="${lang}">`);
  else fail(`${label}: <html> has no lang attribute`);
  return results;
}

/* ----------------------------------------------------- contrast */
function luminance(hex) {
  const c = hex.replace("#", "");
  const rgb = [0, 2, 4].map(i => parseInt(c.substr(i, 2), 16) / 255)
    .map(v => v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4));
  return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2];
}
function ratio(fg, bg) {
  const [a, b] = [luminance(fg), luminance(bg)];
  const [hi, lo] = a > b ? [a, b] : [b, a];
  return (hi + 0.05) / (lo + 0.05);
}
function cssVars(css, selectorRe) {
  const out = {};
  const blocks = css.match(selectorRe) || [];
  for (const block of blocks)
    for (const m of block.matchAll(/(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{6})/g))
      out[m[1]] = m[2];
  return out;
}
function checkPairs(vars, pairs, label) {
  for (const [fg, bg, what] of pairs) {
    if (!vars[fg] || !vars[bg]) { fail(`${label}: missing var ${fg}/${bg}`); continue; }
    const r = ratio(vars[fg], vars[bg]);
    if (r >= 4.5) pass(`${label}: ${what} ${fg} on ${bg} = ${r.toFixed(2)}:1`);
    else fail(`${label}: ${what} ${fg} on ${bg} = ${r.toFixed(2)}:1 (< 4.5)`);
  }
}

/* ---------------------------------------------------------- main */
(async function run() {
  console.log("=== Website pages (jsdom + axe-core) ===");
  const pages = ["index.html", "about.html", "contact.html", "how.html",
                 "login.html", "lot.html", "market.html", "privacy.html",
                 "terms.html", "404.html"];
  for (const page of pages) {
    const html = fs.readFileSync(path.join(WEBSITE, page), "utf8");
    const dom = new JSDOM(html, {
      url: "https://kishan-bazzar.onrender.com/site/" + page,
      resources: "usable", runScripts: "dangerously", pretendToBeVisual: true,
    });
    await auditDom(dom, `website/${page}`);
    dom.window.close();
  }

  console.log("\n=== App shell + rendered views (jsdom + axe-core) ===");
  const { A, sandbox, evalInApp } = require("./harness");
  sandbox.fetch = async () => { throw new Error("offline in audit"); };
  const appHtml = fs.readFileSync(path.join(ROOT, "kisan-bazar.html"), "utf8");
  const appDom = new JSDOM(appHtml, {
    url: "https://kishan-bazzar.onrender.com/",
    runScripts: "outside-only", pretendToBeVisual: true,
  });
  const doc = appDom.window.document;
  await auditDom(appDom, "app shell (as shipped)");

  // The app's bilingual model is one language at a time (ne default,
  // en via the toggle; render() also sets <html lang>). So a view is
  // bilingual iff its ne rendering carries Devanagari AND its en
  // rendering carries Latin — checked per view below, not by mixing.
  const viewEl = doc.querySelector("#main") || doc.body;
  const states = [
    ["guest today", () => { Object.assign(A.account, { token: null, isStaff: false, role: "guest" }); return A.viewToday(); }],
    ["guest market", () => A.viewMarket()],
    ["farmer sell", () => { Object.assign(A.account, { token: "t", isStaff: false, role: "farmer", phone: "9841000001", name: "Sarita" }); return A.viewSell(); }],
    ["buyer mine", () => { Object.assign(A.account, { role: "buyer" }); return A.viewMine(); }],
    ["farmer doctor", () => { Object.assign(A.account, { role: "farmer" }); return A.viewDoctor(); }],
    ["route (demo, non-staff)", () => A.viewRoute()],
  ];
  for (const [label, render] of states) {
    evalInApp("L = 'ne'");
    const neHtml = render();
    viewEl.innerHTML = neHtml;
    await auditDom(appDom, `app view: ${label}`);
    if (!/[\u0900-\u097F]/.test(neHtml)) fail(`app view ${label}: ne rendering has no Devanagari`);
    evalInApp("L = 'en'");
    const enHtml = render();
    if (!/[A-Za-z]/.test(enHtml)) fail(`app view ${label}: en rendering has no Latin text`);
    else pass(`app view: ${label}: renders in both ne (Devanagari) and en (Latin)`);
    evalInApp("L = 'ne'");
  }

  // Staff views need the admin cache + a stubbed live route payload.
  Object.assign(A.account, { token: "t", isStaff: true, role: "staff", phone: "9801100000" });
  evalInApp(`adminCache = {data: {
    farmers: [{phone: "9841000001", name: "Sarita Tamang", coop_name: "Kavre Vegetable Co-op", listing_count: 2, total_kg_listed: 130}],
    listings: [], lots: [{id: "KAV-106", crop_name_ne: "काउली", crop_name_en: "Cauliflower", grade: "A", pooled_kg: 130, target_kg: 130, status: "ready", confirmed_rate: 45, farmer_count: 2}],
    crops: [{code: "KAULI", name_ne: "काउली", name_en: "Cauliflower"}],
    counts: {farmers: 1, listings: 0, lots: 1, orders: 1, total_kg: 130, pooled_kg: 130},
    orders: [{id: 7, buyer_name: "Hotel Buyer", buyer_phone: "9841000999", lot_id: "KAV-106",
              quantity_kg: 100, unit_rate: 45, created_at: "2026-10-23 10:00:00",
              payment_status: "unpaid", settled_amount: null, settlement_method: null}]
  }, at: Date.now(), err: null}`);
  for (const [label, fn] of [["admin overview", "viewAdminOverview"], ["admin farmers", "viewAdminFarmers"],
                              ["admin lots", "viewAdminLots"], ["admin orders", "viewAdminOrders"],
                              ["admin rates", "viewAdminRates"]]) {
    viewEl.innerHTML = A[fn]();
    await auditDom(appDom, `app view: ${label}`);
  }
  const routePayload = {
    coop: { id: "kavre-veg", name: "Kavre Vegetable Co-op", lat: 27.6215, lon: 85.5442 },
    stops: [{ kind: "pickup", seq: 1, phone: "9841000001", name: "Sarita Tamang", lat: 27.6215, lon: 85.5442, kg: 80, lots: ["KAV-106"], crops: ["KAULI"], leg_km: 0 }],
    unlocated: { deliveries: [] },
    totals: { pickup_kg: 80, delivery_kg: 0, stops: 1, est_distance_km: 1.2, return_leg_km: 1.2 },
    distance_basis: "haversine straight-line estimates", source: "live data",
  };
  sandbox.fetch = async url => String(url).includes("/v1/coop/route")
    ? { ok: true, json: async () => routePayload } : { ok: false, json: async () => ({}) };
  evalInApp("routeCache = {data:null, at:0, err:null}");
  A.viewRoute();
  await sleep(60);
  viewEl.innerHTML = A.viewRoute();
  await auditDom(appDom, "app view: route (staff, real data)");
  appDom.window.close();

  console.log("\n=== Contrast pairs from the actual CSS values ===");
  const appCss = appHtml;
  const light = cssVars(appCss, /:root\s*\{[^}]*\}/g);
  const dark = { ...light, ...cssVars(appCss, /\[data-theme="dark"\]\s*\{[^}]*\}/g) };
  const appPairs = [
    ["--ink", "--paper", "body text"], ["--ink-2", "--paper", "secondary text"],
    ["--ink-3", "--paper", "meta text"], ["--ink", "--card", "card text"],
    ["--ink-2", "--card", "card secondary"], ["--ink-3", "--card", "card meta"],
    ["--paddy", "--paper", "brand/links"], ["--alert", "--paper", "alerts"],
    ["--alert", "--card", "alerts on cards"],
    ["--tori-text", "--paper", "pending amounts"], ["--tori-text", "--card", "pending on cards"],
  ];
  checkPairs(light, appPairs, "app light");
  checkPairs(dark, appPairs, "app dark");
  // Primary button: white text on --paddy.
  const btn = ratio("#ffffff", light["--paddy"]);
  if (btn >= 4.5) pass(`app light: button text #ffffff on --paddy = ${btn.toFixed(2)}:1`);
  else fail(`app light: button text #ffffff on --paddy = ${btn.toFixed(2)}:1 (< 4.5)`);
  // Selected chips / tori buttons: near-black text on the --tori accent.
  const chip = ratio("#241C00", light["--tori"]);
  if (chip >= 4.5) pass(`app light: chip text #241C00 on --tori = ${chip.toFixed(2)}:1`);
  else fail(`app light: chip text #241C00 on --tori = ${chip.toFixed(2)}:1 (< 4.5)`);

  const siteCss = fs.readFileSync(path.join(WEBSITE, "style.css"), "utf8");
  const siteVars = cssVars(siteCss, /:root\s*\{[^}]*\}/g);
  if (Object.keys(siteVars).length) {
    checkPairs(siteVars, [
      ["--ink", "--paper", "body text"], ["--ink-2", "--paper", "secondary text"],
      ["--ink", "--card", "card text"], ["--ink-2", "--card", "card secondary"],
      ["--paddy", "--paper", "brand/links"], ["--paddy-deep", "--paper", "deep brand"],
    ], "website");
    // Button label sits on the --tori background.
    const btnSite = ratio("#241C00", siteVars["--tori"]);
    if (btnSite >= 4.5) pass(`website: button text #241C00 on --tori = ${btnSite.toFixed(2)}:1`);
    else fail(`website: button text #241C00 on --tori = ${btnSite.toFixed(2)}:1 (< 4.5)`);
    // Hero stat numbers are white 28px bold (WCAG large text, >= 3:1)
    // on the hero gradient — checked against the gradient's lightest
    // stop (#388E3C), the worst case.
    const hero = ratio("#ffffff", "#388E3C");
    if (hero >= 3.0) pass(`website: hero stat #ffffff on lightest gradient stop = ${hero.toFixed(2)}:1 (large text >= 3:1)`);
    else fail(`website: hero stat #ffffff on lightest gradient stop = ${hero.toFixed(2)}:1 (< 3.0 large)`);
  } else {
    console.log("  note: website/style.css declares no hex custom properties to pair-check");
  }

  console.log(failures === 0 ? "\nAUDIT CLEAN" : `\nAUDIT FAILURES: ${failures}`);
  process.exit(failures ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
