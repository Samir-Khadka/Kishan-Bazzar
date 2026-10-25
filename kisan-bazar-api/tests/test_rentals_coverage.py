"""
Coverage tests for the rentals + assistant-chat surface in
routes_services.py — the least-covered module in the suite (69%
before this file, per coverage.py over the full suite).

Until now the suite only exercised rentals through the seeded rows
(book / overlap / confirm / cancel in test_regressions_oct23.py
section G). These were never hit at all:

  * GET /v1/rentals (list + type/location filters + unavailable rows
    hidden) and POST /v1/rentals (create + every validation branch)
  * book-rental validation: non-integer days, days outside 1-30,
    missing/malformed book_date, unknown or unavailable rental
  * confirm/cancel of a nonexistent booking (404), cancel of an
    already-cancelled booking (409), owner-cancel path
  * assistant chat: the optional-auth personalization branch (a valid
    bearer token turns a ledger question into a personal answer
    instead of a login prompt) and the 30/min rate limit (429)

Run: python3 tests/test_rentals_coverage.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop("KISAN_BAZAR_DEMO_MODE", None)

import seed
import db
import auth
import app as app_module

SARITA = "9841000001"
RAM = "9841000002"
MINA = "9841000003"
KRISHNA = "9841000004"

passed = 0


def check(name, cond, extra=""):
    global passed
    assert cond, f"{name} {extra}"
    passed += 1
    print(f"  PASS — {name}")


def tok(phone):
    return {"Authorization": f"Bearer {auth.issue_token(phone)}"}


def set_available(rental_id, value):
    conn = db.get_db()
    conn.execute("UPDATE rentals SET available=? WHERE id=?", (value, rental_id))
    conn.commit()
    conn.close()


def run():
    seed.seed()
    client = app_module.app.test_client()

    # ---------- A. GET /v1/rentals: list + filters ----------
    print("\n=== A. List rentals ===")
    r = client.get("/v1/rentals")
    check("list rentals -> 200, seeded 4 rows", r.status_code == 200
          and len(r.get_json()) == 4, r.get_json())
    r = client.get("/v1/rentals?type=power_tiller")
    check("type filter -> only the power tiller",
          r.status_code == 200 and len(r.get_json()) == 1
          and r.get_json()[0]["machine_type"] == "power_tiller", r.get_json())
    r = client.get("/v1/rentals?location=Banepa")
    check("location filter -> only the Banepa machine",
          r.status_code == 200 and len(r.get_json()) == 1
          and r.get_json()[0]["location"] == "Banepa", r.get_json())
    r = client.get("/v1/rentals?type=tractor")
    check("type filter with no matches -> empty list",
          r.status_code == 200 and r.get_json() == [], r.get_json())
    r = client.get("/v1/rentals?location=Dhulikhel")
    check("location filter matches both Dhulikhel machines",
          r.status_code == 200 and len(r.get_json()) == 2, r.get_json())

    # ---------- B. POST /v1/rentals: create + validation ----------
    print("\n=== B. Create rental ===")
    body = {"machine_type": "sprayer", "machine_name": "Test Sprayer",
            "location": "Dhulikhel", "contact_phone": SARITA,
            "rate_per_day": 500}
    r = client.post("/v1/rentals", json=body)
    check("create without token -> 401", r.status_code == 401, r.get_json())
    r = client.post("/v1/rentals", json=body, headers=tok(SARITA))
    check("create with token -> 201 with rental_id",
          r.status_code == 201 and isinstance(r.get_json().get("rental_id"), int),
          r.get_json())
    new_id = r.get_json()["rental_id"]
    r = client.get("/v1/rentals?type=sprayer")
    check("new rental appears in the list",
          any(x["id"] == new_id for x in r.get_json()), r.get_json())
    for label, bad in [
        ("unknown machine_type", {**body, "machine_type": "combine"}),
        ("non-numeric rate", {**body, "rate_per_day": "abc"}),
        ("zero rate", {**body, "rate_per_day": 0}),
        ("negative rate", {**body, "rate_per_day": -50}),
        ("missing machine_name", {k: v for k, v in body.items()
                                  if k != "machine_name"}),
        ("missing location", {k: v for k, v in body.items()
                              if k != "location"}),
        ("missing contact_phone", {k: v for k, v in body.items()
                                   if k != "contact_phone"}),
    ]:
        r = client.post("/v1/rentals", json=bad, headers=tok(SARITA))
        check(f"create rejected: {label} -> 400", r.status_code == 400,
              r.get_json())

    # Unavailable rows are hidden from the list, not deleted.
    set_available(new_id, 0)
    r = client.get("/v1/rentals?type=sprayer")
    check("unavailable rental hidden from list",
          all(x["id"] != new_id for x in r.get_json()), r.get_json())
    set_available(new_id, 1)

    # ---------- C. Book validation ----------
    print("\n=== C. Book validation ===")
    conn = db.get_db()
    tiller = conn.execute(
        "SELECT * FROM rentals WHERE machine_type='power_tiller' "
        "AND available=1").fetchone()
    conn.close()
    rid, rate = tiller["id"], tiller["rate_per_day"]
    r = client.post(f"/v1/rentals/{rid}/book",
                    json={"days": 2, "book_date": "2026-12-01"})
    check("book without token -> 401", r.status_code == 401, r.get_json())
    for label, bad in [
        ("non-integer days", {"days": "two", "book_date": "2026-12-01"}),
        ("days = 0", {"days": 0, "book_date": "2026-12-01"}),
        ("days = 31", {"days": 31, "book_date": "2026-12-01"}),
        ("missing book_date", {"days": 2}),
        ("malformed book_date", {"days": 2, "book_date": "01/12/2026"}),
        ("impossible book_date", {"days": 2, "book_date": "2026-13-45"}),
    ]:
        r = client.post(f"/v1/rentals/{rid}/book", json=bad,
                        headers=tok(SARITA))
        check(f"book rejected: {label} -> 400", r.status_code == 400,
              r.get_json())
    r = client.post("/v1/rentals/999999/book",
                    json={"days": 2, "book_date": "2026-12-01"},
                    headers=tok(SARITA))
    check("book unknown rental -> 404", r.status_code == 404, r.get_json())
    set_available(new_id, 0)
    r = client.post(f"/v1/rentals/{new_id}/book",
                    json={"days": 2, "book_date": "2026-12-01"},
                    headers=tok(SARITA))
    check("book unavailable rental -> 404", r.status_code == 404,
          r.get_json())
    set_available(new_id, 1)

    r = client.post(f"/v1/rentals/{rid}/book",
                    json={"days": 2, "book_date": "2026-12-01"},
                    headers=tok(SARITA))
    check("happy-path booking -> 201, total = days x rate",
          r.status_code == 201 and r.get_json()["total"] == 2 * rate
          and r.get_json()["status"] == "requested", r.get_json())
    booking = r.get_json()["booking_id"]

    # ---------- D. Confirm/cancel edge cases ----------
    print("\n=== D. Confirm/cancel edge cases ===")
    r = client.post("/v1/rentals/bookings/999999/confirm",
                    headers=tok(RAM))
    check("confirm unknown booking -> 404", r.status_code == 404,
          r.get_json())
    r = client.post("/v1/rentals/bookings/999999/cancel",
                    headers=tok(SARITA))
    check("cancel unknown booking -> 404", r.status_code == 404,
          r.get_json())
    r = client.post(f"/v1/rentals/bookings/{booking}/cancel",
                    headers=tok(RAM))
    check("owner cancels a requested booking -> 200",
          r.status_code == 200 and r.get_json()["status"] == "cancelled",
          r.get_json())
    r = client.post(f"/v1/rentals/bookings/{booking}/cancel",
                    headers=tok(SARITA))
    check("cancel an already-cancelled booking -> 409",
          r.status_code == 409, r.get_json())
    r = client.post(f"/v1/rentals/bookings/{booking}/confirm",
                    headers=tok(RAM))
    check("confirm a cancelled booking -> 409", r.status_code == 409,
          r.get_json())

    # ---------- E. Assistant chat: optional auth ----------
    print("\n=== E. Assistant chat personalization ===")
    r = client.post("/v1/assistant/chat",
                    json={"message": "mero hisab herna",
                          "session_id": "cov-ledger-anon"})
    check("ledger question without token -> login prompt, not a crash",
          r.status_code == 200 and "login" in r.get_json()["reply"].lower(),
          r.get_json())
    r = client.post("/v1/assistant/chat",
                    json={"message": "mero hisab herna",
                          "session_id": "cov-ledger-auth"},
                    headers=tok(SARITA))
    # Seeded Sarita has no orders, so the honest personal answer is
    # "no pending payments" (Romanized here, since the question was) —
    # the point is it is a ledger answer, not the login prompt.
    check("ledger question with token -> personal answer, no login prompt",
          r.status_code == 200 and r.get_json()["intent"] == "ledger"
          and "login" not in r.get_json()["reply"].lower()
          and ("baaki chhaina" in r.get_json()["reply"]
               or "pending" in r.get_json()["reply"].lower()),
          r.get_json())

    # ---------- F. Assistant chat: rate limit (last: it blocks the IP) --
    print("\n=== F. Assistant chat rate limit ===")
    auth._attempts.clear()
    for i in range(30):
        r = client.post("/v1/assistant/chat",
                        json={"message": "namaste",
                              "session_id": f"cov-rate-{i}"})
        assert r.status_code == 200, (i, r.get_json())
    r = client.post("/v1/assistant/chat",
                    json={"message": "namaste", "session_id": "cov-rate-31"})
    check("31st chat in a minute -> 429", r.status_code == 429,
          r.get_json())

    print(f"\nAll {passed} rentals-coverage checks passed.")


if __name__ == "__main__":
    run()
