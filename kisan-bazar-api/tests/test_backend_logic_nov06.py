"""
Backend-logic fixes, 2026-11-06.

1. web.idempotent stored EVERY response, including transient ones: a
   429 (rate limit) or 5xx replayed from the idempotency table for 24h,
   so a client retrying with the same key kept getting the stale failure
   long after the window cleared / the server recovered. Transient
   responses must never be stored — the claim is released so the retry
   runs the handler again. Deterministic responses (2xx, other 4xx)
   are still stored.
2. repositories.admin_overview returned farmers UNCAPPED (the docstring
   claims row payloads are capped) and counts.farmers was len(farmers):
   past 500 farmers the dashboard payload grew without bound and the
   count would have undercounted had the rows ever been capped. Farmers
   are now capped at 500 rows; the count comes from COUNT(*).
3. routes_auth.signup stored lat/lon verbatim with no range check: a
   lat of 9999 silently dropped the farmer out of every co-op's
   collection radius, and NaN ("nan" parses as a float) became NULL on
   INSERT and 500'd on the NOT NULL column.

Run: python3 tests/test_backend_logic_nov06.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, jsonify

import seed
import db
import auth
import app as app_module
import web

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

    # ---------- 1. transient idempotent responses are not stored ----------
    print("\n=== 1. Idempotency: 429/5xx released, 2xx stored ===")
    stub = Flask(__name__)
    calls = {"n": 0}

    @stub.route("/idem-x", methods=["POST"])
    @web.idempotent
    def idem_x():
        calls["n"] += 1
        if calls["n"] == 1:
            return jsonify({"error": "slow down"}), 429
        if calls["n"] == 2:
            return jsonify({"error": "boom"}), 500
        return jsonify({"ok": True}), 200

    sclient = stub.test_client()
    key = {"Idempotency-Key": "nov06-transient-1"}
    r = sclient.post("/idem-x", headers=key)
    check("first call returns the 429", r.status_code == 429, str(r.status_code))
    r = sclient.post("/idem-x", headers=key)
    check("retry after 429 runs fresh (500, not replayed 429)",
          r.status_code == 500, str(r.status_code))
    r = sclient.post("/idem-x", headers=key)
    check("retry after 500 runs fresh (200, not replayed 500)",
          r.status_code == 200 and r.get_json() == {"ok": True},
          str(r.status_code))
    n_after_success = calls["n"]
    r = sclient.post("/idem-x", headers=key)
    check("200 IS stored: retry replays without re-running the handler",
          r.status_code == 200 and calls["n"] == n_after_success,
          f"calls={calls['n']} expected={n_after_success}")

    # ---------- 2. admin overview farmers cap + exact count ----------
    print("\n=== 2. Admin overview: farmers capped, count exact ===")
    r = client.get("/v1/admin/overview", headers=tok(STAFF))
    j = r.get_json()
    check("seed-only: counts.farmers matches rows returned",
          j["counts"]["farmers"] == len(j["farmers"]) == 6,
          f"counts={j['counts']['farmers']} rows={len(j['farmers'])}")
    conn = db.get_db()
    conn.executemany(
        "INSERT OR IGNORE INTO farmers (phone, name, role, coop_id, lat, lon) "
        "VALUES (?,?,?,?,?,?)",
        [(f"999000{i:04d}", f"Bulk {i}", "farmer", "kavre-veg", 27.6, 85.5)
         for i in range(501)])
    conn.commit()
    conn.close()
    r = client.get("/v1/admin/overview", headers=tok(STAFF))
    j = r.get_json()
    check("farmers payload capped at 500 past 507 farmers",
          len(j["farmers"]) == 500, f"rows={len(j['farmers'])}")
    check("counts.farmers still exact (507) past the cap",
          j["counts"]["farmers"] == 507,
          f"counts={j['counts']['farmers']}")

    # ---------- 3. signup lat/lon range ----------
    print("\n=== 3. Signup: lat/lon range validated ===")
    base = {"phone": SARITA, "name": "Sarita", "role": "farmer",
            "coop_id": "kavre-veg"}
    for lat, lon, label in [(9999, 85.5, "lat=9999"),
                            (-91, 85.5, "lat=-91"),
                            (27.6, 9999, "lon=9999"),
                            (27.6, -181, "lon=-181"),
                            ("nan", 85.5, 'lat="nan"')]:
        r = client.post("/v1/auth/signup",
                        json={**base, "lat": lat, "lon": lon},
                        headers=tok(SARITA))
        check(f"{label} rejected with 400", r.status_code == 400,
              str(r.status_code))
    new_phone = "9841000999"
    r = client.post("/v1/auth/signup",
                    json={"phone": new_phone, "name": "Edge", "role": "farmer",
                          "coop_id": "kavre-veg", "lat": 90, "lon": 180},
                    headers=tok(new_phone))
    check("edge lat=90/lon=180 accepted", r.status_code == 201,
          str(r.status_code))
    r = client.post("/v1/auth/signup",
                    json={"phone": "9841000998", "name": "NoCoords",
                          "role": "farmer", "coop_id": "kavre-veg"},
                    headers=tok("9841000998"))
    check("omitted lat/lon still accepted (defaults)",
          r.status_code == 201, str(r.status_code))

    print(f"\n{passed} passed, 0 failed.")


if __name__ == "__main__":
    run()
