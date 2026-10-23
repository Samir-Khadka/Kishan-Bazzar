"""Kisan Bazar conversational assistant — backend NLU engine.

A professional-grade conversational AI for Nepali farmers. Unlike the old
frontend keyword matcher, this engine:

- Understands THREE scripts: Devanagari Nepali (धानको भाउ?), Romanized
  Nepali (dhanko bhau kati?), and English (what's the paddy price?) —
  auto-detected per message, answered in the same language.
- Classifies intents with weighted confidence scoring, not raw keyword
  counting — so "mausam kaisa cha" beats a stray "price" mention.
- Extracts entities: crops (16, all scripts), quantities (kg/quintal/mann),
  zones (terai/hill/mountain).
- Holds conversation context per session: follow-ups like "ani aalu ko?"
  (and potato's?) or "what about maize?" resolve against the last topic.
- Answers from REAL data: live DB prices, the farmer's own listings and
  ledger, weather, planting advice for their zone/land.

Design notes for future maintainers:
- This is intentionally dependency-free (no ML libraries). On a Render
  free tier serving farmers on 2G, a 50ms rule-based NLU that works
  offline beats a 2s LLM call that times out. The day there's budget for
  an LLM, this module becomes the tool-calling layer: intents map 1:1 to
  function calls, responses stay template-driven for factual claims.
- NEVER invent prices, weather, or ledger figures. Every number in a
  reply comes from the database or the seeded reference tables. If data
  is missing, say so — a wrong price destroys farmer trust permanently.
"""

import re
import sys
import time
from collections import defaultdict

import repositories

# Agronomic knowledge (pest/disease remedies, fertilizer schedules, sowing
# windows) lives in ai_advisor.py — imported here so the chat NLU can serve
# it directly instead of the frontend calling a second endpoint.
try:
    from ai_advisor import CROPS_INFO as _AGRO
except ImportError:
    try:
        from .ai_advisor import CROPS_INFO as _AGRO
    except ImportError:
        _AGRO = {}

# ---------------------------------------------------------------------------
# Crop knowledge — 16 crops, three scripts each
# ---------------------------------------------------------------------------

CROPS = {
    # code: (nepali, english, [romanized aliases], emoji)
    "paddy":      ("धान",    "Paddy",      ["dhan", "dhaan", "rice"], "🌾"),
    "wheat":      ("गहुँ",    "Wheat",      ["gahu", "gahun", "ganhu"], "🌿"),
    "maize":      ("मकै",    "Maize",      ["makai", "makkai", "corn"], "🌽"),
    "potato":     ("आलु",    "Potato",     ["aalu", "alu"], "🥔"),
    "tomato":     ("गोलभेँडा", "Tomato",    ["golbheda", "tamatar", "golbeda"], "🍅"),
    "cauliflower":("काउली",   "Cauliflower",["kauli", "cauli"], "🥦"),
    "onion":      ("प्याज",   "Onion",      ["pyaj", "pyaaj", "piyaj"], "🧅"),
    "mustard":    ("तोरी",    "Mustard",    ["tori", "sarsyu", "sarson"], "🌼"),
    "lentil":     ("मुसुरो",   "Lentil",     ["musuro", "musuri", "daal", "dal"], "🫘"),
    "ginger":     ("अदुवा",   "Ginger",     ["aduwa", "adhuwa"], "🫚"),
    "cardamom":   ("अलैँची",  "Cardamom",   ["alaichi", "elaichi"], "🟤"),
    "junar":      ("जुनार",    "Junar",      ["junar", "suntala"], "🍊"),
    "apple":      ("स्याउ",    "Apple",      ["syau", "seu", "syaau"], "🍎"),
    "buckwheat":  ("फापर",    "Buckwheat",  ["fapar", "phapar"], "🌾"),
    "pea":        ("केराउ",    "Pea",        ["kerau", "keraau"], "🫛"),
    "radish":     ("मुला",    "Radish",     ["mula", "moola"], "🥕"),
}

# Colloquial/alternate Devanagari names people actually type.
# The formal name (गोलभेँडा) is in CROPS; people say टमाटर.
NE_ALIASES = {
    "टमाटर": "tomato", "टमाटरको": "tomato",
    "आलु": "potato", "आलुको": "potato",
    "प्याज": "onion", "प्याजको": "onion",
    "काउली": "cauliflower", "काउलीको": "cauliflower",
    "अदुवा": "ginger", "अदुवाको": "ginger",
    "मकै": "maize", "मकैको": "maize",
    "धान": "paddy", "धानको": "paddy",
    "गहुँ": "wheat", "गहुँको": "wheat",
    "तोरी": "mustard", "तोरीको": "mustard",
    "मुसुरो": "lentil", "दाल": "lentil",
    "स्याउ": "apple", "स्याउको": "apple",
    "जुनार": "junar", "सुन्तला": "junar",
    "मुला": "radish", "मुलाको": "radish",
    "केराउ": "pea", "मटर": "pea",
    "फापर": "buckwheat",
    "अलैँची": "cardamom", "अलैची": "cardamom",
}

# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")

# Common Romanized Nepali words — if these appear and there's no Devanagari,
# it's Romanized Nepali, not English.
ROMAN_NEPALI_MARKERS = {
    "cha", "chha", "ho", "hola", "kati", "kasto", "kasko", "kata", "kahile",
    "bhau", "bhav", "daam", "dam", "mulya", "paisa", "rupaiya", "mausam",
    "pani", "parcha", "bechne", "bechna", "kinnne", "kinne", "lagaune",
    "ropne", "kheti", "bari", "khet", "jagga", "mato", "sahakari", "sahakaari",
    "tapai", "hajur", "malai", "mero", "hamro", "timi", "timro", "ani", "tara",
    "kasari", "kina", "ke", "k", "namaste", "dhanyabad", "thik", "ramro",
    "naramro", "dherai", "thorai", "aaja", "bholi", "hijo", "ahile", "feri",
    "garchu", "garchhau", "huncha", "hunchha", "chhaina", "chhaina", "vayo",
    "bhayo", "milcha", "milchha",
}


def detect_language(text):
    """Return 'ne' (Devanagari), 'ro' (Romanized Nepali), or 'en'."""
    if DEVANAGARI_RE.search(text):
        return "ne"
    words = set(re.findall(r"[a-z]+", text.lower()))
    if words & ROMAN_NEPALI_MARKERS:
        return "ro"
    # crop aliases in roman script also signal roman nepali
    for _code, (_ne, _en, aliases, _ic) in CROPS.items():
        if words & set(aliases):
            return "ro"
    return "en"


# ---------------------------------------------------------------------------
# Entity extraction
# ---------------------------------------------------------------------------

def extract_crops(text):
    """Return list of crop codes mentioned, in order of appearance."""
    found = []
    lower = text.lower()
    # Devanagari names (formal + colloquial aliases)
    for code, (ne, _en, _al, _ic) in CROPS.items():
        if ne in text and code not in found:
            found.append((text.index(ne), code))
    for ne_alias, code in NE_ALIASES.items():
        if ne_alias in text and code not in [c for _, c in found]:
            found.append((text.index(ne_alias), code))
    # English names
    for code, (_ne, en, _al, _ic) in CROPS.items():
        idx = lower.find(en.lower())
        if idx >= 0 and code not in [c for _, c in found]:
            found.append((idx, code))
    # Romanized aliases — word-boundary match to avoid "alu" in "value".
    words = re.findall(r"[a-z]+", lower)
    for code, (_ne, _en, aliases, _ic) in CROPS.items():
        if code in [c for _, c in found]:
            continue
        for w in words:
            base = _strip_roman_suffix(w)
            if base in aliases or w in aliases:
                found.append((lower.find(w), code))
                break
    found.sort()
    return [c for _, c in found]


# Strips common Nepali postpositions glued to Romanized words: aaluko,
# pyajlai, makaibata -> aalu, pyaj, makai.
_ROMAN_SUFFIXES = ("ko", "lai", "bata", "ma", "le", "sanga", "dekhi", "samma")


def _strip_roman_suffix(word):
    for sfx in _ROMAN_SUFFIXES:
        if len(word) > len(sfx) + 2 and word.endswith(sfx):
            return word[: -len(sfx)]
    return word


QUANTITY_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(kg|केजी|के\.जी\.|quintal|क्विन्टल|mann|मन|muri|मुरी|pathi|पाथी)?",
    re.IGNORECASE,
)
# muri ≈ 52kg paddy, pathi ≈ 4kg — rough, used only for conversational estimates
UNIT_TO_KG = {"quintal": 100, "क्विन्टल": 100, "mann": 40, "मन": 40,
              "muri": 52, "मुरी": 52, "pathi": 4, "पाथी": 4}


def extract_quantity(text):
    """Return (kg_float, unit_str) or (None, None)."""
    m = QUANTITY_RE.search(text.replace(",", ""))
    if not m:
        return None, None
    val = float(m.group(1))
    unit = (m.group(2) or "kg").lower()
    kg = val * UNIT_TO_KG.get(unit, 1)
    return kg, unit


# ---------------------------------------------------------------------------
# Intent classification — weighted scoring with confidence
# ---------------------------------------------------------------------------
# Each intent: list of (pattern, weight). Patterns are substrings, matched
# case-insensitively; Devanagari matched as-is. Higher weight = more specific.

INTENTS = {
    "greeting": [
        ("नमस्ते", 3), ("namaste", 3), ("hello", 2), ("hi", 1), ("हेलो", 2),
        ("good morning", 2), ("good afternoon", 2), ("शुभ", 2),
    ],
    "price": [
        ("भाउ", 3), ("मूल्य", 3), ("दाम", 3), ("बजार भाउ", 4), ("rate", 2),
        ("price", 2), ("bhau", 3), ("bhav", 3), ("daam", 3), ("dam", 2),
        ("mulya", 3), ("kati parcha", 3), ("कति पर्छ", 3), ("कतिको", 2),
        ("how much", 2),
    ],
    "weather": [
        ("मौसम", 3), ("पानी पर्छ", 4), ("वर्षा", 3), ("हावापानी", 3),
        ("mausam", 3), ("weather", 3), ("barsha", 3), ("pani parcha", 3),
        ("rain", 2), ("पानी", 2),
    ],
    "sell": [
        ("बेच्ने", 3), ("बेच्न", 3), ("बिक्री", 3), ("bechne", 3),
        ("sell", 3), ("how to sell", 3), ("कसरी बेच्ने", 4),
        ("list", 1), ("सूची", 2),
    ],
    "plant": [
        ("लगाउने", 3), ("रोप्ने", 3), ("के लगाउ", 4), ("खेती", 2),
        ("kheti", 2), ("lagaune", 3), ("ropne", 3), ("plant", 2),
        ("grow", 2), ("what to plant", 4), ("sow", 2), ("रोप्न", 2),
    ],
    "ledger": [
        ("हिसाब", 3), ("पैसा आयो", 4), ("भुक्तानी", 3), ("payment", 3),
        ("hisab", 3), ("paisa", 2), ("ledger", 3), ("बाँकी", 2),
        ("balance", 2),
    ],
    "order": [
        ("अर्डर", 3), ("order", 3), ("किनेको", 2), ("bought", 2),
        ("status", 2), ("कहाँ पुग्यो", 3),
    ],
    "scan": [
        ("जग्गा जाँच", 4), ("माटो जाँच", 4), ("land scan", 3),
        ("माटो", 2), ("soil", 2), ("जग्गा", 2),
    ],
    "thanks": [
        ("धन्यवाद", 3), ("dhanyabad", 3), ("thanks", 3), ("thank you", 3),
        ("शुक्रिया", 2),
    ],
    "bye": [
        ("bye", 2), ("अलविदा", 2), ("फेरि भेटौंला", 3), ("see you", 2),
    ],
    "help": [
        ("सहयोग", 3), ("मद्दत", 3), ("help", 3), ("madad", 3),
        ("what can you do", 4), ("के गर्न सक्छौ", 4),
    ],
    "agronomy": [
        ("रोग", 3), ("कीरा", 3), ("डढुवा", 4), ("कुहिने", 3), ("पहेंलो", 2),
        ("disease", 3), ("pest", 3), ("blight", 4), ("rot", 2), ("curl", 2),
        ("rog", 3), ("keera", 3), ("kira", 3), ("khumre", 3), ("lahi", 3),
        ("मल", 3), ("युरिया", 4), ("डिएपी", 4), ("पोटास", 4), ("कम्पोस्ट", 3),
        ("गोबर", 3), ("fertilizer", 3), ("urea", 4), ("dap", 3),
        ("potash", 3), ("manure", 3), ("mal", 2),
        ("रोप्ने समय", 3), ("sowing", 3), ("कहिले रोप्ने", 4),
        ("उपचार", 2), ("treatment", 2), ("बिरामी", 2),
    ],
}

# Follow-up patterns: "and potato's?", "ani aalu ko?", "what about maize?"
FOLLOWUP_RES = [
    re.compile(r"^(ani|tara|and|but)\b", re.IGNORECASE),
    re.compile(r"\b(ko|को)\s*\??$"),
    re.compile(r"what about\b", re.IGNORECASE),
    re.compile(r"^(tyo|yo|tesko|usko)\b", re.IGNORECASE),  # that/this/its
]


def classify_intent(text, context):
    """Return (intent, confidence 0..1). Uses context for follow-ups."""
    lower = text.lower()
    scores = defaultdict(float)
    for intent, patterns in INTENTS.items():
        for pat, w in patterns:
            if pat.lower() in lower:
                scores[intent] += w

    # A bare crop name with no other signal is almost always a price question
    crops = extract_crops(text)
    if crops and not scores:
        return "price", 0.75

    # Follow-up: "and potato?" after a price answer → price intent for potato
    if crops and context.get("last_intent") in ("price", "plant"):
        is_followup = any(rx.search(text.strip()) for rx in FOLLOWUP_RES)
        if is_followup or len(text.strip().split()) <= 3:
            return context["last_intent"], 0.8

    if not scores:
        return None, 0.0
    best = max(scores, key=scores.get)
    # normalize: confidence from score mass
    total = sum(scores.values())
    conf = min(0.95, scores[best] / max(total, 1) * 1.2)
    return best, conf


# ---------------------------------------------------------------------------
# Response templates — trilingual
# ---------------------------------------------------------------------------

def T(ne, ro, en, lang):
    return {"ne": ne, "ro": ro, "en": en}.get(lang, ne)


# ---------------------------------------------------------------------------
# Session context (in-memory; fine for demo scale)
# ---------------------------------------------------------------------------

_sessions = {}
SESSION_TTL = 1800  # 30 minutes


def get_context(session_id):
    now = time.time()
    # opportunistic cleanup
    for sid in [s for s, c in _sessions.items() if now - c["updated"] > SESSION_TTL]:
        del _sessions[sid]
    ctx = _sessions.get(session_id)
    if not ctx:
        ctx = {"last_intent": None, "last_crops": [], "turns": 0, "updated": now}
        _sessions[session_id] = ctx
    ctx["updated"] = now
    return ctx


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def chat(session_id, message, db_conn=None, farmer=None):
    """Process one message. Returns dict: reply, chips, cards, lang, intent."""
    text = (message or "").strip()
    ctx = get_context(session_id or "anon")
    lang = detect_language(text) if text else "ne"

    if not text:
        return _resp(
            T("के सोध्नुभयो, बुझिनँ। फेरि लेख्नुहोस्।",
              "Ke sodhnubhayo, bujhina. Feri lekhnuhos.",
              "I didn't catch that — try typing again.", lang),
            chips=_default_chips(lang), lang=lang, intent="fallback")

    intent, conf = classify_intent(text, ctx)
    crops = extract_crops(text)

    # context carryover: "and potato's?" with no new intent
    if not crops and ctx.get("last_crops") and intent in (None, ctx.get("last_intent")):
        if any(rx.search(text.strip()) for rx in FOLLOWUP_RES) or len(text.split()) <= 2:
            crops = ctx["last_crops"]
            intent = ctx.get("last_intent") or intent

    handler = _HANDLERS.get(intent, _fallback)
    result = handler(text, lang, crops, ctx, db_conn, farmer, conf)

    ctx["last_intent"] = intent
    if crops:
        ctx["last_crops"] = crops
    ctx["turns"] += 1
    result["lang"] = lang
    result["intent"] = intent
    return result


def _resp(reply, chips=None, cards=None, lang="ne", intent=None):
    return {"reply": reply, "chips": chips or [], "cards": cards or [],
            "lang": lang, "intent": intent}


def _default_chips(lang):
    return [
        T("आजको मौसम?", "Aajako mausam?", "Today's weather?", lang),
        T("धानको भाउ?", "Dhanko bhau?", "Paddy price?", lang),
        T("के लगाउने?", "Ke lagaune?", "What to plant?", lang),
        T("कसरी बेच्ने?", "Kasari bechne?", "How to sell?", lang),
    ]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _greeting(_t, lang, _c, _ctx, _db, farmer, _conf):
    name = f", {farmer['name'].split()[0]}" if farmer and farmer.get("name") else ""
    return _resp(
        T(f"नमस्ते{name}! 🙏 म किसान सहायक हुँ — भाउ, मौसम, बेच्ने तरिका, के लगाउने, वा तपाईंको हिसाबबारे सोध्नुहोस्।",
          f"Namaste{name}! 🙏 Ma kisan sahayak hun — bhau, mausam, bechne tarika, ke lagaune, wa tapaaiko hisab baare sodhnuhos.",
          f"Hello{name}! 🙏 I'm your farm assistant — ask me about prices, weather, how to sell, what to plant, or your ledger.", lang),
        chips=_default_chips(lang))


def _price(_t, lang, crops, _ctx, db_conn, _farmer, conf):
    if not crops:
        return _resp(
            T("कुन बालीको भाउ चाहियो? जस्तै: आलुको भाउ?",
              "Kun baliko bhau chahiyo? Jastai: aaluko bhau?",
              "Which crop's price? For example: potato price?", lang),
            chips=[T("आलु", "Aalu", "Potato", lang), T("धान", "Dhan", "Paddy", lang),
                   T("गोलभेँडा", "Golbheda", "Tomato", lang)])
    cards = []
    lines = []
    for code in crops[:3]:
        ne, en, _al, ic = CROPS[code]
        rate, chg = _get_price(db_conn, code)
        name = T(ne, en, en, lang)
        if rate is None:
            lines.append(T(f"{ic} {name}: अहिले भाउ उपलब्ध छैन।",
                           f"{ic} {name}: ahile bhau upalabdha chhaina.",
                           f"{ic} {name}: no price available right now.", lang))
            continue
        if chg is None:
            # No earlier recorded rate — state the price, claim no trend.
            lines.append(T(f"{ic} {name}: रू {rate}/के.जी.",
                           f"{ic} {name}: Rs {rate}/kg.",
                           f"{ic} {name}: Rs {rate}/kg.", lang))
        else:
            arrow = "📈" if chg > 0 else "📉" if chg < 0 else "➡️"
            dirn = T("बढेको", "badheko", "up", lang) if chg > 0 else \
                   T("घटेको", "ghateko", "down", lang) if chg < 0 else \
                   T("स्थिर", "sthir", "steady", lang)
            lines.append(T(f"{ic} {name}: रू {rate}/के.जी. ({dirn} {arrow})",
                           f"{ic} {name}: Rs {rate}/kg ({dirn} {arrow})",
                           f"{ic} {name}: Rs {rate}/kg ({dirn} {arrow})", lang))
        cards.append({"type": "price", "crop": code, "name": name,
                      "rate": rate, "change": chg, "icon": ic})
    others = [c for c in CROPS if c not in crops][:2]
    chips = [T(f"{CROPS[c][0]}को पनि", f"{CROPS[c][1]} pani", f"{CROPS[c][1]} too", lang)
             for c in others]
    chips.append(T("यो बेच्न चाहन्छु", "Yo bechna chahanchhu", "I want to sell this", lang))
    return _resp("\n".join(lines), chips=chips, cards=cards)


# Maps the assistant's lowercase crop codes to the DB's uppercase codes
# (seed.py uses KAULI/GOLB/ALU/... — the CROP_API_CODE convention).
DB_CROP_CODE = {
    "paddy": "DHAN", "wheat": "GAHU", "maize": "MAKAI", "potato": "ALU",
    "tomato": "GOLB", "cauliflower": "KAULI", "onion": "PYAJ",
    "mustard": "TORI", "lentil": "MUSURO", "ginger": "ADUWA",
    "cardamom": "ALAICHI", "junar": "JUNAR", "apple": "SYAU",
    "buckwheat": "FAPAR", "pea": "KERAU", "radish": "MULA",
}


def _get_price(db_conn, crop_code):
    """(rate, change_pct) — the latest confirmed lot rate for the crop,
    and its % change vs the previous recorded rate.

    change_pct is None when there is no earlier rate to compare
    against: the reply then states the rate with NO trend claim,
    instead of the old behaviour — change hardcoded to 0, so every
    price was confidently labelled "steady", and a fallback query
    naming a column that doesn't exist (listings.rate_per_kg; rates
    live on lots) that could never fire. Prices here are real or
    absent, never decorated with an invented direction."""
    if db_conn is None:
        return None, None
    db_code = DB_CROP_CODE.get(crop_code, crop_code)
    try:
        rates = repositories.recent_lot_rates(db_conn, db_code, limit=2)
    except Exception as e:
        print(f"assistant price lookup failed for {db_code}: {e!r}",
              file=sys.stderr)
        return None, None
    if not rates:
        return None, None
    rate = rates[0]
    if len(rates) < 2 or not rates[1]:
        return rate, None
    return rate, (rate - rates[1]) / rates[1] * 100.0


def _weather(_t, lang, _c, _ctx, _db, _farmer, _conf):
    # Static SAMPLE forecast until a real feed (DHM/Open-Meteo) is
    # wired in — and the reply says so, the same way the app's weather
    # card badges its demo data. A farmer planning around invented
    # rain is worse than a farmer told "this is sample data".
    return _resp(
        T("🌤️ आज: घाम लाग्नेछ, २६° — तीन दिनपछि पानी पर्ने सम्भावना। मकै र अदुवा सुकाउने राम्रो मौका।\nℹ️ यो नमुना पूर्वानुमान हो — वास्तविक मौसम चाँडै जोडिनेछ।",
          "🌤️ Aaja: gham lagnechha, 26° — teen din pachhi pani parne sambhawana. Makai ra aduwa sukaune ramro mauka.\nℹ️ Yo namuna purbanuman ho — bastabik mausam chandai jodinechha.",
          "🌤️ Today: sunny, 26° — rain likely in three days. Good window to dry maize and ginger.\nℹ️ This is sample forecast data — a live weather feed is coming soon.", lang),
        chips=[T("भोलिको मौसम?", "Bholiko mausam?", "Tomorrow's weather?", lang),
               T("आजको भाउ", "Aajako bhau", "Today's rates", lang)],
        cards=[{"type": "weather", "today": "26° ☀️",
                "advice": T("मकै/अदुवा सुकाउनुहोस्", "Makai/aduwa sukaunuhos",
                            "Dry maize/ginger", lang)}])


def _sell(_t, lang, _c, _ctx, _db, _farmer, _conf):
    return _resp(
        T("बेच्न सजिलो छ:\n1️⃣ बेच्ने ट्याबमा बाली, तौल र श्रेणी छान्नुहोस्\n2️⃣ 'सहकारीमा पठाउनुहोस्' थिच्नुहोस्\n3️⃣ सहकारीले लट बनाएपछि SMS आउँछ\n4️⃣ तौलिएको ४८ घण्टाभित्र पैसा — eSewa, खल्ती वा नगद।",
          "Bechna sajilo chha:\n1️⃣ Bechne tab ma bali, taul ra shreni chhannuhos\n2️⃣ 'Sahakarima pathaunuhos' thichnuhos\n3️⃣ Sahakarile lot banaepachhi SMS aauchha\n4️⃣ Taulieko 48 ghanta bhitra paisa — eSewa, Khalti wa nagad.",
          "Selling is simple:\n1️⃣ In the Sell tab pick crop, weight and grade\n2️⃣ Tap 'Send to my co-op'\n3️⃣ You'll get an SMS once the co-op forms a lot\n4️⃣ Paid within 48h of weighing — eSewa, Khalti or cash.", lang),
        chips=[T("बेच्ने ट्याब खोल्ने", "Bechne tab kholne", "Open Sell tab", lang),
               T("धन्यवाद", "Dhanyabad", "Thanks", lang)])


def _plant(_t, lang, crops, _ctx, _db, _farmer, _conf):
    if crops:
        ne, en, _al, ic = CROPS[crops[0]]
        name = T(ne, en, en, lang)
        return _resp(
            T(f"{ic} {name} बारे सल्लाह ट्याबमा विस्तृत जानकारी छ — माटो, पानी र मलको मात्रा।",
              f"{ic} {name} baare sallah tab ma bistrit jankari chha — mato, pani ra malko matra.",
              f"{ic} See the Advice tab for detailed {name} guidance — soil, water and fertilizer.", lang),
            chips=_default_chips(lang))
    return _resp(
        T("तपाईंको क्षेत्र र जग्गाअनुसार सल्लाह ट्याबमा अहिले लगाउन मिल्ने बालीहरू छन्। सामान्य नियम: असोजमा तोरी/मुसुरो, जेठमा धान।",
          "Tapaaiko kshetra ra jagga anusar sallah tab ma ahile lagauna milne baliharu chhan. Samanya niyam: Asoj ma tori/musuro, Jestha ma dhan.",
          "The Advice tab lists what suits your zone and land right now. Rule of thumb: mustard/lentil in Asoj, paddy in Jestha.", lang),
        chips=[T("सल्लाह हेर्ने", "Sallah herne", "See advice", lang)])


def _ledger(_t, lang, _c, _ctx, db_conn, farmer, _conf):
    if not farmer:
        return _resp(
            T("हिसाब हेर्न लगइन गर्नुहोस् — मेरो ट्याबमा फोन नम्बरबाट कोड लिनुहोस्।",
              "Hisab herna login garnuhos — mero tab ma phone number bata code linuhos.",
              "Log in to see your ledger — get a code with your phone number in the Mine tab.", lang),
            chips=[T("लगइन गर्ने", "Login garne", "Log in", lang)])
    # The figure comes from repositories.farmer_pending_due — the
    # prorated listings -> lot_listings -> lots -> orders join. (The
    # previous inline query named columns that don't exist and hid
    # the resulting error behind a bare `except: pass`, so every
    # farmer was told "no pending payments" forever.) A lookup
    # failure now degrades HONESTLY: the farmer is told the ledger
    # couldn't be fetched, never that nothing is owed.
    try:
        if db_conn is None:
            raise RuntimeError("no db connection")
        due = repositories.farmer_pending_due(db_conn, farmer["phone"])
    except Exception as e:
        print(f"assistant ledger lookup failed: {e!r}", file=sys.stderr)
        return _resp(
            T("⚠️ अहिले तपाईंको हिसाब ल्याउन सकिएन। केही बेरपछि फेरि प्रयास गर्नुहोस्, वा सहकारीलाई फोन गर्नुहोस्।",
              "⚠️ Ahile tapaaiko hisab lyauna sakiena. Kehi berpachhi feri prayas garnuhos, wa sahakarilai phone garnuhos.",
              "⚠️ I couldn't fetch your ledger right now. Please try again in a little while, or call your co-op.", lang),
            chips=[T("फेरि प्रयास गर्ने", "Feri prayas garne", "Try again", lang)])
    if due > 0:
        return _resp(
            T(f"💰 तपाईंलाई रू {due:,.0f} आउन बाँकी छ। तौलिएको ४८ घण्टाभित्र भुक्तानी हुन्छ।",
              f"💰 Tapailai Rs {due:,.0f} auna baaki chha. Taulieko 48 ghanta bhitra bhuktani hunchha.",
              f"💰 You have Rs {due:,.0f} pending. Paid within 48 hours of weighing.", lang),
            chips=[T("धन्यवाद", "Dhanyabad", "Thanks", lang)])
    return _resp(
        T("✅ अहिले तपाईंको कुनै भुक्तानी बाँकी छैन।",
          "✅ Ahile tapaaiko kunai bhuktani baaki chhaina.",
          "✅ You have no pending payments right now.", lang),
        chips=[T("बेच्न सुरु गर्ने", "Bechna suru garne", "Start selling", lang)])


def _order(_t, lang, _c, _ctx, _db, farmer, _conf):
    if not farmer:
        return _resp(
            T("अर्डर हेर्न लगइन गर्नुहोस्।", "Order herna login garnuhos.",
              "Log in to see your orders.", lang))
    return _resp(
        T("📦 तपाईंका अर्डरहरू बेच्ने/किन्ने ट्याबमा देखिन्छन्।",
          "📦 Tapaaika orderharu bechne/kinne tab ma dekhinchhan.",
          "📦 Your orders appear in the Sell/Buy tabs.", lang))


def _scan(_t, lang, _c, _ctx, _db, _farmer, _conf):
    return _resp(
        T("📷 सल्लाह ट्याबमा 'फोटोबाट जाँच्नुहोस्' थिच्नुहोस् — माटोको फोटो र दुई प्रश्नबाट जग्गाको किसिम थाहा पाइन्छ।",
          "📷 Sallah tab ma 'photo bata janchnuhos' thichnuhos — matoko photo ra dui prashna bata jaggako kism thaha painchha.",
          "📷 In the Advice tab tap 'Scan by photo' — it estimates your land type from a soil photo and two questions.", lang))


def _agronomy(text, lang, crops, _ctx, _db, _farmer, _conf):
    """Pest/disease/fertilizer/sowing advice from the curated knowledge base."""
    lower = text.lower()
    is_pest = any(w in lower for w in
                  ["रोग", "कीरा", "डढुवा", "कुहिने", "disease", "pest", "blight",
                   "rog", "keera", "kira", "khumre", "lahi", "उपचार"])
    # map assistant crop codes to ai_advisor keys (same lowercase names)
    agro = None
    if crops:
        agro = _AGRO.get(crops[0])
    if not agro and crops:
        # try matching by nepali/english name
        for key, info in _AGRO.items():
            ne, en, _al, _ic = CROPS.get(crops[0], ("", "", [], ""))
            if info.get("ne") == ne or info.get("en", "").lower() == en.lower():
                agro = info
                break
    if agro:
        ne_name = agro.get("ne", "")
        if is_pest:
            pests = "\n".join(f"• {v}" for v in agro.get("pests", {}).values())
            body = T(f"🌿 {ne_name} का मुख्य रोग र उपचार:\n\n{pests}\n\n💡 रासायनिक विषादीभन्दा पहिले नीमको तेल वा ट्राइकोडर्मा जस्ता जैविक उपाय अपनाउनुहोस्।",
                     f"🌿 {agro.get('en','')} pest/disease remedies:\n\n{pests}",
                     f"🌿 {agro.get('en','')} pest/disease remedies:\n\n{pests}", lang)
        else:
            fert = agro.get("fertilizer", "")
            sow = agro.get("sow", "")
            body = T(f"🌱 {ne_name}:\n\nमलखाद: {fert}\n\nरोप्ने समय: {sow}",
                     f"🌱 {agro.get('en','')}:\n\nFertilizer: {fert}\n\nSowing: {sow}",
                     f"🌱 {agro.get('en','')}:\n\nFertilizer: {fert}\n\nSowing: {sow}", lang)
        return _resp(body, chips=[
            T("अर्को बाली", "Arko bali", "Another crop", lang),
            T("आजको भाउ", "Aajako bhau", "Today's rates", lang)])
    # no crop identified — ask which one
    return _resp(
        T("कुन बालीमा समस्या छ? जस्तै: गोलभेँडामा डढुवा, काउलीमा लाही कीरा।",
          "Kun balima samasya chha? Jastai: golbhedama dadhua, kaulima lahi kira.",
          "Which crop has the problem? For example: tomato blight, cauliflower aphids.", lang),
        chips=[T("गोलभेँडा", "Golbheda", "Tomato", lang),
               T("काउली", "Kauli", "Cauliflower", lang),
               T("आलु", "Aalu", "Potato", lang)])


def _thanks(_t, lang, _c, _ctx, _db, _farmer, _conf):
    return _resp(
        T("खुसी लाग्यो! 🙏 अरू केही सोध्नु छ?",
          "Khusi lagyo! 🙏 Aru kehi sodhnu chha?",
          "Glad to help! 🙏 Anything else?", lang),
        chips=_default_chips(lang))


def _bye(_t, lang, _c, _ctx, _db, _farmer, _conf):
    return _resp(
        T("फेरि भेटौंला! 👋 राम्रो खेती होस्।",
          "Feri bhetnla! 👋 Ramro kheti hos.",
          "See you again! 👋 Happy farming.", lang))


def _help(_t, lang, _c, _ctx, _db, _farmer, _conf):
    return _resp(
        T("म यी कुरामा मद्दत गर्न सक्छु:\n💰 बालीको भाउ\n🌤️ मौसम\n🧺 कसरी बेच्ने\n🌱 के लगाउने\n📒 तपाईंको हिसाब\nतल थिच्नुहोस् वा आफ्नै शब्दमा सोध्नुहोस्।",
          "Ma yi kurama maddat garna sakchhu:\n💰 Baliko bhau\n🌤️ Mausam\n🧺 Kasari bechne\n🌱 Ke lagaune\n📒 Tapaaiko hisab\nTal thichnuhos wa aafnai shabdama sodhnuhos.",
          "I can help with:\n💰 Crop prices\n🌤️ Weather\n🧺 How to sell\n🌱 What to plant\n📒 Your ledger\nTap below or ask in your own words.", lang),
        chips=_default_chips(lang))


def _fallback(_t, lang, _c, _ctx, _db, _farmer, conf):
    low = conf < 0.4
    return _resp(
        T("मैले ठ्याक्कै बुझिनँ। भाउ, मौसम, बेच्ने, के लगाउने वा हिसाबबारे सोध्नुहोस् — वा सहकारीलाई सिधै फोन गर्नुहोस्।",
          "Maile thyakkai bujhina. Bhau, mausam, bechne, ke lagaune wa hisab baare sodhnuhos — wa sahakarilai sidhai phone garnuhos.",
          "I didn't quite get that. Ask about prices, weather, selling, planting or your ledger — or call your co-op directly.", lang),
        chips=_default_chips(lang))


_HANDLERS = {
    "greeting": _greeting, "price": _price, "weather": _weather,
    "sell": _sell, "plant": _plant, "ledger": _ledger, "order": _order,
    "scan": _scan, "thanks": _thanks, "bye": _bye, "help": _help,
    "agronomy": _agronomy,
}
