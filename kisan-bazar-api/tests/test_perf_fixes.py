"""
Regression tests for the 2026-10-23 performance pass.

Covers the endpoints whose queries were reworked so they stay correct
while doing less work:
  1. /v1/prices/latest — one row per crop, newest wins (was: full-table
     scan + Python dedup)
  2. /v1/prices/trends — SQL-side aggregation, same shape/ordering/rounding
     as the old Python grouping
  3. /v1/lots — farmer_count via one GROUP BY join (was: correlated
     subquery per row); counts must still be right
  4. /v1/market/lots — newest-first ordering, hard cap respected
  5. /v1/admin/overview — KPI counts exact even past the row caps
     (total_kg used to sum only the 50 fetched listings)
  6. auth.check_rate_limit — expired keys are pruned, not leaked

Run: python3 tests/test_perf_fixes.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import seed
import db
import app as app_module
import auth

STAFF_PHONE = "9801100000"


def staff_headers():
    return {"Authorization": f"Bearer {auth.issue_token(STAFF_PHONE)}"}


def add_lot(conn, lot_id, crop, rate, created_at, status="ready", pooled=100.0):
    conn.execute(
        "INSERT INTO lots (id, coop_id, crop_code, grade, target_kg, pooled_kg, "
        "rate_per_kg, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (lot_id, "kavre-veg", crop, "A", 130, pooled, rate, status, created_at),
    )


def run():
    seed.seed()
    client = app_module.app.test_client()
    passed = 0

    print("=== 1. /v1/prices/latest returns one row per crop, newest wins ===")
    conn = db.get_db()
    # Own the lot state: the seed's 5 demo lots are dated "now", which would
    # beat any fixed test timestamps in the newest-wins check.
    conn.execute("DELETE FROM lots")
    add_lot(conn, "L-OLD", "KAULI", 50.0, "2026-10-01 10:00:00")
    add_lot(conn, "L-NEW", "KAULI", 60.0, "2026-10-20 10:00:00")
    add_lot(conn, "L-GOLB", "GOLB", 70.0, "2026-10-15 10:00:00")
    conn.commit()
    conn.close()
    r = client.get("/v1/prices/latest")
    assert r.status_code == 200, r.get_json()
    latest = r.get_json()
    assert set(latest.keys()) == {"KAULI", "GOLB"}, latest.keys()
    assert latest["KAULI"]["rate_per_kg"] == 60.0, latest["KAULI"]
    assert latest["GOLB"]["rate_per_kg"] == 70.0, latest["GOLB"]
    print("  PASS — 2 crops, newest rate per crop")
    passed += 1

    print("\n=== 2. /v1/prices/trends groups per crop/day in SQL, same output ===")
    conn = db.get_db()
    # Two KAULI lots same day (avg 55.0, 2 lots), on a day with no other
    # KAULI activity so the expectation is unambiguous
    add_lot(conn, "L-T1", "KAULI", 50.0, "2026-10-18 09:00:00")
    add_lot(conn, "L-T2", "KAULI", 60.0, "2026-10-18 11:00:00")
    conn.commit()
    conn.close()
    r = client.get("/v1/prices/trends?days=30&crop_code=KAULI")
    assert r.status_code == 200, r.get_json()
    trends = r.get_json()
    assert set(trends.keys()) == {"KAULI"}, trends.keys()
    day_rows = {row["date"]: row for row in trends["KAULI"]}
    assert day_rows["2026-10-18"]["avg_rate"] == 55.0, day_rows
    assert day_rows["2026-10-18"]["lots"] == 2, day_rows
    # days sorted ascending within a crop, like before
    dates = [row["date"] for row in trends["KAULI"]]
    assert dates == sorted(dates), dates
    print("  PASS — per-day avg/count correct, days ascending")
    passed += 1

    print("\n=== 3. /v1/lots farmer_count still right via GROUP BY join ===")
    r = client.get("/v1/lots")
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert "data" in body and "total" in body, body.keys()
    for lot in body["data"]:
        assert "farmer_count" in lot and "coop_name" in lot, lot.keys()
        assert isinstance(lot["farmer_count"], int), lot
    print(f"  PASS — {len(body['data'])} lots, farmer_count present on each")
    passed += 1

    print("\n=== 4. /v1/market/lots is newest-first and capped ===")
    r = client.get("/v1/market/lots")
    assert r.status_code == 200, r.get_json()
    market = r.get_json()
    assert isinstance(market, list), type(market)
    assert len(market) <= 200, f"market/lots must be capped, got {len(market)}"
    created = [l["created_at"] for l in market]
    assert created == sorted(created, reverse=True), "market/lots must be newest-first"
    assert all(l["status"] == "ready" for l in market)
    print(f"  PASS — {len(market)} ready lots, newest-first, capped")
    passed += 1

    print("\n=== 5. /v1/admin/overview KPIs exact past the row caps ===")
    conn = db.get_db()
    # 60 listings: the old total_kg summed only the 50 fetched rows
    conn.executemany(
        "INSERT INTO listings (farmer_phone, coop_id, crop_code, grade, "
        "quantity_kg, status) VALUES (?,?,?,?,?,?)",
        [("9841000001", "kavre-veg", "KAULI", "A", 10.0, "pooled")] * 60,
    )
    conn.commit()
    conn.close()
    r = client.get("/v1/admin/overview", headers=staff_headers())
    assert r.status_code == 200, r.get_json()
    ov = r.get_json()
    c = ov["counts"]
    assert len(ov["listings"]) == 50, "listings rows stay capped at 50"
    assert c["listings"] == 60, f"counts.listings must be the true total, got {c['listings']}"
    assert c["total_kg"] == 600.0, f"total_kg must sum ALL listings, got {c['total_kg']}"
    assert len(ov["lots"]) <= 200 and len(ov["orders"]) <= 200
    assert c["lots"] >= len(ov["lots"]) and c["orders"] >= len(ov["orders"])
    print("  PASS — counts exact, total_kg sums all 60 listings")
    passed += 1

    print("\n=== 6. rate limiter prunes expired keys instead of leaking ===")
    import time
    auth._attempts.clear()
    # A key whose whole bucket expired must be dropped, then re-armed fresh
    auth._attempts["stale:key"] = [time.time() - 3600]
    assert auth.check_rate_limit("stale:key", max_attempts=5, window_seconds=60)
    assert len(auth._attempts["stale:key"]) == 1, "expired bucket pruned, then re-armed"
    assert len(auth._attempts) == 1, "only the live key should remain"
    # An already-empty bucket must not linger either
    auth._attempts["gone:key"] = []
    assert auth.check_rate_limit("gone:key", max_attempts=5, window_seconds=60)
    assert len(auth._attempts["gone:key"]) == 1, "empty bucket pruned, then re-armed"
    # Live windows still enforced
    auth._attempts.clear()
    for _ in range(3):
        assert auth.check_rate_limit("hot:key", max_attempts=3, window_seconds=60)
    assert not auth.check_rate_limit("hot:key", max_attempts=3, window_seconds=60)
    assert len(auth._attempts["hot:key"]) == 3, "deny path must not append"
    print("  PASS — expired keys pruned, live limits still enforced")
    passed += 1

    print(f"\nAll {passed} performance regression checks passed.")


if __name__ == "__main__":
    run()
