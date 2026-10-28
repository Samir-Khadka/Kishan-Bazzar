"""
Messaging API integration tests. Run: python3 tests/test_messaging_integration.py

Covers the wiring between Kishan-Bazzar's OTP flow and the separate
Messaging API service (github.com/Samir-Khadka/messaging-api):
  1. Disabled by default — request-code behaves exactly as before.
  2. Nepali phone normalization to E.164.
  3. send_otp success/failure through a mocked HTTP layer (no network).
  4. Route level: configured + gateway ok -> 200; configured + gateway
     down -> 502 (fail closed, never a fake "code sent"); unconfigured
     -> 200 via the mock path with no HTTP call at all.
"""

import json
import os
import sys
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import seed
import app as app_module
import auth
import messaging


class FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def read(self):
        return b'{"status": "sent"}'

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def clear_messaging_env():
    for key in ("KISAN_MESSAGING_API_URL", "KISAN_MESSAGING_API_KEY",
                "KISAN_MESSAGING_CHANNEL"):
        os.environ.pop(key, None)


def run():
    seed.seed()
    client = app_module.app.test_client()
    auth._attempts.clear()

    print("=== 1. Disabled by default ===")
    clear_messaging_env()
    assert messaging.messaging_enabled() is False
    r = client.post("/v1/auth/request-code", json={"phone": "9841000001"})
    assert r.status_code == 200, r.get_json()
    print("  PASS — unconfigured request-code still returns 200 (mock path)")

    print("\n=== 2. Phone normalization ===")
    assert messaging.normalize_phone_for_messaging("9841000001") == "+9779841000001"
    assert messaging.normalize_phone_for_messaging("+9779841000001") == "+9779841000001"
    assert messaging.normalize_phone_for_messaging("98 41-000001") == "+9779841000001"
    print("  PASS — bare Nepali mobile gains +977, E.164 passes through")

    print("\n=== 3. send_otp success posts the right payload ===")
    os.environ["KISAN_MESSAGING_API_URL"] = "http://messaging.test"
    os.environ["KISAN_MESSAGING_API_KEY"] = "test-key"
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["key"] = req.headers.get("X-api-key") or req.headers.get("X-Api-Key")
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse(200)

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        assert messaging.send_otp("9841000002", "123456") is True
    assert captured["url"] == "http://messaging.test/v1/messages"
    assert captured["body"]["to"] == "+9779841000002"
    assert captured["body"]["channel"] == "sms"
    assert "123456" in captured["body"]["body"]
    print("  PASS — E.164 number, sms channel, code in body")

    print("\n=== 4. send_otp failure never raises ===")
    def failing_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "boom", None, None)

    with mock.patch("urllib.request.urlopen", side_effect=failing_urlopen):
        assert messaging.send_otp("9841000003", "123456") is False

    def refused_urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    with mock.patch("urllib.request.urlopen", side_effect=refused_urlopen):
        assert messaging.send_otp("9841000003", "123456") is False
    print("  PASS — HTTP 500 and connection refused both report False")

    print("\n=== 5. Route: gateway ok -> 200, gateway down -> 502 ===")
    auth._attempts.clear()
    with mock.patch("urllib.request.urlopen",
                    side_effect=lambda req, timeout=None: FakeResponse(200)):
        r = client.post("/v1/auth/request-code", json={"phone": "9841000004"})
        assert r.status_code == 200, r.get_json()
    auth._attempts.clear()
    with mock.patch("urllib.request.urlopen", side_effect=failing_urlopen):
        r = client.post("/v1/auth/request-code", json={"phone": "9841000005"})
        assert r.status_code == 502, r.get_json()
        assert "could not send code" in r.get_json()["error"]
    print("  PASS — 200 on delivery, 502 fail-closed on gateway failure")

    print("\n=== 6. Unconfigured route makes no HTTP call ===")
    clear_messaging_env()
    auth._attempts.clear()
    with mock.patch("urllib.request.urlopen",
                    side_effect=AssertionError("must not be called")):
        r = client.post("/v1/auth/request-code", json={"phone": "9841000006"})
        assert r.status_code == 200, r.get_json()
    print("  PASS — mock path performs zero HTTP calls")

    clear_messaging_env()
    print("\nAll messaging integration checks passed.")


if __name__ == "__main__":
    run()
