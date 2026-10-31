"""
Architecture regression tests for the 2026-10-31 daily-improvement run
(focus: code architecture). Two real layering issues, both with no
intended behaviour change:

  1. aggregation._form_lot_locked committed inside the module, breaking
     the repo's transaction-ownership rule (repositories.py: "functions
     take an open connection and never commit"). A caller holding its own
     BEGIN IMMEDIATE lost its transaction mid-flight — their rollback
     could no longer undo earlier writes. check_and_form_lot now owns
     the commit (only when it started the transaction); the inner
     function never commits.
  2. The Nepali-mobile rule (9[678]\\d{8}) existed as two separate inline
     regexes in routes_market.py (orders + offers) — the same drift risk
     the web.py seam was created for. Both now call
     web.is_valid_nepali_mobile().

Run: python3 tests/test_architecture_oct31.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aggregation
import auth
import db
import seed
import web
import app as app_module

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

    # ---------- A. check_and_form_lot must not commit the caller's transaction ----------
    print("\n=== A. transaction ownership in aggregation ===")
    conn = db.get_db()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO listings (farmer_phone, coop_id, crop_code, grade, quantity_kg) "
        "VALUES (?,?,?,?,?)",
        ("9841000001", "kavre-veg", "KAULI", "A", 50))
    # Tiny target so a lot forms from this one listing.
    result = aggregation.check_and_form_lot(conn, "kavre-veg", "KAULI", "A", 1)
    check("lot formed", result is not None and result["pooled_kg"] > 0, result)
    check("caller's transaction is still open (no inner commit)",
          conn.in_transaction)
    lot_id = result["lot_id"]
    conn.rollback()
    conn.close()
    probe = db.get_db()
    check("rollback undoes the formed lot",
          probe.execute("SELECT 1 FROM lots WHERE id=?", (lot_id,)).fetchone() is None)
    check("rollback undoes the caller's listing insert",
          probe.execute(
              "SELECT COUNT(*) AS c FROM listings "
              "WHERE farmer_phone='9841000001' AND quantity_kg=50").fetchone()["c"] == 0)
    probe.close()

    # Standalone call (no caller transaction) still commits its own work.
    conn = db.get_db()
    conn.execute(
        "INSERT INTO listings (farmer_phone, coop_id, crop_code, grade, quantity_kg) "
        "VALUES (?,?,?,?,?)",
        ("9841000002", "kavre-veg", "KAULI", "A", 50))
    conn.commit()
    result = aggregation.check_and_form_lot(conn, "kavre-veg", "KAULI", "A", 1)
    conn.close()
    probe = db.get_db()
    check("standalone call still persists the formed lot",
          probe.execute("SELECT 1 FROM lots WHERE id=?",
                        (result["lot_id"],)).fetchone() is not None)
    probe.close()

    # ---------- B. one shared Nepali-mobile validator ----------
    print("\n=== B. is_valid_nepali_mobile ===")
    for good in ("9841000001", "9741000001", "9641000001", " 9841000001 "):
        check(f"valid: {good!r}", web.is_valid_nepali_mobile(good))
    for bad in ("9541000001", "984100000", "98410000011", "", None,
                "98abc00001", "+9779841000001", 9841000001.5):
        check(f"invalid: {bad!r}", not web.is_valid_nepali_mobile(bad))

    # ---------- C. both money-path endpoints use the shared validator ----------
    print("\n=== C. endpoints reject invalid buyer_phone via the helper ===")
    client = app_module.app.test_client()
    # Malformed phone passes require_self (token issued for it) but must
    # fail the format check BEFORE any lot lookup.
    r = client.post("/v1/offers",
                    json={"buyer_name": "X", "buyer_phone": "98abc",
                          "lot_id": "NOPE", "bid_rate_per_kg": 10,
                          "quantity_kg": 1},
                    headers=tok("98abc"))
    check("offer with malformed buyer_phone -> 400",
          r.status_code == 400
          and "valid Nepali mobile" in r.get_json().get("error", ""),
          r.get_json())
    r = client.post("/v1/orders",
                    json={"buyer_name": "X", "buyer_phone": "98abc",
                          "lot_id": "NOPE", "quantity_kg": 1},
                    headers=tok("98abc"))
    check("order with malformed buyer_phone -> 400",
          r.status_code == 400
          and "valid Nepali mobile" in r.get_json().get("error", ""),
          r.get_json())
    # A valid phone must get PAST the format gate (unknown lot -> 404).
    r = client.post("/v1/orders",
                    json={"buyer_name": "X", "buyer_phone": "9841000099",
                          "lot_id": "NOPE", "quantity_kg": 1},
                    headers=tok("9841000099"))
    check("order with valid phone reaches the lot lookup (404)",
          r.status_code == 404, r.get_json())

    print(f"\n{passed} checks passed.")


if __name__ == "__main__":
    run()
