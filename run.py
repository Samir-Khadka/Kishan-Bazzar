#!/usr/bin/env python3
"""
Kisan Bazar (किसान बजार) — Quickstart Runner
Starts the Flask API server and opens the web application in your browser.
"""
import os
import sys
import time
import webbrowser
import subprocess

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(PROJECT_ROOT, "kisan-bazar-api")

def main():
    print("=" * 60)
    print("      🌾 KISAN BAZAR (किसान बजार) — DIRECT MARKETPLACE 🌾")
    print("=" * 60)

    # 1. Ensure database is seeded
    db_path = os.path.join(API_DIR, "kisan_bazar.db")
    if not os.path.exists(db_path):
        print("\n[*] Initializing & seeding SQLite database (Kavre corridor)...")
        subprocess.run([sys.executable, "seed.py"], cwd=API_DIR, check=True)
    else:
        print("\n[*] Database ready at:", db_path)

    port = int(os.environ.get("PORT", 5050))
    url = f"http://localhost:{port}"

    print(f"\n[*] Starting Kisan Bazar API & Web App on {url}...")
    print(f"[*] Access the Web App in your browser: {url}")
    print("\n" + "-" * 60)
    print("DEMO ACCOUNTS FOR REAL BACKEND LOGIN:")
    print("  • Sarita Tamang (Farmer):    9841000001")
    print("  • Ram Bahadur K.C. (Farmer): 9841000002")
    print("  • Mina Shrestha (Farmer):    9841000003")
    print("  • Krishna Lama (Farmer):     9841000004")
    print("  • Kavre Co-op Desk (Staff):  9801100000")
    print("\nNOTE: When you request an SMS code in the app, the 6-digit")
    print("      OTP code will print directly in this console below.")
    print("-" * 60 + "\n")

    # Schedule browser open after 1 second
    def open_browser():
        time.sleep(1.2)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    import threading
    threading.Thread(target=open_browser, daemon=True).start()

    # Import and run Flask app
    sys.path.insert(0, API_DIR)
    import db
    db.init_db()
    import app as flask_app_module
    flask_app_module.app.run(host="127.0.0.1", port=port, debug=False)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[!] Kisan Bazar stopped. Goodbye!")
        sys.exit(0)
