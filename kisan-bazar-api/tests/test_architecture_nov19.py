"""
Architecture regression: assistant intent handlers take ONE Msg argument.

Run: python3 tests/test_architecture_nov19.py

Pins the 2026-11-19 refactor that replaced the 7-positional-argument
handler signature (text, lang, crops, ctx, db, farmer, conf) — of which
most handlers used one or two and ignored the rest — with a single
Msg dataclass carrying the whole request by name. A caller could
previously pass db and farmer in the wrong order and no handler would
notice until runtime; the old call shape must now fail loudly, and
every intent must still dispatch to the same reply through chat().
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import inspect
import assistant
import db
import seed

passed = failed = 0
def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL: {name}")

if __name__ == "__main__":
    # --- the contract: one argument, named msg ---
    handlers = dict(assistant._HANDLERS)
    handlers["fallback"] = assistant._fallback
    check("12 intents + fallback registered", len(assistant._HANDLERS) == 12)
    for name, fn in sorted(handlers.items()):
        params = list(inspect.signature(fn).parameters.values())
        check(f"{name} takes exactly one positional arg",
              len(params) == 1 and params[0].kind == inspect.Parameter.POSITIONAL_OR_KEYWORD)
        check(f"{name} arg is named msg", params[0].name == "msg")

    # --- Msg carries the whole request by name ---
    m = assistant.Msg(text="namaste", lang="ne", crops=[], ctx={},
                      db=None, farmer=None, conf=0.9)
    check("Msg fields readable by name",
          m.text == "namaste" and m.lang == "ne" and m.crops == []
          and m.ctx == {} and m.db is None and m.farmer is None
          and m.conf == 0.9)
    check("Msg is a dataclass", hasattr(assistant.Msg, "__dataclass_fields__"))
    check("Msg has the 7 expected fields",
          set(assistant.Msg.__dataclass_fields__) ==
          {"text", "lang", "crops", "ctx", "db", "farmer", "conf"})

    # --- the old 7-arg call shape fails loudly, not silently ---
    for name, fn in sorted(handlers.items()):
        try:
            fn("namaste", "ne", [], {}, None, None, 0.9)
            check(f"old 7-arg call to {name} raises TypeError", False)
        except TypeError:
            check(f"old 7-arg call to {name} raises TypeError", True)

    # --- every intent still dispatches through chat() ---
    cases = [
        ("namaste", "greeting"), ("aaluko bhau kati?", "price"),
        ("aaja mausam kasto cha?", "weather"), ("kasari bechne?", "sell"),
        ("ke lagaune ahile?", "plant"), ("mero hisab herna", "ledger"),
        ("mero order ke cha?", "order"), ("माटो जाँच", "scan"),
        ("dhanyabad!", "thanks"), ("bye", "bye"),
        ("what can you do?", "help"),
        ("gahunma kira lagyo", "agronomy"),
        ("xyzzy nonsense words here", None),  # -> fallback
    ]
    for i, (text, want) in enumerate(cases):
        r = assistant.chat(f"nov19-{i}", text)
        check(f"chat {want or 'fallback'}: intent + reply",
              r["intent"] == want and bool(r["reply"]) and r["lang"] in ("ne", "ro", "en"))

    # --- handlers that need the DB still get it via Msg ---
    db.init_db(fresh=True)
    seed.seed()
    conn = db.get_db()
    conn.execute(
        "INSERT INTO lots (id, coop_id, crop_code, grade, target_kg, pooled_kg, rate_per_kg, status) "
        "VALUES ('LOT-NOV19','kavre-veg','DHAN','A',1000,500,32,'confirmed')")
    conn.execute(
        "INSERT INTO lots (id, coop_id, crop_code, grade, target_kg, pooled_kg, rate_per_kg, status) "
        "VALUES ('LOT-NOV19B','kavre-veg','ALU','A',1000,500,45,'confirmed')")
    conn.commit()
    r = assistant.chat("nov19-price", "धानको भाउ?", db_conn=conn)
    check("price handler reads live rate via Msg.db",
          r["intent"] == "price" and "32" in r["reply"])
    r = assistant.chat("nov19-price2", "aalu ko bhau?", db_conn=conn)
    check("price handler second crop", r["intent"] == "price" and "45" in r["reply"])
    r = assistant.chat("nov19-ledger", "mero hisab",
                       db_conn=conn, farmer={"phone": "9841000001", "name": "Sarita Tamang"})
    check("ledger handler reads farmer via Msg.farmer",
          r["intent"] == "ledger" and ("बाँकी छैन" in r["reply"] or "baaki chhaina" in r["reply"]))
    r = assistant.chat("nov19-greet", "namaste",
                       farmer={"phone": "9841000001", "name": "Sarita Tamang"})
    check("greeting handler personalises via Msg.farmer",
          r["intent"] == "greeting" and "Sarita" in r["reply"])
    conn.close()

    print(f"\n{passed} passed, {failed} failed.")
    sys.exit(1 if failed else 0)
