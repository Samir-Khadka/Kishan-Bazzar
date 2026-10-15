"""
Plain-SMS keyword commands — for phones or moments where USSD isn't reachable
(USSD needs a live session and can fail on weak signal; a plain SMS queues and
retries at the network level, which matters in the hills). Same backend calls
as the USSD flow, different transport.

Commands (case-insensitive, space or comma separated):
  RATE <CROP>              -> today's rate for one crop
  RATE                     -> today's rate for all crops
  BECH <CROP> <KG> <GRADE> -> list produce for sale  (bech = "sell")
  MAUSAM                   -> weather for the farmer's registered ward
  HISAB                    -> recent ledger entries
  HELP                     -> list of commands

Crop codes match the USSD menu's underlying codes (KAULI, GOLB, ALU, PYAJ,
ADUWA, MAKAI) so a co-op field agent training farmers only teaches one
vocabulary, not two.
"""
from mock_backend import CROPS, FARMERS, WEATHER_BY_WARD, create_listing, farmer_ledger

HELP_TEXT_NE = (
    "आदेशहरू:\n"
    "RATE बाली - भाउ हेर्न\n"
    "BECH बाली तौल श्रेणी - बेच्न राख्न (जस्तै: BECH KAULI 40 A)\n"
    "MAUSAM - मौसम हेर्न\n"
    "HISAB - आफ्नो हिसाब हेर्न"
)
HELP_TEXT_EN = (
    "Commands:\n"
    "RATE crop - check price\n"
    "BECH crop kg grade - list produce (e.g. BECH KAULI 40 A)\n"
    "MAUSAM - check weather\n"
    "HISAB - check your ledger"
)


def handle_sms(phone: str, body: str) -> str:
    """Returns the SMS reply text the gateway should send back."""
    farmer = FARMERS.get(phone)
    lang = farmer.lang if farmer else "ne"
    tokens = body.strip().upper().split()
    if not tokens:
        return HELP_TEXT_NE if lang == "ne" else HELP_TEXT_EN

    cmd = tokens[0]

    if cmd == "HELP":
        return HELP_TEXT_NE if lang == "ne" else HELP_TEXT_EN

    if cmd == "RATE":
        if len(tokens) == 1:
            lines = [f"{c.name_ne if lang=='ne' else c.name_en}: रु{c.rate}" for c in CROPS.values()]
            return "\n".join(lines)
        crop = CROPS.get(tokens[1])
        if not crop:
            return _unknown_crop(lang, tokens[1])
        name = crop.name_ne if lang == "ne" else crop.name_en
        return (f"{name}: प्रति के.जी. रु {crop.rate}" if lang == "ne"
                else f"{name}: Rs {crop.rate} per kg")

    if cmd == "BECH":
        if len(tokens) != 4:
            return ("गलत ढाँचा। जस्तै: BECH KAULI 40 A" if lang == "ne"
                    else "Wrong format. Example: BECH KAULI 40 A")
        _, crop_code, kg_str, grade = tokens
        crop = CROPS.get(crop_code)
        if not crop:
            return _unknown_crop(lang, crop_code)
        if not kg_str.isdigit() or int(kg_str) <= 0:
            return "तौल संख्यामा लेख्नुहोस्।" if lang == "ne" else "Weight must be a number."
        if grade not in ("A", "B", "C"):
            return "श्रेणी A, B, वा C हुनुपर्छ।" if lang == "ne" else "Grade must be A, B, or C."
        listing = create_listing(phone, crop_code, int(kg_str), grade)
        name = crop.name_ne if lang == "ne" else crop.name_en
        if lang == "ne":
            return f"राखियो: {name} {kg_str}के.जी. श्रेणी {grade}। {listing['coop']}ले हेर्नेछ।"
        return f"Listed: {name} {kg_str}kg grade {grade}. {listing['coop']} will review."

    if cmd == "MAUSAM":
        ward = farmer.ward.split("-")[0] if farmer else None
        bulletin = WEATHER_BY_WARD.get(ward)
        if not bulletin:
            return "अहिलेलाई कुनै चेतावनी छैन।" if lang == "ne" else "No active warning right now."
        return bulletin.message_ne if lang == "ne" else bulletin.message_en

    if cmd == "HISAB":
        rows = farmer_ledger(phone)
        lines = [f"{r['crop']} {r['kg']}kg — {r['status']} रु{r['amount']}" for r in rows]
        return "\n".join(lines)

    return ("चिन्नँ। HELP लेखेर पठाउनुहोस्।" if lang == "ne"
            else "Unrecognized. Text HELP for commands.")


def _unknown_crop(lang, code):
    known = ", ".join(CROPS.keys())
    return (f"'{code}' चिन्नँ। यीमध्ये लेख्नुहोस्: {known}" if lang == "ne"
            else f"Unknown crop '{code}'. Use one of: {known}")
