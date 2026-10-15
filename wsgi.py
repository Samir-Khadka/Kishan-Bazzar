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
db.init_db()
conn = db.get_db()
coops = conn.execute("SELECT COUNT(*) AS n FROM cooperatives").fetchone()["n"]
conn.close()
if coops == 0:
    import seed
    seed.seed()

app = app_module.app
