"""
Mock data layer standing in for the real API (/v1/listings, /v1/market/lots,
/v1/weather/current, etc. from the backend architecture doc). In production,
every function here becomes an HTTP call from the SMS/USSD gateway service
to those same endpoints — the menu code below never talks to a database
directly, so swapping this module for a real API client is a drop-in change.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class Crop:
    code: str          # short code used in SMS commands, e.g. "KAULI"
    name_ne: str
    name_en: str
    rate: int           # rs per kg, today
    grades: list = field(default_factory=lambda: ["A", "B", "C"])


CROPS = {
    "KAULI": Crop("KAULI", "काउली", "Cauliflower", 55),
    "GOLB":  Crop("GOLB",  "गोलभेँडा", "Tomato", 41),
    "ALU":   Crop("ALU",   "आलु", "Potato", 38),
    "PYAJ":  Crop("PYAJ",  "प्याज", "Onion", 62),
    "ADUWA": Crop("ADUWA", "अदुवा", "Ginger", 120),
    "MAKAI": Crop("MAKAI", "मकै", "Maize", 30),
}


@dataclass
class Farmer:
    phone: str
    name: str
    ward: str
    coop_name: str
    lang: str = "ne"   # ne / en


FARMERS = {
    "9841000001": Farmer("9841000001", "Sarita Tamang", "Dhulikhel-4", "Kavre Vegetable Co-op"),
    "9841000002": Farmer("9841000002", "Ram Bahadur K.C.", "Panauti-2", "Kavre Vegetable Co-op"),
}


@dataclass
class WeatherBulletin:
    severity: str        # info / advisory / warning / severe
    message_ne: str
    message_en: str


# keyed by ward prefix, mirrors weather_bulletins.affected_wards in the schema
WEATHER_BY_WARD = {
    "Dhulikhel": WeatherBulletin(
        "warning",
        "भोलि दिउँसो चट्याङसहित वर्षा। खुला खेतमा नबस्नुहोस्।",
        "Thunderstorms tomorrow afternoon. Stay out of open fields.",
    ),
    "Panauti": WeatherBulletin(
        "warning",
        "भोलि दिउँसो चट्याङसहित वर्षा। खुला खेतमा नबस्नुहोस्।",
        "Thunderstorms tomorrow afternoon. Stay out of open fields.",
    ),
}

# in-memory ledger of listings created via SMS/USSD this session, for demo purposes
LISTINGS = []


def create_listing(phone: str, crop_code: str, kg: int, grade: str) -> dict:
    """Mirrors POST /v1/listings. Returns what the API would return."""
    farmer = FARMERS.get(phone)
    crop = CROPS.get(crop_code)
    listing = {
        "id": f"L{len(LISTINGS) + 1:04d}",
        "farmer": farmer.name if farmer else phone,
        "coop": farmer.coop_name if farmer else "unassigned",
        "crop": crop.name_ne if crop else crop_code,
        "kg": kg,
        "grade": grade,
        "created_via": "sms",
        "created_at": datetime.now(),
        "expires_at": datetime.now() + timedelta(hours=60),
    }
    LISTINGS.append(listing)
    return listing


def farmer_ledger(phone: str) -> list:
    """Mirrors GET /v1/listings/mine — demo data, static for the illustration."""
    return [
        {"crop": "काउली", "kg": 40, "status": "पैसा बाँकी", "amount": 2200},
        {"crop": "गोलभेँडा", "kg": 25, "status": "भुक्तानी भयो", "amount": 1025},
    ]
