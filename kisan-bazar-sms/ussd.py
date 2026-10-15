"""
USSD state machine for *1234# — dial-in menu navigation for feature phones.

A telco USSD gateway sends each key the user presses as a new HTTP request
carrying the whole input string accumulated so far (e.g. "1*2" means
"pressed 1, then 2"), and expects back either CON (session continues, show
more text) or END (session closes, show final text). handle_ussd() is what
the gateway would call.

Design note: state lives on the Session object (an explicit `stage`), not
in the accumulated text string. Deriving position purely by re-parsing the
full text on every call is tempting but breaks as soon as any action has a
side effect that isn't idempotent under replay — e.g. a language toggle
re-processed from scratch on every request would fire again each time the
string is re-read, flipping back and forth. Reading only the *latest*
token (text.split("*")[-1]) and advancing session.stage avoids that class
of bug entirely, at the cost of assuming the gateway always appends
exactly one new token per request, which is the standard USSD contract.

Menu tree:

  *1234#
   1. आजको भाउ (rates)          -> pick crop -> show rate            [END]
   2. मौसम (weather)             -> show current ward bulletin        [END]
   3. बेच्ने (sell)               -> pick crop -> enter kg -> pick grade
                                     -> confirm                        [END]
   4. मेरो हिसाब (my ledger)      -> show last 2 entries               [END]
   0. भाषा बदल्नुहोस् / Language  -> toggle ne/en, restart at root      [CON]
"""
from mock_backend import CROPS, FARMERS, WEATHER_BY_WARD, create_listing, farmer_ledger

CROP_LIST = list(CROPS.values())   # stable order for numbered menu options
GRADES = [("1", "A", "उत्तम", "Best"), ("2", "B", "मध्यम", "Medium"), ("3", "C", "सामान्य", "Basic")]


def _t(lang, ne, en):
    return ne if lang == "ne" else en


def _crop_menu(lang):
    lines = [_t(lang, "बाली छान्नुहोस्:", "Choose a crop:")]
    for i, c in enumerate(CROP_LIST, 1):
        lines.append(f"{i}. {_t(lang, c.name_ne, c.name_en)}")
    return "\n".join(lines)


def _root_menu(lang):
    header = _t(lang, "किसान बजार", "Kisan Bazar")
    items = [
        _t(lang, "1. आजको भाउ", "1. Today's rate"),
        _t(lang, "2. मौसम", "2. Weather"),
        _t(lang, "3. बेच्ने", "3. Sell produce"),
        _t(lang, "4. मेरो हिसाब", "4. My ledger"),
        _t(lang, "0. Language / भाषा", "0. Language / भाषा"),
    ]
    return header + "\n" + "\n".join(items)


class Session:
    """Server-side session state, keyed by (phone, session_id) by the gateway.
    Kept minimal and serializable — a real deployment stores this in Redis
    with a short TTL (USSD sessions time out in ~90 seconds of inactivity)."""
    def __init__(self, phone):
        self.phone = phone
        self.lang = FARMERS.get(phone).lang if phone in FARMERS else "ne"
        self.stage = "root"      # root | rate_crop | sell_crop | sell_kg | sell_grade
        self.sell_crop = None
        self.sell_kg = None


def handle_ussd(phone: str, text: str, session: Session) -> tuple[str, bool]:
    """
    text: the full input string the gateway has accumulated this session.
    Only the latest token (the part after the last "*") is read — session.stage
    carries where the caller is in the menu, so earlier tokens are never
    replayed. Returns (message, is_final): True means send END, False means CON.
    """
    lang = session.lang
    latest = text.split("*")[-1] if text else ""

    # first dial-in: no input yet
    if text == "":
        session.stage = "root"
        return _root_menu(lang), False

    if session.stage == "root":
        if latest == "0":
            session.lang = "en" if session.lang == "ne" else "ne"
            return _root_menu(session.lang), False
        if latest == "1":
            session.stage = "rate_crop"
            return _crop_menu(lang), False
        if latest == "2":
            return _weather_reply(phone, lang), True
        if latest == "3":
            session.stage = "sell_crop"
            return _crop_menu(lang), False
        if latest == "4":
            return _ledger_reply(phone, lang), True
        return _t(lang, "गलत छनोट। पुनः प्रयास गर्नुहोस्।", "Invalid choice. Please try again."), True

    if session.stage == "rate_crop":
        idx = int(latest) - 1 if latest.isdigit() else -1
        if not (0 <= idx < len(CROP_LIST)):
            return _t(lang, "गलत छनोट।", "Invalid choice."), True
        c = CROP_LIST[idx]
        msg = _t(lang,
                 f"{c.name_ne}: प्रति के.जी. रु {c.rate}\n(कालीमाटी थोक भाउ)",
                 f"{c.name_en}: Rs {c.rate} per kg\n(Kalimati wholesale rate)")
        return msg, True

    if session.stage == "sell_crop":
        idx = int(latest) - 1 if latest.isdigit() else -1
        if not (0 <= idx < len(CROP_LIST)):
            return _t(lang, "गलत छनोट।", "Invalid choice."), True
        session.sell_crop = CROP_LIST[idx].code
        session.stage = "sell_kg"
        return _t(lang, "कति के.जी.? (संख्या लेख्नुहोस्)", "How many kg? (type a number)"), False

    if session.stage == "sell_kg":
        if not latest.isdigit() or int(latest) <= 0:
            return _t(lang, "संख्या मात्र लेख्नुहोस्।", "Please enter digits only."), True
        session.sell_kg = int(latest)
        session.stage = "sell_grade"
        grade_menu = _t(lang, "श्रेणी छान्नुहोस्:", "Choose a grade:") + "\n" + \
            "\n".join(f"{n}. {_t(lang, ne, en)}" for n, _, ne, en in GRADES)
        return grade_menu, False

    if session.stage == "sell_grade":
        match = next((g for g in GRADES if g[0] == latest), None)
        if not match:
            return _t(lang, "गलत छनोट।", "Invalid choice."), True
        grade = match[1]
        listing = create_listing(phone, session.sell_crop, session.sell_kg, grade)
        crop = CROPS[session.sell_crop]
        msg = _t(lang,
                 f"राखियो: {crop.name_ne} {session.sell_kg} के.जी. श्रेणी {grade}।\n"
                 f"{listing['coop']}ले हेरेर SMS पठाउनेछ।",
                 f"Listed: {crop.name_en} {session.sell_kg} kg, grade {grade}.\n"
                 f"{listing['coop']} will SMS you once reviewed.")
        return msg, True

    # shouldn't be reachable, but fail safe rather than crash the call
    return _t(lang, "त्रुटि भयो। फेरि प्रयास गर्नुहोस्।", "Something went wrong. Please try again."), True


def _weather_reply(phone, lang):
    farmer = FARMERS.get(phone)
    ward = farmer.ward.split("-")[0] if farmer else None
    bulletin = WEATHER_BY_WARD.get(ward)
    if bulletin:
        return _t(lang, bulletin.message_ne, bulletin.message_en)
    return _t(lang, "अहिलेलाई कुनै चेतावनी छैन।", "No active warning right now.")


def _ledger_reply(phone, lang):
    rows = farmer_ledger(phone)
    lines = [_t(lang, "पछिल्लो हिसाब:", "Recent ledger:")]
    for r in rows:
        lines.append(f"{r['crop']} {r['kg']}kg — {r['status']} रु{r['amount']}")
    return "\n".join(lines)
