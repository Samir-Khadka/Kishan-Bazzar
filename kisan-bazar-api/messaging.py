"""Optional real SMS delivery via the Messaging API (a separate service:
https://github.com/Samir-Khadka/messaging-api).

When KISAN_MESSAGING_API_URL and KISAN_MESSAGING_API_KEY are both set,
OTP codes requested through /v1/auth/request-code are ALSO sent as a
real SMS through that service (channel from KISAN_MESSAGING_CHANNEL,
default "sms"). When either is unset — the default — nothing changes:
codes go to the in-memory dev outbox exactly as before, and the public
demo keeps working via /v1/dev/latest-code.

Design notes, all deliberate:
- The Messaging API is transport only. The code is still minted,
  hashed, and verified locally (auth.py); this module never makes a
  verification decision and never stores a code.
- Phones in this app are bare 10-digit Nepali mobiles (e.g.
  98XXXXXXXX). The Messaging API requires E.164, so a bare 10-digit
  number starting with 9 gets +977. Numbers already in E.164 pass
  through untouched. Anything else is left for the Messaging API to
  reject (its 4xx becomes a send failure here).
- Failures fail CLOSED: if the Messaging API is configured but the
  send fails, request-code answers 502 instead of claiming a code was
  sent. A code the user never received is worse than an honest error.
- Stdlib only (urllib), matching auth.py's no-new-dependencies rule.
  Env vars are read at call time (not import time) so tests can toggle
  the integration without reimporting.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

_TIMEOUT_SECONDS = 10


def messaging_enabled() -> bool:
    return bool(os.environ.get("KISAN_MESSAGING_API_URL", "").strip()
                and os.environ.get("KISAN_MESSAGING_API_KEY", "").strip())


def normalize_phone_for_messaging(phone: str) -> str:
    """Bare Nepali mobile -> E.164. E.164 input passes through."""
    digits = re.sub(r"[\s\-()]", "", phone.strip())
    if digits.startswith("+"):
        return digits
    if len(digits) == 10 and digits.startswith("9"):
        return "+977" + digits
    return digits


def send_otp(phone: str, code: str) -> bool:
    """Send one OTP code as a real message. Returns True on success,
    False on ANY failure (network, 4xx, 5xx, bad config). Never raises:
    the route decides what a failure means (502), this module just
    reports it. The code itself is never logged."""
    url = os.environ.get("KISAN_MESSAGING_API_URL", "").strip().rstrip("/")
    key = os.environ.get("KISAN_MESSAGING_API_KEY", "").strip()
    channel = os.environ.get("KISAN_MESSAGING_CHANNEL", "sms").strip() or "sms"
    if not url or not key:
        return False
    payload = json.dumps({
        "to": normalize_phone_for_messaging(phone),
        "channel": channel,
        "body": f"Your Kisan Bazar verification code is {code}. "
                f"It expires in 5 minutes. Do not share it.",
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/v1/messages", data=payload, method="POST",
        headers={"X-API-Key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            resp.read()
            return 200 <= resp.status < 300
    except Exception as exc:  # network error, HTTPError, timeout, ...
        masked = phone[:4] + "•••" + phone[-2:] if len(phone) >= 6 else "•••"
        sys.stderr.write(
            f"\n⚠️ [SMS GATEWAY] send failed for {masked}: "
            f"{type(exc).__name__}\n")
        sys.stderr.flush()
        return False
