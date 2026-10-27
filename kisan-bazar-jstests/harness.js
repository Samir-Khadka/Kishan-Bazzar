/*
 * Runs the REAL script extracted from kisan-bazar.html inside a minimal
 * stubbed DOM (via Node's vm module), then exercises the app's internal
 * functions directly — not a reimplementation, the actual code that ships.
 *
 * Trick: top-level `const`/`let` bindings in a vm-executed script do NOT
 * become properties of the context object, so they'd normally be
 * invisible from outside. We get around that by appending one line to
 * the SAME script (so it shares the same lexical scope) that copies
 * everything we want to inspect onto `globalThis.__test`.
 */
const vm = require("vm");
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(path.join(__dirname, "..", "kisan-bazar.html"), "utf8");
const scriptMatch = html.match(/<script>([\s\S]*)<\/script>/);
if (!scriptMatch) { console.error("Could not extract <script> from kisan-bazar.html"); process.exit(1); }
const appJs = scriptMatch[1];

// ---- minimal DOM stubs: enough that the app's render() doesn't throw ----
function fakeEl() {
  const store = {};
  return new Proxy(store, {
    get(target, prop) {
      if (prop in target) return target[prop];
      if (prop === "style" || prop === "dataset") return (target[prop] = {});
      if (prop === "classList") return { add(){}, remove(){}, toggle(){}, contains(){return false;} };
      if (prop === "children") return [];
      if (typeof prop === "string") {
        return (...args) => {
          if (prop === "querySelectorAll") return [];
          if (prop === "getAttribute" || prop === "closest" || prop === "querySelector") return null;
          return undefined;
        };
      }
      return undefined;
    },
    set(target, prop, value) { target[prop] = value; return true; }
  });
}

const memoryStorage = (() => {
  let data = {};
  return {
    getItem: k => (k in data ? data[k] : null),
    setItem: (k, v) => { data[k] = String(v); },
    removeItem: k => { delete data[k]; }
  };
})();

const sandbox = {
  console,
  Math, Date, JSON, Array, Object, String, Number, isNaN, parseInt, parseFloat, RegExp, Boolean,
  setTimeout, clearTimeout,
  localStorage: memoryStorage,
  document: {
    getElementById: () => fakeEl(),
    addEventListener: () => {},
    createElement: () => fakeEl(),
    querySelector: () => fakeEl(),
    querySelectorAll: () => [],
    documentElement: fakeEl()
  },
  window: { scrollTo: () => {}, addEventListener: () => {} },
  navigator: {},
  Image: function () { this.onload = null; this.onerror = null; },
  FileReader: function () { this.onload = null; this.onerror = null; this.readAsDataURL = () => {}; },
  speechSynthesis: undefined,
  // Node's WebCrypto — the app's apiCreateListing/doPlaceOrder use
  // crypto.randomUUID() for Idempotency-Key headers.
  crypto: globalThis.crypto,
  // Node's built-in fetch, so the app's own API-client code (apiRequestCode,
  // apiCreateListing, ...) runs unmodified and makes REAL HTTP requests.
  fetch: globalThis.fetch,
  // prompt() for the rental-booking flow; tests override sandbox.prompt.
  prompt: () => null
};
vm.createContext(sandbox);

const EXPORT_LIST = [
  "N","money","esc","T","classifySoilColor","rgbToHsl","combineLandType","LAND_TYPE_SUPPORT",
  "RULES","LANDS","ZONES","CROPS","WEATHER","LOTS","BS_MONTHS","BS_EN","BS_NOW","BS_YEAR",
  "S","sell","scan","offerRow","viewToday","viewSell","viewAdvice","viewMine","viewDoctor",
  "viewMarket","viewCart","viewHow","viewPool","viewRoute","viewBoard","render","header","nav",
  "chat","assistantReply","findCropInText","scoreIntent","defaultChips","renderSahayak",
  "sendChatMessage","CROP_ALIASES","INTENT_KEYWORDS","sahayakSeen",
  "CROP_API_CODE","BACKEND","account","apiRequestCode","apiVerifyCode","apiCreateListing",
  "apiSaveLandScan","apiGetLandScan",
  "doRequestCode","doVerifyCode","handlePostBtn","renderAccountPanel",
  "fetchAdminOverview","checkSession","onSessionExpired","doLogout","SessionExpiredError","adminPanelBar",
  "isStale","refreshOnce","renderPriceTrends","viewRental","viewMyOffers","viewAdminOffers",
  "viewAdminOrders","doSettleOrder","fetchCoopRoute","renderRealRoute",
  "viewAdminOverview","viewAdminFarmers","viewAdminLots","viewAdminRates",
  "doPlaceOrder","doSubmitOffer","trendsCache","rentalsCache","myOffersCache","adminOffersCache",
  "loadLots","doBookRental","nepalDateString","decideOffer","doConfirmLot","doSettleOrder"
];
const combined = appJs + `\n;globalThis.__test = {${EXPORT_LIST.join(",")}};`;

vm.runInContext(combined, sandbox, { filename: "kisan-bazar-extracted.js" });
const A = sandbox.__test;

// Evaluate code inside the app's own context. Needed because the app
// reassigns some top-level `let` bindings (e.g. `sell = {...}` after a
// post), which leaves an exported snapshot like A.sell pointing at a stale
// object — evaluating inside the context always sees the live binding.
function evalInApp(code) { return vm.runInContext(code, sandbox); }

module.exports = { A, sandbox, evalInApp };
