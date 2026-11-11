"""Architecture regression tests for the Nov 11 2026 hardening pass.

Covers the holes found in the code-architecture review — one canonical
JSON-body reader (web.json_body) plus two repository-seam fixes:

  1. Non-object JSON bodies 500'd: fourteen handlers read the body in
     three different spellings, and a JSON array / string / number
     sailed through `or {}` (truthy non-dicts) and 500'd on the first
     `.get()` — verified live on /v1/listings, /v1/orders and
     /v1/assistant/chat. A bare `get_json(force=True)` raised
     BadRequest on malformed/empty bodies, answering with Werkzeug's
     generic "browser sent a request..." text instead of the API's
     own error wording. Every POST endpoint now reads through
     json_body(), which returns {} unless the body parses to a dict,
     so garbage degrades to the handler's own 400s.
  2. require_self parsed the body with get_json(silent=True) — no
     force — so a valid JSON body posted without a Content-Type
     header 403'd a legitimate caller while the handler behind it
     (force=True) would have parsed it fine. It now uses json_body().
  3. aggregation.open_listings_in_radius duplicated the co-op lookup
     (inline SELECT * FROM cooperatives) instead of using
     repositories.get_cooperative — the seam repositories.py exists
     to own. Same for the farmer display-name fallback, hand-rolled
     twice in routes_services (now repositories.farmer_display_name).

Run: python3 tests/test_architecture_nov11.py
"""
import json
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import seed
import app as app_module
import auth
import aggregation
import db
import repositories

FARMER = "9841000001"
STAFF = "9801100000"
IP = "203.0.113.21"


def token_header(phone):
    return {"Authorization": f"Bearer {auth.issue_token(phone)}"}


def post_raw(client, path, data, as_phone=None, ip=IP, content_type="application/json"):
    headers = token_header(as_phone) if as_phone else {}
    return client.post(path, data=data, content_type=content_type,
                       headers=headers,
                       environ_base={"REMOTE_ADDR": ip})


# Garbage bodies: none of these may ever produce a 500.
GARBAGE = [
    ("empty body", b""),
    ("malformed JSON", b"{not json"),
    ("JSON array", b"[1, 2]"),
    ("JSON string", b'"hello"'),
    ("JSON number", b"42"),
    ("JSON null", b"null"),
]

# (path, authenticated phone or None, expected non-500 status per body)
# require_self endpoints answer 403 when no phone survives parsing;
# the rest answer their own field-validation 400/401.
CASES = [
    ("/v1/listings", FARMER, 403),
    ("/v1/orders", FARMER, 400),
    ("/v1/assistant/chat", None, 400),
    ("/v1/auth/request-code", None, 400),
    ("/v1/auth/verify", None, 400),
    ("/v1/auth/admin", None, 401),
    ("/v1/auth/signup", FARMER, 403),
    (f"/v1/farmers/{FARMER}/land-scan", FARMER, 400),
    ("/v1/lots/KVR-118/confirm", STAFF, 400),
    ("/v1/crop-doctor/diagnose", FARMER, 400),
    ("/v1/rentals", FARMER, 400),
    ("/v1/offers", FARMER, 403),
    ("/v1/offers/1/decide", STAFF, 400),
    ("/v1/orders/1/settle", STAFF, 400),
    ("/v1/rentals/1/book", FARMER, 400),
]


def run():
    seed.seed()
    client = app_module.app.test_client()
    auth._attempts.clear()
    passed = 0

    print("=== 1. Garbage bodies never 500, and get honest errors ===")
    for path, phone, expected in CASES:
        for label, data in GARBAGE:
            # A fresh IP per call: several of these endpoints are
            # rate-limited per IP, and a 429 here would mask the
            # status under test.
            ip = f"203.0.113.{abs(hash((path, label))) % 200 + 1}"
            r = post_raw(client, path, data, as_phone=phone, ip=ip)
            assert r.status_code != 500, (
                f"{path} {label}: got 500")
            assert r.status_code == expected, (
                f"{path} {label}: got {r.status_code}, want {expected}: "
                f"{r.get_data(as_text=True)[:120]}")
        print(f"  PASS — {path}: all 6 garbage bodies -> {expected}")
        passed += 1

    print("=== 2. Valid JSON without a Content-Type header reaches the handler ===")
    # require_self used to parse without force=True: this exact
    # request 403'd before the fix, even though the body is valid.
    good = json.dumps({"farmer_phone": FARMER, "coop_id": "kavre-veg",
                       "crop_code": "KAULI", "grade": "A",
                       "quantity_kg": 10}).encode()
    r = post_raw(client, "/v1/listings", good, as_phone=FARMER,
                 ip="203.0.113.99", content_type="text/plain")
    assert r.status_code == 201, (r.status_code, r.get_data(as_text=True))
    print("  PASS — listing created (201) with no Content-Type header")
    passed += 1

    print("=== 3. Valid flows still work end to end ===")
    r = client.post("/v1/rentals", json={
        "machine_type": "thresher", "machine_name": "Test Thresher",
        "location": "Dhulikhel", "contact_phone": FARMER,
        "rate_per_day": 1000}, headers=token_header(FARMER))
    assert r.status_code == 201, (r.status_code, r.get_data(as_text=True))
    rid = r.get_json()["rental_id"]
    conn = db.get_db()
    row = conn.execute("SELECT owner_name FROM rentals WHERE id=?",
                       (rid,)).fetchone()
    assert row["owner_name"] == "Sarita Tamang", row["owner_name"]
    conn.close()
    print("  PASS — rental created with the farmer's display name")
    passed += 1

    print("=== 4. Repository seam: co-op lookup + display name ===")
    conn = db.get_db()
    assert repositories.farmer_display_name(conn, FARMER) == "Sarita Tamang"
    assert repositories.farmer_display_name(conn, "9800000000") == "9800000000"
    assert aggregation.open_listings_in_radius(conn, "no-such-coop", "KAULI", "A") == []
    in_range = aggregation.open_listings_in_radius(conn, "kavre-veg", "KAULI", "A")
    assert len(in_range) == 1 and in_range[0]["farmer_phone"] == FARMER, in_range
    conn.close()
    print("  PASS — get_cooperative seam + display-name fallback (known/unknown)")
    passed += 1

    print(f"\n{passed} checks passed.")


if __name__ == "__main__":
    run()
