"""
Root routing tests (2026-10-23, website/app door separation): the main
address belongs to the WEBSITE, the app lives at its own address.

Samir's requirement, stated three times: website login and app login
are separate doors. Serving the app AT "/" defeated that no matter how
the logins were wired — anyone opening the bare domain landed in the
app. So:

  GET /        -> 302 to /site/   (the website is the front door)
  GET /app     -> 200, the app    (kisan-bazar.html, same send_file
  GET /app/    -> 200, the app     path and headers as "/" had)
  GET /site/*  -> unchanged
  GET /v1/*    -> unchanged

Run: python3 tests/test_root_routing.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module

CHECKS = 0


def check(name, cond, detail=""):
    global CHECKS
    CHECKS += 1
    assert cond, f"{name} :: {detail}"
    print(f"  ok {CHECKS}: {name}")


def run():
    client = app_module.app.test_client()

    print("=== / redirects to the website ===")
    r = client.get("/")
    check("GET / is a 302", r.status_code == 302, r.status_code)
    check("GET / Location is /site/", r.headers.get("Location", "").endswith("/site/"),
          r.headers.get("Location"))

    print("=== the app lives at /app ===")
    for path in ("/app", "/app/"):
        r = client.get(path)
        check(f"GET {path} is 200", r.status_code == 200, r.status_code)
        body = r.get_data(as_text=True)
        check(f"GET {path} serves the app shell",
              "Kisan Bazar" in body and "headerLogoutBtn" in body,
              body[:120])

    print("=== website + API unchanged ===")
    r = client.get("/site/")
    check("GET /site/ is 200", r.status_code == 200, r.status_code)
    r = client.get("/site/login.html")
    check("GET /site/login.html is 200", r.status_code == 200, r.status_code)
    check("website login page has no website->app root link left",
          'href="/"' not in r.get_data(as_text=True))
    r = client.get("/v1/health")
    check("GET /v1/health is 200", r.status_code == 200, r.status_code)

    print(f"\nALL {CHECKS} ROOT-ROUTING CHECKS PASSED")


if __name__ == "__main__":
    run()
