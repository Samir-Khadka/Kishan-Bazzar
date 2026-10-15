"""
Tests for the conversational assistant NLU engine (assistant.py).

Run: python3 tests/test_assistant.py
Covers: language detection, crop extraction (3 scripts), intent
classification, follow-up context, and the /v1/assistant/chat endpoint.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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

# --- language detection ---
check("devanagari -> ne", assistant.detect_language("धानको भाउ कति?") == "ne")
check("roman -> ro", assistant.detect_language("dhanko bhau kati?") == "ro")
check("english -> en", assistant.detect_language("what is the paddy price?") == "en")
check("roman mausam -> ro", assistant.detect_language("aaja mausam kasto cha?") == "ro")
check("mixed devanagari wins", assistant.detect_language("hello नमस्ते") == "ne")

# --- crop extraction: all three scripts ---
check("devanagari crop", assistant.extract_crops("गोलभेँडाको भाउ?") == ["tomato"])
check("english crop", assistant.extract_crops("potato price?") == ["potato"])
check("roman crop", assistant.extract_crops("aaluko bhau kati?") == ["potato"])
check("roman alias makai", assistant.extract_crops("makai bechne") == ["maize"])
check("no false positive: 'value' has 'alu' inside",
      "potato" not in assistant.extract_crops("what is the value?"))
check("multiple crops", set(assistant.extract_crops("aalu ra pyaj ko bhau")) == {"potato", "onion"})

# --- quantity extraction ---
kg, _u = assistant.extract_quantity("100 kg aalu")
check("kg quantity", kg == 100)
kg, _u = assistant.extract_quantity("2 quintal dhan")
check("quintal -> kg", kg == 200)
kg, _u = assistant.extract_quantity("5 muri")
check("muri -> kg", kg == 260)

# --- intent classification ---
def intent_of(text, ctx=None):
    return assistant.classify_intent(text, ctx or {})[0]

check("price intent ne", intent_of("आलुको भाउ कति?") == "price")
check("price intent ro", intent_of("tamatar ko bhau?") == "price")
check("price intent en", intent_of("what is the onion price?") == "price")
check("weather intent", intent_of("भोलि पानी पर्छ?") == "weather")
check("weather intent ro", intent_of("mausam kasto cha?") == "weather")
check("sell intent", intent_of("kasari bechne?") == "sell")
check("plant intent", intent_of("ke lagaune ahile?") == "plant")
check("ledger intent", intent_of("mero hisab herna") == "ledger")
check("greeting", intent_of("namaste") == "greeting")
check("thanks", intent_of("dhanyabad!") == "thanks")
check("help", intent_of("what can you do?") == "help")
check("bare crop -> price", intent_of("गोलभेँडा") == "price")
check("unknown -> None", intent_of("xyzzy plugh") is None)

# --- follow-up context ---
ctx = {"last_intent": "price", "last_crops": ["potato"]}
check("follow-up 'ani pyaj ko?' -> price",
      assistant.classify_intent("ani pyaj ko?", ctx)[0] == "price")

# --- end-to-end chat() ---
r = assistant.chat("t1", "namaste")
check("chat greeting", "नमस्ते" in r["reply"] or "Namaste" in r["reply"])
check("chat returns lang", r["lang"] in ("ne", "ro", "en"))

r = assistant.chat("t2", "aaluko bhau kati?")
check("chat roman price", r["intent"] == "price" and r["lang"] == "ro")

# seed a priced lot so the price handler has real data
db.init_db(fresh=True)
seed.seed()
_conn = db.get_db()
_conn.execute(
    "INSERT INTO lots (id, coop_id, crop_code, grade, target_kg, pooled_kg, rate_per_kg, status) "
    "VALUES ('LOT-TEST1','kavre-veg','DHAN','A',1000,500,32,'confirmed')")
_conn.commit()
r = assistant.chat("t3", "धानको भाउ?", db_conn=_conn)
check("chat ne price", r["intent"] == "price" and "रू" in r["reply"])
_conn.close()

# follow-up across turns in one session
assistant.chat("s1", "potato price?")
r = assistant.chat("s1", "and onion?")
check("session follow-up keeps price intent", r["intent"] == "price")

r = assistant.chat("t4", "xyzzy nonsense words here")
check("fallback has chips", len(r["chips"]) > 0)

# --- endpoint via Flask test client ---
db.init_db(fresh=True)
seed.seed()
import app as app_module
client = app_module.app.test_client()

resp = client.post("/v1/assistant/chat", json={"message": "namaste", "session_id": "e2e1"})
check("endpoint 200", resp.status_code == 200)
data = resp.get_json()
check("endpoint has reply", bool(data.get("reply")))
check("endpoint has chips", isinstance(data.get("chips"), list))

resp = client.post("/v1/assistant/chat", json={"message": "aaluko bhau?", "session_id": "e2e2"})
check("endpoint roman price", resp.get_json()["intent"] == "price")

resp = client.post("/v1/assistant/chat", json={"message": "", "session_id": "e2e3"})
check("endpoint rejects empty", resp.status_code == 400)

resp = client.post("/v1/assistant/chat", json={"message": "x" * 501, "session_id": "e2e4"})
check("endpoint rejects too-long", resp.status_code == 400)

# --- agronomy intent ---
check("agronomy pest ne", intent_of("गोलभेँडामा डढुवा रोग लाग्यो") == "agronomy")
check("agronomy fert ro", intent_of("aaluma kun mal halne?") == "agronomy")
check("agronomy en", intent_of("tomato blight treatment") == "agronomy")

r = assistant.chat("ag1", "गोलभेँडामा डढुवा रोग", db_conn=_conn)
check("agronomy pest reply", r["intent"] == "agronomy" and "डढुवा" in r["reply"])

r = assistant.chat("ag2", "kaulima kun mal halne?")
check("agronomy fert reply", r["intent"] == "agronomy")

r = assistant.chat("ag3", "mal ko barema")
check("agronomy asks crop", r["intent"] == "agronomy" and len(r["chips"]) > 0)

print(f"\n{passed} passed, {failed} failed.")
sys.exit(1 if failed else 0)
