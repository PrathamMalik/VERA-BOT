"""Unit tests of the AI path with a fake model: good AI output is used, bad output is rejected -> rules-only."""
import json, os, sys
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from data import load_all, bundle
from vera import llm, composer

cats, ms, cus, trs, pairs = load_all()
U = list(ms.values())
os.environ.update(LLM_API_KEY="fake", LLM_MODEL="fake", LLM_RPM="1000")
P = {p["test_id"]: p for p in pairs}
fails = []


def run(tid, fake):
    llm.complete = (lambda *a, **k: json.dumps(fake)) if not callable(fake) else fake
    composer._cache.clear()
    c, m, t, cu = bundle(P[tid], cats, ms, cus, trs)
    return composer.compose(c, m, t, cu, universe=U)


def expect(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        fails.append(label)


# picks_line (#6): AI picks the review weak spot only
r = run("T09", {"picks": ["theme_wait_time"], "line": ""})
expect("#6 AI picks respected", r["_source"] == "hybrid" and "Wait time" in r["body"] and "CTR" not in r["body"])
r = run("T09", {"picks": ["made_up_id"], "line": ""})
expect("#6 unknown pick ids ignored -> default picks", "CTR" in r["body"])
# full (#5): good vs invented number
good5 = {"body": "Ramesh, summer is here:\n• ORS: +40%\n• Sunscreen: +38%\nYour 1,820 customers will need them. Draft a WhatsApp? Reply YES.", "rationale": "x"}
expect("#5 good AI body used", run("T05", good5)["_source"] == "ai")
bad5 = dict(good5, body=good5["body"].replace("1,820", "5,000"))
expect("#5 invented number rejected", run("T05", bad5)["_source"] == "rules")
expect("#5 missing bullets rejected", run("T05", {"body": "Ramesh, summer demand is up. Draft a post? Reply YES."})["_source"] == "rules")
# full (#19): qualifying question after yes rejected
r = run("T01", {"body": "Suresh, here's the plan:\n• Base: Weekday Lunch Thali @ ₹149\n• 10+ thalis: ₹140 (suggested)\nWould you like tiers? Reply YES."})
expect("#19 qualifying question rejected", r["_source"] == "rules")
# label (#4 isn't a test pair; use trigger directly)
c = cats["pharmacies"]; m = ms["m_009_apollo_pharmacy_jaipur"]; t = trs["trg_018_supply_atorvastatin_recall"]
llm.complete = lambda *a, **k: json.dumps({"label": "life-critical"}); composer._cache.clear()
expect("#4 AI label used", "life-critical" in composer.compose(c, m, t, None, universe=U)["body"])
llm.complete = lambda *a, **k: json.dumps({"label": "very scary"}); composer._cache.clear()
expect("#4 bad label -> fallback map", "chronic-important" in composer.compose(c, m, t, None, universe=U)["body"])
# relevance (#28): not relevant or AI failure -> skip
news = {"id": "n1", "kind": "local_news_event", "scope": "merchant", "merchant_id": "m_002_bharat_dentist_mumbai",
        "payload": {"city": "Mumbai", "headline": "Mumbai-Pune expressway closed for 3 hours"}, "urgency": 2, "suppression_key": "n1"}
m2 = ms["m_002_bharat_dentist_mumbai"]
llm.complete = lambda *a, **k: json.dumps({"relevant": False, "line": ""}); composer._cache.clear()
expect("#28 not relevant -> skip", composer.compose(cats["dentists"], m2, news, None, universe=U)["skip"])
llm.complete = lambda *a, **k: json.dumps({"relevant": True, "line": "Patients driving in from the Pune side may run late today, so keep a little buffer between appointments."}); composer._cache.clear()
r = composer.compose(cats["dentists"], m2, news, None, universe=U)
expect("#28 relevant -> sent", not r["skip"] and "expressway" in r["body"])
def boom(*a, **k): raise TimeoutError()
llm.complete = boom; composer._cache.clear()
expect("#28 AI down -> skip", composer.compose(cats["dentists"], m2, news, None, universe=U)["skip"])
expect("#5 AI down -> rules", run("T05", boom)["_source"] == "rules")
# heatwave (#27) line with a number -> dropped (no numbers rule)
heat = {"id": "h1", "kind": "weather_heatwave", "scope": "merchant", "merchant_id": "m_001_drmeera_dentist_delhi",
        "payload": {"city": "Delhi", "temp_c": 42}, "urgency": 2, "suppression_key": "h1"}
llm.complete = lambda *a, **k: json.dumps({"line": "Kids drink 3x more cold drinks in heat."}); composer._cache.clear()
r = composer.compose(cats["dentists"], ms["m_001_drmeera_dentist_delhi"], heat, None, universe=U)
expect("#27 AI line with invented number dropped", "3x" not in r["body"] and "42°C" in r["body"])
heat2 = dict(heat, payload={"city": "Mumbai", "temp_c": 42}); composer._cache.clear()
expect("#27 other city -> skip", composer.compose(cats["dentists"], ms["m_001_drmeera_dentist_delhi"], heat2, None, universe=U)["skip"])
print("\nLLM PATH OK" if not fails else f"\n{len(fails)} FAILED")
sys.exit(1 if fails else 0)
