"""
Regression tests for the 2026-10-24 idempotency rewrite (web.py).

The old decorator did SELECT-then-run: two requests racing with the
same Idempotency-Key could BOTH run the handler (duplicate orders /
listings) — the INSERT-conflict replay only fixed the response, not
the double side effect. The new decorator claims the key atomically
under BEGIN IMMEDIATE, so exactly one request runs the handler and
the loser waits for the winner's stored response.

Also guards the require_staff cleanup: the staff invariant now lives
only in repositories.farmer_is_staff — no second inline SQL copy in
web.py to drift.

Run: python3 tests/test_idempotent_race.py
"""
import sys
import os
import inspect
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


def ctx(path="/v1/orders"):
    return app_module.app.test_request_context(path, method="POST")


def release(key, path="/v1/orders"):
    with ctx(path):
        web._idem_release_claim(key)


def run():
    seed.seed()
    client = app_module.app.test_client()
    auth._attempts.clear()

    # ---------- 1. the claim is atomic: one winner, rest wait ----------
    print("\n=== 1. Racing claims: exactly one winner ===")
    key = "race-atomic-1"
    results = []

    def claimer():
        with ctx():
            results.append(web._idem_claim(key)[0])

    threads = [threading.Thread(target=claimer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("exactly one 'run', the rest 'wait'",
          results.count("run") == 1
          and all(r == "wait" for r in results if r != "run"),
          str(results))
    release(key)

    # ---------- 2. loser waits, winner stores, loser replays ----------
    print("\n=== 2. Loser waits for the winner's stored response ===")
    key = "race-wait-1"
    with ctx():
        check("winner claims first", web._idem_claim(key) == ("run",))
    outcome = {}

    def loser():
        with ctx():
            action = web._idem_claim(key)
            outcome["action"] = action[0]
            if action[0] == "wait":
                row = web._idem_await_completion(key)
                outcome["row"] = dict(row) if row else None

    t = threading.Thread(target=loser)
    t.start()
    time.sleep(0.6)  # let the loser reach the wait loop
    with ctx():
        resp = app_module.app.response_class(
            response='{"order_id": 42}', status=201,
            mimetype="application/json")
        web._idem_store(key, (resp, 201))
    t.join(timeout=20)
    check("loser waited (did not run the handler)",
          outcome.get("action") == "wait", str(outcome))
    check("loser got the winner's stored response",
          outcome.get("row")
          and outcome["row"]["status_code"] == 201
          and outcome["row"]["response_body"] == '{"order_id": 42}',
          str(outcome.get("row")))

    # ---------- 3. a completed key replays without running ----------
    print("\n=== 3. Completed key replays ===")
    with ctx():
        action = web._idem_claim(key)
        check("completed key -> replay", action[0] == "replay",
              str(action[0]))
        replayed = web._idem_replay(action[1])
        check("replayed body/status match",
              replayed.status_code == 201
              and replayed.get_data(as_text=True) == '{"order_id": 42}')

    # ---------- 4. handler exception releases the claim ----------
    print("\n=== 4. Exception releases the claim for a fresh retry ===")
    key = "race-exc-1"
    with ctx():
        check("first claim -> run", web._idem_claim(key) == ("run",))
        web._idem_release_claim(key)
        check("retry after failure claims again",
              web._idem_claim(key) == ("run",))
        web._idem_release_claim(key)

    # ---------- 5. a stale (dead worker) claim is reclaimed ----------
    print("\n=== 5. Stale pending claim is reclaimed, not waited on ===")
    key = "race-stale-1"
    conn = db.get_db()
    conn.execute(
        "INSERT INTO idempotency_keys (key, endpoint, status_code, "
        "response_body, created_at) "
        "VALUES (?, '/v1/orders', 0, '', datetime('now', '-10 minutes'))",
        (key,))
    conn.commit()
    conn.close()
    with ctx():
        check("stale claim -> run (no 10s wait)",
              web._idem_claim(key) == ("run",))
    release(key)

    # ---------- 6. HTTP-level race: one order, every reply identical --
    print("\n=== 6. Concurrent POST /v1/orders, same key: one order ===")
    conn = db.get_db()
    conn.execute(
        "INSERT INTO lots (id, coop_id, crop_code, grade, target_kg, "
        "pooled_kg, rate_per_kg, status) "
        "VALUES ('RACE-1', 'kavre-veg', 'KAULI', 'A', 130, 130, 45, 'ready')")
    conn.commit()
    conn.close()
    key = "race-http-orders-1"
    headers = {**tok(SARITA), "Idempotency-Key": key}
    body = {"buyer_name": "Race Buyer", "buyer_phone": SARITA,
            "lot_id": "RACE-1", "quantity_kg": 5}
    responses = []

    def poster():
        c = app_module.app.test_client()
        responses.append(c.post("/v1/orders", json=body, headers=headers))

    threads = [threading.Thread(target=poster) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    check("all 6 threads got a response",
          len(responses) == 6, f"{len(responses)}")
    statuses = {r.status_code for r in responses}
    check("no 409 'still processing' and no 500s", statuses == {201},
          str(statuses))
    order_ids = {r.get_json().get("order_id") for r in responses}
    check("all responses carry the SAME order_id", len(order_ids) == 1,
          str(order_ids))
    conn = db.get_db()
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM orders WHERE lot_id='RACE-1' "
        "AND buyer_name='Race Buyer'").fetchone()["n"]
    conn.close()
    check("exactly ONE order row in the DB", n == 1, f"rows={n}")

    # ---------- 7. sequential replay still stores once, replays ------
    print("\n=== 7. Sequential duplicate: replay, no new row ===")
    key = "race-seq-listing-1"
    headers = {**tok(SARITA), "Idempotency-Key": key}
    lbody = {"farmer_phone": SARITA, "coop_id": "kavre-veg",
             "crop_code": "GOLB", "grade": "A", "quantity_kg": 10}
    r1 = client.post("/v1/listings", json=lbody, headers=headers)
    r2 = client.post("/v1/listings", json=lbody, headers=headers)
    check("both 201", r1.status_code == 201 and r2.status_code == 201,
          f"{r1.status_code}/{r2.status_code}")
    check("same listing_id replayed",
          r2.get_json().get("listing_id") == r1.get_json().get("listing_id")
          and r1.get_json().get("listing_id") is not None,
          f"{r1.get_json()} vs {r2.get_json()}")

    # ---------- 8. require_staff has no inline SQL copy ---------------
    print("\n=== 8. require_staff delegates to repositories ===")
    src = inspect.getsource(web.require_staff)
    check("no SELECT in require_staff",
          "SELECT" not in src, src.strip().splitlines()[0])
    check("delegates to repositories.farmer_is_staff",
          "repositories.farmer_is_staff" in src)
    # Behavioural: the staff gate still rejects non-staff before any
    # handler logic (require_auth -> require_staff -> handler).
    r = client.post("/v1/offers/99999/decide",
                    json={"decision": "rejected"}, headers=tok(SARITA))
    check("non-staff decide -> 403", r.status_code == 403, r.get_json())
    r = client.post("/v1/offers/99999/decide",
                    json={"decision": "rejected"}, headers=tok(STAFF))
    check("staff decide on missing offer -> 404 (gate passed)",
          r.status_code == 404, r.get_json())

    print(f"\nAll {passed} idempotency/staff checks passed.")


if __name__ == "__main__":
    run()
