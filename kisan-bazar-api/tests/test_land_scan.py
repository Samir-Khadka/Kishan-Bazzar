"""
End-to-end test of land-scan persistence. Run: python3 tests/test_land_scan.py

Covers: saving a scan, reading it back as "latest", a second scan
overwriting the farmer's current land_type while both scans remain in
history, validation of bad input, the 404 paths, and — since the
security hardening pass — that a farmer cannot save or read a scan under
another farmer's phone number even with a perfectly valid token of their
own.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import seed
import app as app_module
import auth


def token_header(phone):
    return {"Authorization": f"Bearer {auth.issue_token(phone)}"}


def run():
    seed.seed()
    client = app_module.app.test_client()
    phone = "9841000001"
    other_phone = "9841000002"

    print("=== 1. No scan on file yet -> 404 ===")
    r = client.get(f"/v1/farmers/{phone}/land-scan", headers=token_header(phone))
    assert r.status_code == 404, r.get_json()
    print(f"  PASS — {r.status_code}: {r.get_json()['error']}")

    print("\n=== 2. Save a first scan (photo + both quiz answers, dark+clay+khet) ===")
    r = client.post(f"/v1/farmers/{phone}/land-scan", json={
        "land_type": "khet", "confidence": "good",
        "color_key": "dark", "texture": "clay", "drainage": "khet", "had_photo": True,
    }, headers=token_header(phone))
    assert r.status_code == 201, r.get_json()
    scan1 = r.get_json()
    print(f"  saved scan id={scan1['id']}, land_type={scan1['land_type']}, confidence={scan1['confidence']}")

    print("\n=== 3. Farmer's record should now show land_type='khet' ===")
    r = client.get(f"/v1/farmers/{phone}/land-scan", headers=token_header(phone))
    assert r.status_code == 200
    latest = r.get_json()
    assert latest["land_type"] == "khet"
    print(f"  PASS — latest scan shows land_type=khet")

    print("\n=== 4. Rescan without a photo (skip-photo path) -> balaute, lower confidence ===")
    r = client.post(f"/v1/farmers/{phone}/land-scan", json={
        "land_type": "balaute", "confidence": "ok",
        "color_key": None, "texture": "sandy", "drainage": "fast", "had_photo": False,
    }, headers=token_header(phone))
    assert r.status_code == 201, r.get_json()
    scan2 = r.get_json()
    assert scan2["color_key"] is None and scan2["had_photo"] == 0
    print(f"  PASS — saved without a photo, had_photo={scan2['had_photo']}, color_key={scan2['color_key']}")

    print("\n=== 5. 'Latest' should now be the second scan, not the first ===")
    r = client.get(f"/v1/farmers/{phone}/land-scan", headers=token_header(phone))
    latest = r.get_json()
    assert latest["id"] == scan2["id"], f"expected latest to be scan {scan2['id']}, got {latest['id']}"
    assert latest["land_type"] == "balaute"
    print(f"  PASS — latest is scan {latest['id']} (balaute), as expected")

    print("\n=== 6. History should contain BOTH scans, most recent first ===")
    r = client.get(f"/v1/farmers/{phone}/land-scan/history", headers=token_header(phone))
    history = r.get_json()
    assert len(history) == 2, f"expected 2 scans in history, got {len(history)}"
    assert history[0]["id"] == scan2["id"] and history[1]["id"] == scan1["id"]
    print(f"  PASS — {len(history)} scans in history, newest first (khet scan not lost)")

    print("\n=== 7. Invalid land_type is rejected ===")
    r = client.post(f"/v1/farmers/{phone}/land-scan", json={
        "land_type": "moon_dust", "confidence": "good",
    }, headers=token_header(phone))
    assert r.status_code == 400, r.get_json()
    print(f"  PASS — rejected with 400: {r.get_json()['error']}")

    print("\n=== 8. Unknown farmer phone -> 404 ===")
    unknown = "0000000000"
    r = client.post(f"/v1/farmers/{unknown}/land-scan", json={
        "land_type": "bari", "confidence": "ok",
    }, headers=token_header(unknown))  # a token CAN be minted for any phone string;
    assert r.status_code == 404, r.get_json()      # the farmer-existence check is separate from auth
    print(f"  PASS — rejected with 404: {r.get_json()['error']}")

    print("\n=== 9. Missing required fields is rejected, not a 500 ===")
    r = client.post(f"/v1/farmers/{phone}/land-scan", json={"confidence": "good"}, headers=token_header(phone))
    assert r.status_code == 400, r.get_json()
    print(f"  PASS — rejected with 400: {r.get_json()['error']}")

    print("\n=== 10. SECURITY: no token at all -> 401, not a silent pass-through ===")
    r = client.get(f"/v1/farmers/{phone}/land-scan")
    assert r.status_code == 401, r.get_json()
    print(f"  PASS — rejected with 401: {r.get_json()['error']}")

    print("\n=== 11. SECURITY: farmer A's valid token cannot read farmer B's scan ===")
    r = client.get(f"/v1/farmers/{other_phone}/land-scan", headers=token_header(phone))
    assert r.status_code == 403, r.get_json()
    print(f"  PASS — rejected with 403: {r.get_json()['error']}")

    print("\n=== 12. SECURITY: farmer A's valid token cannot SAVE a scan as farmer B ===")
    r = client.post(f"/v1/farmers/{other_phone}/land-scan", json={
        "land_type": "khet", "confidence": "good",
    }, headers=token_header(phone))
    assert r.status_code == 403, r.get_json()
    print(f"  PASS — rejected with 403: {r.get_json()['error']}")

    print("\n=== 13. SECURITY: a garbage/tampered token is rejected, not silently accepted ===")
    r = client.get(f"/v1/farmers/{phone}/land-scan", headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401, r.get_json()
    print(f"  PASS — rejected with 401: {r.get_json()['error']}")

    print("\nAll land-scan and security checks passed.")


if __name__ == "__main__":
    run()
