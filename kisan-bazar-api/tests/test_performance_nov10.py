"""
Performance regression tests for the 2026-11-10 pass.

Every fix here was verified live with EXPLAIN QUERY PLAN (or a real
response header) before it was made:

  1. farmer_dues full-scanned lot_listings (EXPLAIN: SCAN ll) on every
     ledger/dues read. The query now drives from the farmer's listings
     through the new idx_lot_listings_listing — the plan must contain
     no SCAN, and the figures must stay identical (test_aplus pins
     2769.23 / 3600.00 on the same seed flow; here we pin the plan).
  2. open_listings_in_radius (run after EVERY new listing) fetched all
     listings of a crop via idx_listings_crop, then filtered coop/grade/
     status in Python. idx_listings_pool must serve the whole predicate.
  3. /v1/listings/mine, /v1/rentals and land-scan history returned
     unbounded rows. All three are now capped at 200.
  4. Static files (/site/*, /app) were served with Cache-Control:
     no-cache (Flask's send_file default), so every navigation
     revalidated every asset. They now carry public, max-age=3600,
     while API JSON keeps no-store.

Run: python3 tests/test_performance_nov10.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3

import seed
import db
import app as app_module
import auth
import repositories
import aggregation

RAM = "9841000002"  # Ram Bahadur K.C. — a seeded farmer with no orders


def explain(conn, sql, params=()):
    """EXPLAIN QUERY PLAN for a statement captured via the trace
    callback. Python 3.12 expands bound parameters into the traced
    SQL, older versions leave '?' placeholders — handle both."""
    try:
        return [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql)]
    except sqlite3.ProgrammingError:
        return [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]


def captured_sql(conn, fn, marker):
    seen = []
    conn.set_trace_callback(lambda s: seen.append(s))
    try:
        fn()
    finally:
        conn.set_trace_callback(None)
    matches = [s for s in seen if marker in s]
    assert matches, f"no traced SQL containing {marker!r}: {seen}"
    return matches[0]


def run():
    seed.seed()
    client = app_module.app.test_client()
    passed = 0

    print("=== 1. farmer_dues plan: every step an index seek, no SCAN ===")
    conn = db.get_db()
    sql = captured_sql(
        conn,
        lambda: repositories.farmer_dues(conn, RAM),
        "FROM listings li")
    plan = explain(conn, sql, (RAM,))
    print("   plan:", " | ".join(plan))
    assert not any("SCAN" in p for p in plan), f"farmer_dues still scans: {plan}"
    assert any("idx_lot_listings_listing" in p for p in plan), plan
    assert any("idx_listings_farmer_created" in p for p in plan), plan
    dues = repositories.farmer_dues(conn, RAM)
    assert dues == {"pending": 0.0, "settled": 0.0}, dues
    conn.close()
    print("  PASS — no SCAN; a farmer with no orders owes/receives nothing")
    passed += 1

    print("\n=== 2. open_listings_in_radius seeks idx_listings_pool ===")
    conn = db.get_db()
    sql = captured_sql(
        conn,
        lambda: aggregation.open_listings_in_radius(conn, "kavre-veg", "KAULI", "A"),
        "FROM listings l")
    plan = explain(conn, sql, ("kavre-veg", "KAULI", "A"))
    print("   plan:", " | ".join(plan))
    assert any("idx_listings_pool" in p for p in plan), plan
    assert not any("SCAN" in p for p in plan), plan
    conn.close()
    print("  PASS — coop+crop+grade+status served by one index seek")
    passed += 1

    print("\n=== 3. /v1/listings/mine is capped at 200 ===")
    conn = db.get_db()
    conn.executemany(
        "INSERT INTO listings (farmer_phone, coop_id, crop_code, grade, "
        "quantity_kg, status) VALUES (?,?,?,?,?,?)",
        [(RAM, "kavre-veg", "KAULI", "A", 5.0, "open")] * 210,
    )
    conn.commit()
    conn.close()
    r = client.get("/v1/listings/mine",
                   headers={"Authorization": f"Bearer {auth.issue_token(RAM)}"})
    assert r.status_code == 200, r.get_json()
    assert len(r.get_json()) == 200, f"mine must cap at 200, got {len(r.get_json())}"
    print("  PASS — 210 listings in, exactly 200 out")
    passed += 1

    print("\n=== 4. /v1/rentals is capped at 200 ===")
    conn = db.get_db()
    conn.executemany(
        "INSERT INTO rentals (owner_phone, owner_name, machine_type, "
        "machine_name, rate_per_day, location, contact_phone, available) "
        "VALUES (?,?,?,?,?,?,?,1)",
        [(RAM, "Ram", "sprayer", "Sprayer", 500.0, "Banepa", RAM)] * 205,
    )
    conn.commit()
    conn.close()
    r = client.get("/v1/rentals")
    assert r.status_code == 200, r.get_json()
    assert len(r.get_json()) == 200, f"rentals must cap at 200, got {len(r.get_json())}"
    print("  PASS — 205+ available rentals in, exactly 200 out")
    passed += 1

    print("\n=== 5. land-scan history is capped at 200 ===")
    conn = db.get_db()
    conn.executemany(
        "INSERT INTO land_scans (farmer_phone, land_type, confidence) "
        "VALUES (?,?,?)",
        [(RAM, "khet", "good")] * 210,
    )
    conn.commit()
    rows = repositories.land_scan_history(conn, RAM)
    conn.close()
    assert len(rows) == 200, f"history must cap at 200, got {len(rows)}"
    print("  PASS — 210 scans in, exactly 200 out")
    passed += 1

    print("\n=== 6. static files cacheable for 1h, API JSON stays no-store ===")
    for path in ("/site/", "/site/index.html", "/site/site.js", "/app"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.status_code)
        assert r.headers.get("Cache-Control") == "public, max-age=3600", (
            path, r.headers.get("Cache-Control"))
    for path in ("/v1/health", "/v1/prices/latest"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.status_code)
        assert r.headers.get("Cache-Control") == "no-store", (
            path, r.headers.get("Cache-Control"))
    print("  PASS — /site/* + /app: public, max-age=3600; API: no-store")
    passed += 1

    print("\n=== 7. new indexes exist, redundant single-column ones are gone ===")
    conn = db.get_db()
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    for name in ("idx_listings_pool", "idx_listings_farmer_created",
                 "idx_diagnoses_farmer_created", "idx_lot_listings_listing",
                 "idx_rentals_available_created"):
        assert name in have, f"missing index {name}"
    for name in ("idx_listings_farmer", "idx_diagnoses_farmer"):
        assert name not in have, f"redundant index {name} still present"
    conn.close()
    print("  PASS — 5 new indexes present, 2 redundant ones dropped")
    passed += 1

    print(f"\nAll {passed} performance checks passed.")


if __name__ == "__main__":
    run()
