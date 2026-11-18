"""
Performance regression tests for the 2026-11-18 pass.

Every fix here was verified live before it was made:

  1. farmer_dues pulled every (order, lot_listing) pair for the
     farmer into Python and prorated in a loop — a farmer with 40
     lots x 25 orders made every ledger chat transfer 1,000 joined
     rows, growing linearly with lifetime volume. The proration is
     now aggregated in SQL (SUM ... GROUP BY settlement state), so
     the call returns at most 2 rows. Values must stay identical to
     the old Python shape (test_aplus pins 2769.23 / 3600.00 on the
     same seed flow; here we pin values against an independent
     Python reference computed from the raw joined rows), the
     zero-pooled guard must still hold, and the EXPLAIN plan must
     stay SCAN-free with both indexes in use.
  2. Six public, unauthenticated read endpoints were served with
     Cache-Control: no-store (the after_request default) even
     though the website and the app hit them on every load — only
     /v1/stats already carried public, max-age=60. They now carry
     the same 60s public cache; authenticated/user-scoped responses
     must keep no-store.

Run: python3 tests/test_performance_nov18.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3

import seed
import db
import app as app_module
import auth
import repositories

SARITA = "9841000001"
RAM = "9841000002"
STAFF = "9801100000"
GHOST = "9841000999"   # valid-format phone with NO farmer record


def explain(conn, sql, params=()):
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


def tok(phone):
    return {"Authorization": f"Bearer {auth.issue_token(phone)}"}


def pooled_lot(client, lot_id="TST-910"):
    """Sarita 80 kg + Ram 50 kg pooled into one ready lot @ Rs 45."""
    conn = db.get_db()
    conn.execute(
        "INSERT INTO lots (id, coop_id, crop_code, grade, target_kg, pooled_kg, rate_per_kg, status) "
        "VALUES (?, 'kavre-veg', 'KAULI', 'A', 130, 130, 45, 'ready')", (lot_id,))
    for phone, kg in ((SARITA, 80), (RAM, 50)):
        cur = conn.execute(
            "INSERT INTO listings (farmer_phone, coop_id, crop_code, grade, quantity_kg, quantity_committed_kg, status) "
            "VALUES (?, 'kavre-veg', 'KAULI', 'A', ?, ?, 'pooled')", (phone, kg, kg))
        conn.execute(
            "INSERT INTO lot_listings (lot_id, listing_id, kg_contributed) VALUES (?, ?, ?)",
            (lot_id, cur.lastrowid, kg))
    conn.commit()
    conn.close()


def python_reference_dues(conn, phone):
    """The OLD shape, computed independently in the test from the raw
    joined rows: prorate each (order, lot_listing) pair in Python and
    split by settlement state. farmer_dues must agree with this."""
    rows = conn.execute(
        "SELECT o.quantity_kg AS qty, o.unit_rate AS rate, "
        "ll.kg_contributed AS contrib, lot.pooled_kg AS pooled, "
        "(s.id IS NOT NULL) AS is_settled "
        "FROM listings li "
        "JOIN lot_listings ll ON ll.listing_id = li.id "
        "JOIN lots lot ON lot.id = ll.lot_id "
        "JOIN orders o ON o.lot_id = lot.id "
        "LEFT JOIN order_settlements s ON s.order_id = o.id "
        "WHERE li.farmer_phone=?", (phone,)).fetchall()
    dues = {"pending": 0.0, "settled": 0.0}
    for r in rows:
        pooled = r["pooled"] or 0
        if pooled <= 0:
            continue
        share = r["qty"] * r["rate"] * (r["contrib"] / pooled)
        dues["settled" if r["is_settled"] else "pending"] += share
    return dues


def run():
    seed.seed()
    client = app_module.app.test_client()
    auth._attempts.clear()
    auth._dev_outbox.clear()
    passed = 0

    print("=== 1. farmer_dues matches the old Python shape (pending + settled) ===")
    pooled_lot(client)
    r = client.post("/v1/orders",
                    json={"buyer_name": "Hotel Buyer", "buyer_phone": GHOST,
                          "lot_id": "TST-910", "quantity_kg": 100},
                    headers=tok(GHOST))
    assert r.status_code == 201, r.get_json()
    order_id = r.get_json()["order_id"]
    r = client.post(f"/v1/orders/{order_id}/settle",
                    json={"amount": 4500, "method": "esewa"}, headers=tok(STAFF))
    assert r.status_code == 201, r.get_json()
    # A second, unsettled order on the same lot: pending and settled both non-zero.
    r = client.post("/v1/orders",
                    json={"buyer_name": "Second Buyer", "buyer_phone": GHOST,
                          "lot_id": "TST-910", "quantity_kg": 30},
                    headers=tok(GHOST))
    assert r.status_code == 201, r.get_json()
    conn = db.get_db()
    dues = repositories.farmer_dues(conn, SARITA)
    ref = python_reference_dues(conn, SARITA)
    conn.close()
    # Sarita's share is 80/130 of each order: 4500*80/130 settled +
    # 1350*80/130 pending. The SQL SUM must agree with the Python loop.
    assert abs(dues["pending"] - ref["pending"]) < 0.01, (dues, ref)
    assert abs(dues["settled"] - ref["settled"]) < 0.01, (dues, ref)
    assert abs(dues["pending"] - 830.77) < 0.01, dues
    assert abs(dues["settled"] - 2769.23) < 0.01, dues
    print(f"  PASS — dues {dues} == reference {ref}")
    passed += 1

    print("=== 2. farmer_dues aggregates in SQL: <= 2 rows for 1,000 joined pairs ===")
    conn = db.get_db()
    for i in range(40):
        lot_id = f"BENCH-{i:03d}"
        cur = conn.execute(
            "INSERT INTO listings (farmer_phone, coop_id, crop_code, grade, quantity_kg) "
            "VALUES (?,?,?,?,?)", (SARITA, "kavre-veg", "GOLB", "A", 130))
        conn.execute(
            "INSERT INTO lots (id, coop_id, crop_code, grade, target_kg, pooled_kg, "
            "rate_per_kg, status) VALUES (?,?,?,?,?,?,?,?)",
            (lot_id, "kavre-veg", "GOLB", "A", 130, 130, 50.0, "ready"))
        conn.execute(
            "INSERT INTO lot_listings (lot_id, listing_id, kg_contributed) VALUES (?,?,?)",
            (lot_id, cur.lastrowid, 130))
        conn.executemany(
            "INSERT INTO orders (buyer_name, buyer_phone, lot_id, quantity_kg, unit_rate) "
            "VALUES (?,?,?,?,?)",
            [("Bench Buyer", "9800000000", lot_id, 1, 50.0)] * 25)
    conn.commit()
    joined = conn.execute(
        "SELECT COUNT(*) AS c FROM listings li "
        "JOIN lot_listings ll ON ll.listing_id = li.id "
        "JOIN lots lot ON lot.id = ll.lot_id "
        "JOIN orders o ON o.lot_id = lot.id "
        "WHERE li.farmer_phone=?", (SARITA,)).fetchone()["c"]
    assert joined >= 1000, joined
    sql = captured_sql(conn, lambda: repositories.farmer_dues(conn, SARITA),
                       "FROM listings li")
    assert "GROUP BY" in sql, sql
    # Python 3.12 expands bound parameters into the traced SQL; older
    # versions leave '?' placeholders (same note as test_performance_nov10).
    rows = conn.execute(sql, (SARITA,)).fetchall() if "?" in sql \
        else conn.execute(sql).fetchall()
    assert len(rows) <= 2, f"farmer_dues must return <= 2 rows, got {len(rows)}"
    dues = repositories.farmer_dues(conn, SARITA)
    ref = python_reference_dues(conn, SARITA)
    conn.close()
    assert abs(dues["pending"] - ref["pending"]) < 0.01, (dues, ref)
    assert abs(dues["settled"] - ref["settled"]) < 0.01, (dues, ref)
    print(f"  PASS — {joined} joined pairs in, {len(rows)} rows out, values agree")
    passed += 1

    print("=== 3. farmer_dues plan still SCAN-free, both indexes in use ===")
    conn = db.get_db()
    # GHOST has no farmer record and no listings at all, so its dues
    # are exactly zero (RAM already has orders from check 1 above).
    sql = captured_sql(conn, lambda: repositories.farmer_dues(conn, GHOST),
                       "FROM listings li")
    plan = explain(conn, sql, (GHOST,))
    print("   plan:", " | ".join(plan))
    assert not any("SCAN" in p for p in plan), f"farmer_dues still scans: {plan}"
    assert any("idx_lot_listings_listing" in p for p in plan), plan
    assert any("idx_listings_farmer_created" in p for p in plan), plan
    dues = repositories.farmer_dues(conn, GHOST)
    assert dues == {"pending": 0.0, "settled": 0.0}, dues
    conn.close()
    print("  PASS — no SCAN; a farmer with no orders owes/receives nothing")
    passed += 1

    print("=== 4. zero-pooled lot still contributes nothing ===")
    conn = db.get_db()
    before = repositories.farmer_dues(conn, SARITA)
    cur = conn.execute(
        "INSERT INTO listings (farmer_phone, coop_id, crop_code, grade, "
        "quantity_kg) VALUES (?,?,?,?,?)",
        (SARITA, "kavre-veg", "GOLB", "A", 40))
    conn.execute(
        "INSERT INTO lots (id, coop_id, crop_code, grade, target_kg, "
        "pooled_kg, rate_per_kg, status) "
        "VALUES ('ZERO-1','kavre-veg','GOLB','A',40,0,NULL,'forming')")
    conn.execute(
        "INSERT INTO lot_listings (lot_id, listing_id, kg_contributed) "
        "VALUES ('ZERO-1',?,40)", (cur.lastrowid,))
    conn.execute(
        "INSERT INTO orders (buyer_name, buyer_phone, lot_id, "
        "quantity_kg, unit_rate) VALUES ('Buyer',?, 'ZERO-1', 10, 50)",
        (GHOST,))
    conn.commit()
    after = repositories.farmer_dues(conn, SARITA)
    conn.close()
    assert after == before, f"before={before} after={after}"
    print("  PASS — zero-pooled order contributes nothing, no exception")
    passed += 1

    print("=== 5. public read endpoints carry public, max-age=60 ===")
    for path in ("/v1/prices/latest", "/v1/prices/trends",
                 "/v1/market/lots", "/v1/lots", "/v1/lots/MST-012",
                 "/v1/rentals", "/v1/stats"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.get_json())
        assert r.headers.get("Cache-Control") == "public, max-age=60", \
            (path, r.headers.get("Cache-Control"))
    # Bodies unchanged: latest is a per-crop dict, market lots a list.
    latest = client.get("/v1/prices/latest").get_json()
    assert isinstance(latest, dict) and latest, latest
    market = client.get("/v1/market/lots").get_json()
    assert isinstance(market, list) and market, market
    print("  PASS — 7 public endpoints cacheable for 60s, bodies unchanged")
    passed += 1

    print("=== 6. authenticated / error responses keep no-store ===")
    r = client.get("/v1/orders/mine", headers=tok(GHOST))
    assert r.status_code == 200, r.get_json()
    assert r.headers.get("Cache-Control") == "no-store", r.headers.get("Cache-Control")
    r = client.get("/v1/admin/overview", headers=tok(STAFF))
    assert r.status_code == 200, r.get_json()
    assert r.headers.get("Cache-Control") == "no-store", r.headers.get("Cache-Control")
    r = client.get("/v1/lots/NOPE-1")
    assert r.status_code == 404, r.get_json()
    assert r.headers.get("Cache-Control") == "no-store", r.headers.get("Cache-Control")
    print("  PASS — user-scoped and error responses are not cacheable")
    passed += 1

    print(f"\nAll {passed} performance checks passed.")


if __name__ == "__main__":
    run()
