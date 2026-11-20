"""Coverage regression tests for the Nov 20 2026 hardening pass.

The Python suite sat at 98% overall, but the remaining misses were
concentrated in the fault-handling paths of the mechanisms that must
never fail silently — the signing-secret resolution (auth.py), the
idempotency claim protocol (web.py), the lot-formation transaction
(aggregation.py), the WAL boot retry (db.py) — plus three honest
branches (decide-on-a-vanished-lot, the ZoneInfo fallback, the
serve_app fallback) and the agronomy name-match fallback in
assistant.py, which hid a real (latent) bug: with an unknown crop
code the fallback matched an _AGRO entry lacking "en" vacuously
("" == "") and answered with an empty crop name and empty advice
instead of the "which crop?" prompt. The guard is fixed in
assistant.py; the test below fails on the unfixed code.

Every branch here was probed live against a seeded server before
being pinned. Run: python3 tests/test_coverage_nov20.py
"""
import importlib
import os
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aggregation
import app as app_module
import assistant
import auth
import db
import seed
import web

FARMER = "9841000001"
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
    # ---------- 0. auth: signing-secret resolution on a fresh DB ----------
    # The suite never exercised the fresh-create path in-process: the
    # committed kisan_bazar.db already holds a signing_secret row, so
    # every in-process _secret_bytes() took the row-exists branch, and
    # the fresh-create path only ever ran inside the subprocesses of
    # test_session_persistence (unrecorded by coverage).
    print("\n=== 0. auth secret resolution (fresh temp DB, env unset) ===")
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    real_path = db.DB_PATH
    saved_secret = auth._SECRET_BYTES
    saved_env = os.environ.pop("KISAN_BAZAR_SECRET", None)
    try:
        db.DB_PATH = tmp.name
        db.init_db()
        auth._SECRET_BYTES = None
        s1 = auth._secret_bytes()
        row = db.get_db().execute(
            "SELECT value FROM app_settings WHERE key='signing_secret'"
        ).fetchone()
        check("fresh secret is created and persisted in app_settings",
              row is not None and row[0] == s1.decode())
        auth._SECRET_BYTES = None
        check("a second resolution reads the SAME persisted secret",
              auth._secret_bytes() == s1)

        # Lock contention on the first two attempts, then success:
        # the OperationalError retry must converge, not give up.
        orig_get_db = db.get_db
        calls = {"n": 0}

        def flaky_get_db():
            calls["n"] += 1
            if calls["n"] <= 2:
                raise sqlite3.OperationalError("database is locked")
            return orig_get_db()

        auth._SECRET_BYTES = None
        db.get_db = flaky_get_db
        try:
            check("two lock failures then success still resolves",
                  auth._secret_bytes() == s1 and calls["n"] == 3,
                  str(calls))
        finally:
            db.get_db = orig_get_db

        # A generic failure inside the create transaction (e.g. the
        # commit itself dies) must roll back and report "no secret",
        # not leak a half-written row or raise into the caller.
        real_conn = orig_get_db()

        class CommitFails:
            def __init__(self, real):
                self._real = real
                self._failed = False

            def execute(self, *a, **k):
                return self._real.execute(*a, **k)

            def commit(self):
                if not self._failed:
                    self._failed = True
                    raise RuntimeError("simulated commit failure")
                return self._real.commit()

            def rollback(self):
                return self._real.rollback()

            def close(self):
                return self._real.close()

        db.get_db = lambda: CommitFails(real_conn)
        try:
            check("commit failure inside secret creation returns None",
                  auth._load_or_create_db_secret() is None)
        finally:
            db.get_db = orig_get_db
            real_conn.close()

        # Total DB failure: three lock failures, then the ephemeral
        # fallback — the process still works, tokens just die with it.
        def dead_get_db():
            raise sqlite3.OperationalError("unable to open database file")

        auth._SECRET_BYTES = None
        db.get_db = dead_get_db
        try:
            s4 = auth._secret_bytes()
            check("unreachable DB falls back to an ephemeral secret",
                  isinstance(s4, bytes) and len(s4) == 64)
        finally:
            db.get_db = orig_get_db

        # The db module itself unimportable: also None, not a crash.
        sys.modules["db"] = None
        try:
            check("db import failure returns None",
                  auth._load_or_create_db_secret() is None)
        finally:
            sys.modules.pop("db", None)
    finally:
        db.DB_PATH = real_path
        auth._SECRET_BYTES = None
        if saved_env is not None:
            os.environ["KISAN_BAZAR_SECRET"] = saved_env
        else:
            os.environ.pop("KISAN_BAZAR_SECRET", None)
        os.unlink(tmp.name)

    seed.seed()
    client = app_module.app.test_client()
    auth._attempts.clear()

    # ---------- 1. aggregation: lot-code walk + formation rollback ----------
    print("\n=== 1. aggregation: _next_lot_code walk + rollback ===")
    conn = db.get_db()
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM lots WHERE coop_id='kavre-veg'"
    ).fetchone()["n"]
    # The insert below raises the count by one, so the code the walk
    # computes afterwards is 100 + (count + 1) + 1 — that is the one
    # to pre-occupy so the while loop actually walks.
    nxt = 100 + count + 2
    conn.execute(
        "INSERT INTO lots (id, coop_id, crop_code, grade, target_kg, "
        "pooled_kg, status) VALUES (?,?,?,?,?,0,'forming')",
        (f"KAV-{nxt}", "kavre-veg", "DHAN", "A", 130))
    conn.commit()
    check("collision walk skips the taken code",
          aggregation._next_lot_code(conn, "kavre-veg") == f"KAV-{nxt + 1}",
          f"count={count} nxt={nxt}")
    conn.execute("DELETE FROM lots WHERE id=?", (f"KAV-{nxt}",))
    conn.commit()
    conn.close()

    real_form = aggregation._form_lot_locked

    def boom(conn, *a):
        conn.execute(
            "INSERT INTO lots (id, coop_id, crop_code, grade, target_kg, "
            "pooled_kg, status) VALUES ('KAV-999','kavre-veg','DHAN','A',130,0,'forming')")
        raise RuntimeError("simulated crash mid-formation")

    aggregation._form_lot_locked = boom
    try:
        conn = db.get_db()
        try:
            aggregation.check_and_form_lot(conn, "kavre-veg", "DHAN", "A", 130)
            check("mid-formation crash propagates", False)
        except RuntimeError:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM lots WHERE id='KAV-999'"
            ).fetchone()["n"]
            check("mid-formation crash rolls back the partial lot",
                  n == 0 and not conn.in_transaction)
        finally:
            conn.close()
        # Caller-held transaction: the callee must NOT roll back the
        # caller's transaction — only its own started_here one.
        conn = db.get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                aggregation.check_and_form_lot(conn, "kavre-veg", "DHAN", "A", 130)
                check("caller-held crash propagates", False)
            except RuntimeError:
                check("caller-held transaction stays open on inner error",
                      conn.in_transaction)
            conn.rollback()
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM lots WHERE id='KAV-999'"
            ).fetchone()["n"]
            check("caller rollback undoes the partial lot", n == 0)
        finally:
            conn.close()
    finally:
        aggregation._form_lot_locked = real_form

    # ---------- 2. web: idempotency claim race branches ----------
    print("\n=== 2. web._idem_claim lost-race branches ===")
    conn = db.get_db()
    conn.execute("DELETE FROM idempotency_keys WHERE key LIKE 'NOV20-%'")
    conn.execute(
        "INSERT INTO idempotency_keys (key, endpoint, status_code, response_body) "
        "VALUES ('NOV20-P','/v1/orders',0,'')")
    conn.commit()
    conn.close()
    orig_lookup = web._idem_lookup
    with app_module.app.test_request_context("/v1/orders", method="POST"):
        state = {"n": 0}

        def miss_once(conn, key):
            state["n"] += 1
            if state["n"] == 1:
                return None  # simulate the lookup missing a concurrent insert
            return orig_lookup(conn, key)

        web._idem_lookup = miss_once
        try:
            check("INSERT conflict on a pending claim waits",
                  web._idem_claim("NOV20-P") == ("wait",))
        finally:
            web._idem_lookup = orig_lookup
        conn = db.get_db()
        conn.execute(
            "UPDATE idempotency_keys SET status_code=201, "
            "response_body='{\"ok\":1}' WHERE key='NOV20-P'")
        conn.commit()
        conn.close()
        state["n"] = 0
        web._idem_lookup = miss_once
        try:
            kind, row = web._idem_claim("NOV20-P")
            check("INSERT conflict on a completed claim replays",
                  kind == "replay" and row["status_code"] == 201)
        finally:
            web._idem_lookup = orig_lookup
        web._idem_lookup = lambda conn, key: None
        try:
            check("INSERT conflict with no row visible runs without storing",
                  web._idem_claim("NOV20-P") == ("run-nostore",))
        finally:
            web._idem_lookup = orig_lookup
        fake = mock.MagicMock()
        fake.execute.side_effect = RuntimeError("boom")
        with mock.patch.object(web.db, "get_db", return_value=fake):
            try:
                web._idem_claim("NOV20-X")
                check("claim failure propagates", False)
            except RuntimeError:
                check("claim failure rolls back, closes, and re-raises",
                      fake.rollback.called and fake.close.called)
    conn = db.get_db()
    conn.execute("DELETE FROM idempotency_keys WHERE key LIKE 'NOV20-%'")
    conn.commit()
    conn.close()

    # ---------- 3. decide on a vanished lot ----------
    print("\n=== 3. decide-accept when the lot no longer exists ===")
    conn = db.get_db()
    conn.execute(
        "INSERT INTO lots (id, coop_id, crop_code, grade, target_kg, "
        "pooled_kg, rate_per_kg, status) "
        "VALUES ('KAV-900','kavre-veg','DHAN','A',130,100,50,'ready')")
    conn.execute(
        "INSERT INTO offers (lot_id, buyer_phone, buyer_name, "
        "bid_rate_per_kg, quantity_kg, status) "
        "VALUES ('KAV-900',?,?,50,10,'pending')", (FARMER, "Test Buyer"))
    conn.commit()
    offer_id = conn.execute(
        "SELECT id FROM offers WHERE lot_id='KAV-900'").fetchone()["id"]
    conn.close()
    raw = sqlite3.connect(str(db.DB_PATH))  # FK enforcement off: orphan the offer
    raw.execute("DELETE FROM lots WHERE id='KAV-900'")
    raw.commit()
    raw.close()
    r = client.post(f"/v1/offers/{offer_id}/decide",
                    json={"decision": "accepted"}, headers=tok(STAFF))
    check("accept on a deleted lot is an honest 404",
          r.status_code == 404 and r.get_json().get("error") == "lot not found",
          f"{r.status_code} {r.get_json()}")
    conn = db.get_db()
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM orders WHERE lot_id='KAV-900'"
    ).fetchone()["n"]
    check("no order is created for the vanished lot", n == 0)
    conn.execute("DELETE FROM offers WHERE id=?", (offer_id,))
    conn.commit()
    conn.close()

    # ---------- 4. ZoneInfo fallback in book_rental ----------
    print("\n=== 4. book_rental falls back when ZoneInfo is unavailable ===")
    conn = db.get_db()
    rental_id = conn.execute(
        "SELECT id FROM rentals WHERE available=1 LIMIT 1").fetchone()["id"]
    conn.close()
    future = (date.today() + timedelta(days=5)).isoformat()
    with mock.patch("zoneinfo.ZoneInfo", side_effect=Exception("no tzdata")):
        r = client.post(f"/v1/rentals/{rental_id}/book",
                        json={"days": 2, "book_date": future},
                        headers=tok(FARMER))
    check("booking still succeeds on server-local date",
          r.status_code == 201, f"{r.status_code} {r.get_json()}")
    conn = db.get_db()
    conn.execute("DELETE FROM rental_bookings WHERE rental_id=?", (rental_id,))
    conn.commit()
    conn.close()

    # ---------- 5. assistant: extract_quantity + agronomy fallback ----------
    print("\n=== 5. assistant: extract_quantity + _agronomy fallback ===")
    check("extract_quantity with no number returns (None, None)",
          assistant.extract_quantity("hello") == (None, None))
    check("extract_quantity parses kg",
          assistant.extract_quantity("5 kg aalu") == (5.0, "kg"))
    check("extract_quantity converts quintal",
          assistant.extract_quantity("2 quintal dhaan") == (200.0, "quintal"))

    def msg(text, crops):
        return assistant.Msg(text=text, lang="en", crops=crops, ctx={},
                             db=None, farmer=None, conf=1.0)

    saved_agro = assistant._AGRO
    try:
        # ai_advisor keys are lowercase names; an entry keyed by a
        # different string with the same Nepali/English name must be
        # found through the name-match loop.
        assistant._AGRO = {"tamatar": {
            "ne": "गोलभेँडा", "en": "Tomato", "fertilizer": "FERT-X",
            "sow": "SOW-X", "pests": {"blight": "BLIGHT-X"}}}
        r = assistant._agronomy(msg("golbheda ma blight", ["tomato"]))
        check("name-match fallback serves pest advice",
              "BLIGHT-X" in r["reply"], r["reply"][:80])
        r = assistant._agronomy(msg("golbheda", ["tomato"]))
        check("name-match fallback serves fertilizer advice",
              "FERT-X" in r["reply"] and "SOW-X" in r["reply"])
        # An _AGRO entry with no "en"/"ne" must NOT match an unknown
        # crop code on empty strings — that used to answer with an
        # empty crop name and empty advice instead of "which crop?".
        assistant._AGRO = {"weird": {"pests": {"x": "y"}}}
        r = assistant._agronomy(msg("???", ["bogus-code"]))
        check("unknown crop + nameless entry asks which crop",
              "Which crop" in r["reply"], r["reply"][:80])
    finally:
        assistant._AGRO = saved_agro

    # ---------- 6. assistant: ai_advisor import fallback ----------
    print("\n=== 6. assistant import fallback when ai_advisor is missing ===")
    sys.modules["ai_advisor"] = None
    try:
        importlib.reload(assistant)
        check("blocked ai_advisor import degrades to empty knowledge",
              assistant._AGRO == {})
    finally:
        sys.modules.pop("ai_advisor", None)
        importlib.reload(assistant)
    check("ai_advisor import restores the knowledge base",
          len(assistant._AGRO) == 6, str(sorted(assistant._AGRO)))

    # ---------- 7. serve_app fallback when the html file is gone ----------
    print("\n=== 7. /app answers JSON when kisan-bazar.html is missing ===")
    real_exists = os.path.exists

    def exists_side(p):
        if p.endswith("kisan-bazar.html"):
            return False
        return real_exists(p)

    with mock.patch("os.path.exists", side_effect=exists_side):
        r = client.get("/app")
    check("/app without the html file returns the running JSON",
          r.status_code == 200
          and r.get_json() == {"service": "kisan-bazar-api", "status": "running"},
          f"{r.status_code} {r.get_json()}")

    # ---------- 8. db.get_db WAL retry ----------
    print("\n=== 8. db.get_db retries a locked WAL transition ===")
    real_connect = sqlite3.connect
    wal_calls = {"n": 0}
    opened = []

    class FakeConn:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *a, **k):
            if "journal_mode" in sql:
                wal_calls["n"] += 1
                if wal_calls["n"] <= 2:
                    raise sqlite3.OperationalError("database is locked")
            return self._real.execute(sql, *a, **k)

        def __getattr__(self, name):
            return getattr(self._real, name)

    def fake_connect(*a, **k):
        c = FakeConn(real_connect(*a, **k))
        opened.append(c)
        return c

    with mock.patch.object(db.sqlite3, "connect", fake_connect):
        with mock.patch.object(db.time, "sleep", lambda s: None):
            c = db.get_db()
            check("two locked WAL attempts then success",
                  wal_calls["n"] == 3)
            c.close()

    def always_locked(*a, **k):
        class LockedConn(FakeConn):
            def execute(self, sql, *a2, **k2):
                if "journal_mode" in sql:
                    wal_calls["n"] += 1
                    raise sqlite3.OperationalError("database is locked")
                return self._real.execute(sql, *a2, **k2)

        c = LockedConn(real_connect(*a, **k))
        opened.append(c)
        return c

    wal_calls["n"] = 0
    with mock.patch.object(db.sqlite3, "connect", always_locked):
        with mock.patch.object(db.time, "sleep", lambda s: None):
            try:
                db.get_db()
                check("persistent WAL lock raises", False)
            except sqlite3.OperationalError:
                check("persistent WAL lock raises after 20 attempts",
                      wal_calls["n"] == 20, str(wal_calls))
    for c in opened:
        try:
            c._real.close()
        except Exception:
            pass

    print(f"\n{passed} checks passed.")


if __name__ == "__main__":
    run()
