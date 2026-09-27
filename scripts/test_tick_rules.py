"""Checks the tick policies from DECISIONS.md against a running bot."""
import json, os, sys, urllib.request
sys.path.insert(0, os.path.dirname(__file__))
from data import load_all
BOT = os.environ.get("BOT_URL", "http://localhost:8080")
FAILS = []


def call(method, path, body=None):
    req = urllib.request.Request(BOT + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def ok(c, label):
    print(("  PASS " if c else "  FAIL ") + label)
    if not c:
        FAILS.append(label)


def push(scope, cid, payload, v=1):
    call("POST", "/v1/context", {"scope": scope, "context_id": cid, "version": v, "payload": payload, "delivered_at": "x"})


cats, ms, cus, trs, _ = load_all()
call("POST", "/v1/teardown", {})
for k, v in cats.items(): push("category", k, v)
for k, v in ms.items(): push("merchant", k, v)
for k, v in cus.items(): push("customer", k, v)
for k, v in trs.items(): push("trigger", k, v)

tick = lambda now, ids: call("POST", "/v1/tick", {"now": now, "available_triggers": ids})["actions"]

a = tick("2026-04-26T10:00:00Z", ["trg_022_cde_webinar_dentists", "trg_023_competitor_opened_dentist"])
ok([x["trigger_id"] for x in a] == ["trg_023_competitor_opened_dentist"], "#3 CDE held back while a more important trigger waits")
a = tick("2026-04-26T10:10:00Z", ["trg_022_cde_webinar_dentists"])
ok(a == [], "overlap rule: no second message to the same merchant within the gap (no reply yet)")
a = tick("2026-04-26T10:45:00Z", ["trg_022_cde_webinar_dentists"])
ok([x["trigger_id"] for x in a] == ["trg_022_cde_webinar_dentists"], "after the gap, CDE (filler) goes out")

sched = {"id": "trg_sched_m006", "kind": "scheduled_recurring", "scope": "merchant", "merchant_id": "m_006_southindiancafe_restaurant_bangalore",
         "payload": {}, "urgency": 1, "suppression_key": "sched:m006:w1"}
push("trigger", sched["id"], sched)
ok(tick("2026-04-26T11:00:00Z", [sched["id"]]) == [], "#30 skip check-in: merchant talked to Vera within 48h")
sched2 = dict(sched, id="trg_sched_m029", merchant_id="m_029_anand_restaurant_chandigarh", suppression_key="sched:m029:w1")
push("trigger", sched2["id"], sched2)
a = tick("2026-04-26T11:00:00Z", [sched2["id"]])
ok(len(a) == 1 and "freebie" in a[0]["body"], "#30 first rotation format = curious ask (#16)")
sched3 = dict(sched2, id="trg_sched_m029_w2", suppression_key="sched:m029:w2")
push("trigger", sched3["id"], sched3)
a = tick("2026-04-26T12:00:00Z", [sched3["id"]])
ok(len(a) == 1 and "case study" in a[0]["body"].lower(), "#30 second rotation format = case study (#12)")

heat = {"id": "trg_heat_delhi", "kind": "weather_heatwave", "scope": "merchant", "merchant_id": "m_005_pizzajunction_restaurant_delhi",
        "payload": {"city": "Delhi", "temp_c": 42}, "urgency": 3, "suppression_key": "heat:delhi:1"}
heat_b = dict(heat, id="trg_heat_blr", merchant_id="m_006_southindiancafe_restaurant_bangalore", suppression_key="heat:delhi:2")
push("trigger", heat["id"], heat); push("trigger", heat_b["id"], heat_b)
a = tick("2026-04-26T13:00:00Z", [heat["id"], heat_b["id"]])
print("    " + (a[0]["body"].replace("\n", "\n    ") if a else "(none)"))
ok([x["merchant_id"] for x in a] == ["m_005_pizzajunction_restaurant_delhi"], "#27 heatwave: Delhi restaurant yes, Bangalore restaurant skipped")

news = {"id": "trg_news", "kind": "local_news_event", "scope": "merchant", "merchant_id": "m_002_bharat_dentist_mumbai",
        "payload": {"city": "Mumbai", "headline": "Mumbai-Pune expressway closed for 3 hours"}, "urgency": 2, "suppression_key": "news:1"}
push("trigger", news["id"], news)
ok(tick("2026-04-26T13:00:00Z", [news["id"]]) == [], "#28 local news without AI -> skipped (restraint)")

call("POST", "/v1/reply", {"conversation_id": "c_ar", "merchant_id": "m_004_glamour_salon_pune", "from_role": "merchant",
                           "message": "Thank you for contacting us! We will get back to you shortly.", "turn_number": 2})
a = tick("2026-04-26T13:00:00Z", ["trg_009_winback_glamour"])
ok(len(a) == 1 and a[0]["body"].startswith("(For the owner"), "auto-reply flag -> next proactive message addresses the owner")

call("POST", "/v1/reply", {"conversation_id": a[0]["conversation_id"], "merchant_id": "m_004_glamour_salon_pune", "from_role": "merchant",
                           "message": "not interested", "turn_number": 2})
t2 = dict(trs["trg_009_winback_glamour"], id="trg_wb2", suppression_key="winback:again")
push("trigger", "trg_wb2", t2)
ok(tick("2026-04-26T15:00:00Z", ["trg_wb2"]) == [], "S4 'not interested' pauses that trigger type for the merchant")

print("\nTICK RULES OK" if not FAILS else f"\n{len(FAILS)} FAILED")
sys.exit(1 if FAILS else 0)
