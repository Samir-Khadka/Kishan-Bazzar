"""
Session persistence tests (2026-10-23 hotfix).
Run: python3 tests/test_session_persistence.py

The bug: with KISAN_BAZAR_SECRET unset, auth.py generated a RANDOM
signing secret per process. Every restart/redeploy (and every extra
gunicorn worker) invalidated all sessions — the live admin panel
showed a dead-end "server 401" — and OTP code hashes (keyed with the
same secret) broke across restarts too.

The fix: the secret is resolved lazily — env var first, else read-or-
created ATOMICALLY in the app_settings table of the SQLite DB, so all
processes sharing the database converge on one value and sessions
survive restarts. These tests prove it with REAL subprocess restarts
(a fresh Python process is the only honest simulation of a restart —
an in-process reload alone wouldn't catch import-time state), plus a
concurrent-boot convergence race.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PHONE = "9841000001"

# Driver executed in a CHILD process against a caller-chosen DB file.
# It patches db.DB_PATH before importing auth, exactly like a server
# process booting against its configured database, then performs one
# operation and prints a JSON result line.
DRIVER = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])            # api dir
os.environ.pop("KISAN_BAZAR_SECRET", None)  # prove the DB path, not env
from pathlib import Path
import db
db.DB_PATH = Path(sys.argv[2])             # the shared database file
import auth

mode = sys.argv[3]
if mode == "init-and-issue":
    db.init_db()
    conn = db.get_db()
    code = auth.create_auth_code(conn, "%(phone)s")
    conn.close()
    out = {
        "token": auth.issue_token("%(phone)s"),
        "code": code,
        "secret_sha": __import__("hashlib").sha256(auth._secret_bytes()).hexdigest(),
    }
elif mode == "verify":
    token, code = sys.argv[4], sys.argv[5]
    conn = db.get_db()
    out = {
        "token_phone": auth.verify_token(token),
        "code_ok": auth.verify_code(conn, "%(phone)s", code),
        "secret_sha": __import__("hashlib").sha256(auth._secret_bytes()).hexdigest(),
    }
    conn.close()
elif mode == "resolve":
    db.init_db()
    out = {"secret_sha": __import__("hashlib").sha256(auth._secret_bytes()).hexdigest()}
print("RESULT " + json.dumps(out))
""" % {"phone": PHONE}


def run_driver(db_path, *args):
    proc = subprocess.run(
        [sys.executable, "-c", DRIVER, API_DIR, str(db_path), *args],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"driver failed: {proc.stderr[-600:]}"
    line = [l for l in proc.stdout.splitlines() if l.startswith("RESULT ")]
    assert line, f"driver printed no result: {proc.stdout[-300:]} {proc.stderr[-300:]}"
    return json.loads(line[0][len("RESULT "):])


def run():
    tmp = Path(tempfile.mkdtemp(prefix="kb-session-"))
    db_file = tmp / "session.db"

    print("=== 1. Token + OTP issued in process A still verify in process B (restart) ===")
    a = run_driver(db_file, "init-and-issue")
    assert a["token"] and a["code"], a
    b = run_driver(db_file, "verify", a["token"], a["code"])
    assert b["token_phone"] == PHONE, b
    print("  PASS — bearer token survives a full process restart")
    assert b["code_ok"] is True, b
    print("  PASS — OTP code requested before the restart verifies after it")
    assert a["secret_sha"] == b["secret_sha"], (a, b)
    print("  PASS — both processes resolved the SAME signing secret from the DB")

    print("\n=== 2. Concurrent first boot: two processes converge on ONE secret ===")
    race_db = tmp / "race.db"
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", DRIVER, API_DIR, str(race_db), "resolve"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(2)
    ]
    shas = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, f"racing driver failed: {err[-600:]}"
        line = [l for l in out.splitlines() if l.startswith("RESULT ")]
        shas.append(json.loads(line[0][len("RESULT "):])["secret_sha"])
    assert shas[0] == shas[1], shas
    print("  PASS — simultaneous read-or-create produced one shared secret")

    print("\n=== 3. Env var still wins over the persisted secret ===")
    import auth
    saved = auth._SECRET_BYTES
    try:
        auth._SECRET_BYTES = None
        os.environ["KISAN_BAZAR_SECRET"] = "test-env-secret"
        assert auth._secret_bytes() == b"test-env-secret"
    finally:
        auth._SECRET_BYTES = saved
        os.environ.pop("KISAN_BAZAR_SECRET", None)
    print("  PASS — KISAN_BAZAR_SECRET takes priority when set")

    print("\nAll session persistence checks passed.")


if __name__ == "__main__":
    run()
