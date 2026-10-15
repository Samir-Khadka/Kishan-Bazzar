"""Production entrypoint: gunicorn wsgi:app

Run locally with:  gunicorn wsgi:app --bind 0.0.0.0:5050
Render/Railway use: gunicorn wsgi:app --bind 0.0.0.0:$PORT
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "kisan-bazar-api"))

import db
import app as app_module

# Make sure the SQLite schema exists and demo data is seeded on first boot.
# On Render's ephemeral disk this re-seeds on every fresh deploy, which is
# exactly what we want for a public demo: every visitor gets a clean market.
#
# A file lock guards against two gunicorn workers racing here at startup:
# seed.seed() does init_db(fresh=True), which unlinks the DB file — without
# the lock, worker B can query a half-recreated database and 500 every
# request until the next deploy.
import fcntl

lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".seed.lock")
with open(lock_path, "w") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        db.init_db()
        conn = db.get_db()
        coops = conn.execute("SELECT COUNT(*) AS n FROM cooperatives").fetchone()["n"]
        conn.close()
        if coops == 0:
            import seed
            seed.seed()
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)

app = app_module.app
