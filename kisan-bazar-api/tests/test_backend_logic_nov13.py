"""
Backend-logic fixes, 2026-11-13.

1. routes_auth.signup re-registration demoted a staff account's role:
   the UPDATE set role='farmer'/'buyer' while is_staff stayed 1, so
   role and is_staff disagreed afterwards (/v1/me reported role
   'farmer' with isStaff true; admin login returned role 'farmer'
   for the desk account) and the signup response itself lied with
   a hardcoded "is_staff": False. Staff now keep role='staff' on
   re-signup, and the response reports the stored is_staff value.
2. The same UPDATE silently dropped coop_id/lat/lon: a farmer who
   moved co-op or corrected their map pin got a 201 "account
   registered successfully" while the stored co-op and pin never
   moved — and the pin feeds the aggregation radius, so a stale pin
   silently excluded the farmer from pooling. Re-signup now updates
   coop_id, lat and lon like a fresh signup does.

Run: python3 tests/test_backend_logic_nov13.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import seed
import db
import auth
import app as app_module

SARITA = "9841000001"
STAFF = "9801100000"

passed = 0


def check(name, cond, extra=""):
    global passed
    assert cond, f"{name} {extra}"
    passed += 1
    print(f"  PASS — {name}")


def tok(phone):
    return {"Authorization": f"Bearer {auth.issue_token(phone)}"}


def run():
    seed.seed()
    client = app_module.app.test_client()
    auth._attempts.clear()

    # ---------- 1. staff re-signup keeps role='staff' ----------
    print("\n=== 1. Staff re-signup must not demote role ===")
    r = client.get("/v1/me", headers=tok(STAFF))
    check("staff starts as role staff + isStaff",
          r.get_json()["role"] == "staff" and r.get_json()["isStaff"] is True)
    r = client.post("/v1/auth/signup",
                    json={"phone": STAFF, "name": "Kavre Co-op Desk",
                          "role": "farmer", "coop_id": "kavre-veg",
                          "lat": 27.62, "lon": 85.54,
                          "land_type": "bari", "payout_method": "esewa"},
                    headers=tok(STAFF))
    check("staff re-signup 201", r.status_code == 201, str(r.status_code))
    check("signup response reports is_staff true for staff",
          r.get_json()["is_staff"] is True, str(r.get_json()))
    conn = db.get_db()
    row = conn.execute("SELECT role, is_staff FROM farmers WHERE phone=?",
                       (STAFF,)).fetchone()
    conn.close()
    check("stored role still 'staff' after re-signup with role=farmer",
          row["role"] == "staff" and row["is_staff"] == 1, str(dict(row)))
    r = client.get("/v1/me", headers=tok(STAFF))
    check("/v1/me agrees: role staff + isStaff",
          r.get_json()["role"] == "staff" and r.get_json()["isStaff"] is True,
          str(r.get_json()))

    # ---------- 2. farmer re-signup updates coop/pin ----------
    print("\n=== 2. Farmer re-signup updates coop_id/lat/lon ===")
    r = client.post("/v1/auth/signup",
                    json={"phone": SARITA, "name": "Sarita Tamang",
                          "role": "farmer", "coop_id": "kavre-veg",
                          "lat": 27.70, "lon": 85.60,
                          "land_type": "khet", "payout_method": "cash"},
                    headers=tok(SARITA))
    check("farmer re-signup 201", r.status_code == 201, str(r.status_code))
    check("signup response reports is_staff false for a farmer",
          r.get_json()["is_staff"] is False, str(r.get_json()))
    conn = db.get_db()
    row = conn.execute(
        "SELECT lat, lon, land_type FROM farmers WHERE phone=?",
        (SARITA,)).fetchone()
    conn.close()
    check("stored lat/lon moved to the new pin",
          row["lat"] == 27.70 and row["lon"] == 85.60, str(dict(row)))
    check("land_type still updated alongside",
          row["land_type"] == "khet", str(dict(row)))

    # ---------- 3. ordinary farmer role change still works ----------
    print("\n=== 3. Non-staff role change still honoured ===")
    r = client.post("/v1/auth/signup",
                    json={"phone": SARITA, "name": "Sarita Tamang",
                          "role": "buyer", "coop_id": "kavre-veg",
                          "lat": 27.70, "lon": 85.60,
                          "land_type": "khet", "payout_method": "cash"},
                    headers=tok(SARITA))
    check("farmer->buyer re-signup 201", r.status_code == 201,
          str(r.status_code))
    conn = db.get_db()
    row = conn.execute("SELECT role, is_staff FROM farmers WHERE phone=?",
                       (SARITA,)).fetchone()
    conn.close()
    check("non-staff role change applied, is_staff untouched",
          row["role"] == "buyer" and row["is_staff"] == 0, str(dict(row)))

    print(f"\n{passed} passed, 0 failed.")


if __name__ == "__main__":
    run()
