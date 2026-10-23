"""
Runnable demo of the USSD and SMS handlers, played as transcripts.
Run: python3 tests/test_flows.py
"""
import sys, os
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ussd import Session, handle_ussd
from sms import handle_sms


def rule():
    print("-" * 52)


def ussd_conversation(phone, steps, title):
    print(f"\n=== USSD: {title} (phone {phone}) ===")
    session = Session(phone)
    text = ""
    for step in steps:
        text = step if text == "" else text + "*" + step
        msg, final = handle_ussd(phone, text, session)
        tag = "END" if final else "CON"
        print(f"[dial *1234# then presses '{text}']")
        print(f"  {tag} > " + msg.replace("\n", "\n        "))
        if final:
            break
    rule()


def sms_conversation(phone, messages, title):
    print(f"\n=== SMS: {title} (phone {phone}) ===")
    for m in messages:
        reply = handle_sms(phone, m)
        print(f"  farmer sends: {m}")
        print("  reply:        " + reply.replace("\n", "\n                "))
    rule()


if __name__ == "__main__":
    print("KISAN BAZAR — SMS/USSD reference implementation, live run\n")

    # 1. USSD: check today's cauliflower rate (Nepali, default language)
    ussd_conversation("9841000001", ["1", "1"], "check rate")

    # 2. USSD: check weather — this farmer's ward has an active warning
    ussd_conversation("9841000001", ["2"], "check weather")

    # 3. USSD: full sell flow — cauliflower, 44kg, grade A
    ussd_conversation("9841000002", ["3", "1", "44", "1"], "list produce for sale")

    # 4. USSD: switch language mid-session, then check ledger
    ussd_conversation("9841000001", ["0", "4"], "switch to English, check ledger")

    # 5. SMS: rate check, all crops
    sms_conversation("9841000002", ["RATE"], "check all rates")

    # 6. SMS: sell command, same as USSD flow #3 but one message
    sms_conversation("9841000002", ["BECH KAULI 44 A"], "list produce via one SMS")

    # 7. SMS: malformed command — should fail helpfully, not silently
    sms_conversation("9841000001", ["BECH KAULI abc A"], "malformed weight")

    # 8. SMS: unknown crop code
    sms_conversation("9841000001", ["RATE SEB"], "unrecognized crop code")

    # 9. SMS: help
    sms_conversation("9841000002", ["HELP"], "help text")

    # 10. regression check: toggle language mid-session must not swallow the
    #     next real menu choice (this broke once during development)
    print("\n=== Regression: language toggle must not eat the next keypress ===")
    session = Session("9841000001")
    msg1, final1 = handle_ussd("9841000001", "0", session)
    assert not final1, "language toggle should keep the session open (CON)"
    assert session.lang == "en", "first toggle should switch ne -> en"
    msg2, final2 = handle_ussd("9841000001", "0*4", session)
    assert final2, "ledger choice should end the session (END)"
    assert "ledger" in msg2.lower() or "काउली" in msg2, \
        f"expected ledger content after toggle+choice, got: {msg2!r}"
    print("  PASS — toggle consumed once, '4' correctly reached the ledger branch")

    # 11. invalid mid-flow input shouldn't crash, just end with a clear message
    print("\n=== Edge case: non-numeric input during 'sell' kg step ===")
    session2 = Session("9841000002")
    handle_ussd("9841000002", "3", session2)
    handle_ussd("9841000002", "3*1", session2)
    msg3, final3 = handle_ussd("9841000002", "3*1*abc", session2)
    assert final3, "bad input should end the session cleanly, not hang"
    print(f"  PASS — got a clean error end: {msg3!r}")

    print("\nAll flows and regression checks passed.")
