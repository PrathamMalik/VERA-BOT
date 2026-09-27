"""End-to-end self-test against a running bot (default http://localhost:8080).

Mimics the real judge: warmup push of all base contexts, idempotency/409 checks, ticks over every trigger,
schema + latency checks, then the replay scenarios (auto-reply hell, intent transition, hostile/off-topic,
customer slot booking, defer). Prints PASS/FAIL per check and exits non-zero on any failure.
"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
from data import load_all  # noqa: E402

BOT = os.environ.get("BOT_URL", "http://localhost:8080")
FAILS = []


def call(method, path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BOT + path, data=data, method=method, headers={"content-type": "application/json"})
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode()), time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}"), time.time() - t


def ok(cond, label):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        FAILS.append(label)


def main():
    cats, ms, cus, trs, pairs = load_all()
    call("POST", "/v1/teardown", {})
    print("== warmup")
    for slug, c in cats.items():
        call("POST", "/v1/context", {"scope": "category", "context_id": slug, "version": 1, "payload": c, "delivered_at": "2026-04-26T10:00:00Z"})
    for mid, m in ms.items():
        call("POST", "/v1/context", {"scope": "merchant", "context_id": mid, "version": 1, "payload": m, "delivered_at": "2026-04-26T10:00:00Z"})
    for cid, c in cus.items():
        call("POST", "/v1/context", {"scope": "customer", "context_id": cid, "version": 1, "payload": c, "delivered_at": "2026-04-26T10:00:00Z"})
    s, h, _ = call("GET", "/v1/healthz")
    ok(h["contexts_loaded"] == {"category": 5, "merchant": 50, "customer": 200, "trigger": 0}, f"healthz counts {h['contexts_loaded']}")
    s, r, _ = call("POST", "/v1/context", {"scope": "category", "context_id": "dentists", "version": 1, "payload": cats["dentists"], "delivered_at": "x"})
    ok(s == 200 and r["accepted"], "same version re-post is idempotent (200 accepted)")
    call("POST", "/v1/context", {"scope": "category", "context_id": "dentists", "version": 3, "payload": cats["dentists"], "delivered_at": "x"})
    s, r, _ = call("POST", "/v1/context", {"scope": "category", "context_id": "dentists", "version": 2, "payload": cats["dentists"], "delivered_at": "x"})
    ok(s == 409 and r.get("current_version") == 3, "lower version -> 409 stale_version")

    print("== ticks over all 100 triggers")
    for tid, t in trs.items():
        call("POST", "/v1/context", {"scope": "trigger", "context_id": tid, "version": 1, "payload": t, "delivered_at": "x"})
    tids = list(trs)
    all_actions, worst = [], 0
    for i in range(0, len(tids), 10):
        s, r, dt = call("POST", "/v1/tick", {"now": "2026-04-26T10:30:00Z", "available_triggers": tids[i:i + 10]})
        worst = max(worst, dt)
        all_actions += r["actions"]
    ok(worst < 15, f"slowest tick {worst:.2f}s < 15s")
    req = {"conversation_id", "merchant_id", "send_as", "trigger_id", "body", "cta", "suppression_key", "rationale", "template_name", "template_params"}
    ok(all(req <= set(a) for a in all_actions), f"all {len(all_actions)} actions have required fields")
    ok(all(a["body"].strip() for a in all_actions), "no empty bodies")
    bodies = [a["body"] for a in all_actions]
    ok(len(bodies) == len(set(bodies)), f"no duplicate bodies across actions ({len(bodies)} / {len(set(bodies))} unique)")
    ok(all("{" not in b and "None" not in b and "nan" not in b.lower().split() for b in bodies), "no template artifacts (braces/None) in bodies")
    sent_keys = [a["suppression_key"] for a in all_actions]
    s, r, _ = call("POST", "/v1/tick", {"now": "2026-04-26T10:35:00Z", "available_triggers": tids})
    again = [a for a in r["actions"] if a["suppression_key"] in sent_keys]
    ok(not again and len(sent_keys) == len(set(sent_keys)), f"no suppression key sent twice (second pass sent {len(r['actions'])} previously-deferred triggers)")
    all_actions += r["actions"]
    json.dump(all_actions, open(os.path.join(os.path.dirname(__file__), "..", "out_tick_actions.json"), "w"), ensure_ascii=False, indent=1)

    print("== replay: auto-reply hell (new conversation id each turn, like the simulator)")
    auto = "Thank you for contacting us! Our team will respond shortly."
    ended_at = None
    for i in range(1, 5):
        s, r, _ = call("POST", "/v1/reply", {"conversation_id": f"conv_auto_{i}", "merchant_id": "m_002_bharat_dentist_mumbai", "customer_id": None,
                                             "from_role": "merchant", "message": auto, "received_at": "x", "turn_number": i + 1})
        print(f"    turn {i}: {r['action']} {r.get('body', '')[:90]}")
        if r["action"] == "end":
            ended_at = i; break
    ok(ended_at is not None and ended_at <= 2, f"auto-reply exit by turn 2 (ended at {ended_at})")

    print("== replay: auto-reply inside a real conversation (Hindi canned text)")
    a = next(x for x in all_actions if x["merchant_id"] == "m_003_studio11_salon_hyderabad")
    hi_auto = "Aapki jaankari ke liye bahut-bahut shukriya. Main aapki yeh sabhi baatein team tak pahuncha deti hoon."
    r1 = call("POST", "/v1/reply", {"conversation_id": a["conversation_id"], "merchant_id": a["merchant_id"], "from_role": "merchant", "message": hi_auto, "turn_number": 2})[1]
    r2 = call("POST", "/v1/reply", {"conversation_id": a["conversation_id"], "merchant_id": a["merchant_id"], "from_role": "merchant", "message": hi_auto, "turn_number": 3})[1]
    print(f"    {r1['action']}: {r1.get('body', '')[:100]}\n    {r2['action']}")
    ok(r1["action"] == "send" and r2["action"] == "end", "Hindi auto-reply: one nudge then exit")

    print("== replay: intent transition")
    a = next(x for x in all_actions if x["trigger_id"] == "trg_013_corporate_thali_planning")
    s, r, _ = call("POST", "/v1/reply", {"conversation_id": a["conversation_id"], "merchant_id": a["merchant_id"], "from_role": "merchant",
                                         "message": "Ok lets do it. Whats next?", "turn_number": 2})
    body = r.get("body", "").lower()
    print("    " + r.get("body", "").replace("\n", "\n    ")[:600])
    ok(r["action"] == "send" and not any(q in body for q in ["would you", "do you", "can you tell", "what if", "how about"]), "commitment -> action, no qualifying question")
    s, r, _ = call("POST", "/v1/reply", {"conversation_id": "conv_intent_1", "merchant_id": "m_001_drmeera_dentist_delhi", "from_role": "merchant",
                                         "message": "Ok lets do it. Whats next?", "turn_number": 2})
    body = r.get("body", "").lower()
    print("    (unknown conv) " + r.get("body", "")[:200].replace("\n", " | "))
    ok(any(w in body for w in ["done", "sending", "draft", "here", "confirm", "proceed", "next"]) and not any(q in body for q in ["would you", "do you", "can you tell", "what if", "how about"]),
       "simulator intent check passes on a conversation the bot never opened")

    def rep(conv_id, mid, msg, role="merchant", cid=None, received_at="2026-04-26T10:45:00Z"):
        return call("POST", "/v1/reply", {"conversation_id": conv_id, "merchant_id": mid, "customer_id": cid, "from_role": role,
                                          "message": msg, "received_at": received_at, "turn_number": 2})[1]

    print("== replay: hostile then off-topic (brief's replay 3)")
    a = next(x for x in all_actions if x["merchant_id"] == "m_005_pizzajunction_restaurant_delhi")
    r1 = rep(a["conversation_id"], a["merchant_id"], "This is useless nonsense, you people are a scam")
    r2 = rep(a["conversation_id"], a["merchant_id"], "Can you also help me file my GST?")
    print(f"    {r1['action']}: {r1.get('body', '')[:140]}\n    {r2['action']}: {r2.get('body', '')[:180]}")
    ok(r1["action"] == "send" and "sorry" in r1["body"].lower() and "stop" in r1["body"].lower(), "S3 angry -> apology + STOP exit")
    ok(r2["action"] == "send" and "ca" in r2["body"].lower() and "reply yes" not in r2["body"].lower(), "S7 'file my GST' (complex) -> boundary only, no push")
    r2b = rep("conv_gst_facts", "m_006_southindiancafe_restaurant_bangalore", "Is there any GST change for takeaway packaging?")
    print(f"    GST facts: {r2b.get('body', '')[:260]}")
    ok("GST Council" in r2b.get("body", "") and "not tax advice" in r2b.get("body", ""), "S7 GST question matching an official item -> source-cited facts + disclaimer")
    r3 = rep("conv_hostile", "m_001_drmeera_dentist_delhi", "Stop messaging me. This is useless spam.")
    ok(r3["action"] == "send" and "won't" in r3.get("body", "") and "START" in r3.get("body", ""), "S2 'stop' -> one confirmation (won't message, reply START)")
    ok(rep("conv_hostile", "m_001_drmeera_dentist_delhi", "hello?")["action"] == "end", "S2 after opt-out -> conversation stays ended")
    s, r, _ = call("POST", "/v1/tick", {"now": "2026-04-26T12:00:00Z", "available_triggers": ["trg_022_cde_webinar_dentists", "trg_009_winback_glamour"]})
    ok(all(x["merchant_id"] != "m_001_drmeera_dentist_delhi" for x in r["actions"]), "S2 opted-out merchant gets no further proactive sends")

    print("== replay: customer booking, recall question")
    a = next((x for x in all_actions if x["trigger_id"] == "trg_003_recall_due_priya"), None)
    if a:
        r = rep(a["conversation_id"], a["merchant_id"], "2", "customer", a["customer_id"])
        ok(r["action"] == "send" and "Thu 6 Nov" in r["body"], "customer picks slot 2 -> booked Thu 6 Nov")
    a = next((x for x in all_actions if x["trigger_id"] == "trg_019_chronic_refill_grandfather"), None)
    if a:
        ok("recall" not in a["body"].lower(), "#22 refill message does not mention the recall")
        r = rep(a["conversation_id"], a["merchant_id"], "Is my atorvastatin safe? I heard about a recall", "customer", a["customer_id"])
        print(f"    {r.get('body', '')[:200]}")
        ok("due process" in r.get("body", "").lower(), "#22 customer asks about recall -> due-process reassurance")

    print("== replay: not now / not interested / CALL / START")
    a = next(x for x in all_actions if x["merchant_id"] == "m_007_powerhouse_gym_bangalore" and x["send_as"] == "vera")
    r = rep(a["conversation_id"], a["merchant_id"], "busy right now, message me after 6")
    print(f"    {r['action']} {r.get('wait_seconds')}s: {r.get('body')}")
    ok(r["action"] == "wait" and r.get("body") and 3600 <= r.get("wait_seconds", 0) <= 86400, "S5 'after 6' -> acknowledge + wait until then")
    a = next(x for x in all_actions if x["merchant_id"] == "m_010_sunrisepharm_pharmacy_lucknow")
    r = rep(a["conversation_id"], a["merchant_id"], "not interested")
    ok(r["action"] == "send" and ("no problem" in r["body"].lower() or "koi baat nahi" in r["body"].lower()), "S4 not interested -> polite close")
    ok(rep(a["conversation_id"], a["merchant_id"], "ok")["action"] == "end", "S4 conversation ended after close")
    r = rep("conv_call", "m_008_zenyoga_gym_chennai", "CALL")
    ok("call" in r.get("body", "").lower() and "magicpin team" in r.get("body", ""), "S8 CALL -> human handoff")
    ok(rep("conv_call2", "m_001_drmeera_dentist_delhi", "START")["action"] == "send", "START re-subscribes")

    print("== replay: questions")
    r = rep("conv_q1", "m_008_zenyoga_gym_chennai", "Will this get me more members?")
    print(f"    {r.get('body', '')[:300]}")
    ok("CALL" in r.get("body", "") and "won't guess" in r.get("body", ""), "S8 unknown answer -> honest + CALL option")

    print("== replay: yes -> deliver + approve + next step")
    a = next(x for x in all_actions if x["merchant_id"] == "m_009_apollo_pharmacy_jaipur" and x["send_as"] == "vera")
    r = rep(a["conversation_id"], a["merchant_id"], "haan theek hai, kar do")
    print(f"    {r['action']}: {r.get('body', '')[:260]}")
    ok(r["action"] == "send" and not any(q in r["body"].lower() for q in ["would you", "do you", "what if", "how about"]), "S6 Hinglish yes -> delivered, no re-qualifying")
    a = next((x for x in all_actions if x["trigger_id"] == "trg_023_competitor_opened_dentist"), None) or \
        next((x for x in all_actions if "competitor" in x["trigger_id"]), None)
    if a:
        r = rep(a["conversation_id"], a["merchant_id"], "yes please")
        print("    " + r.get("body", "").replace("\n", "\n    ")[:600])
        ok("*Common ways" in r.get("body", "") and not any(ch.isdigit() for ch in r["body"].split("*Common ways")[1]), "#6 YES -> positives + strategies (no numbers in strategies)")

    print("== replay: max turns + sticky language")
    conv = "conv_turns"
    acts = [rep(conv, "m_006_southindiancafe_restaurant_bangalore", m)["action"] for m in ["hmm", "k", "hmm", "ok"]]
    ok(acts[:2] == ["send", "send"] and "end" in acts[2:], f"3 non-real replies in a row -> end ({acts})")
    r1 = rep("conv_lang", "m_006_southindiancafe_restaurant_bangalore", "haan bhai kya chal raha hai")
    ok(r1.get("body") and "Ho gaya" not in r1["body"][:10], "one Hindi message does not switch an English-profile merchant")
    rep("conv_lang", "m_006_southindiancafe_restaurant_bangalore", "haan theek hai bhai batao")
    r3 = rep("conv_lang", "m_006_southindiancafe_restaurant_bangalore", "haan bhai kar do yeh kaam")
    ok("Ho gaya" in r3.get("body", "") or "yeh raha" in r3.get("body", "").lower() or "kar dungi" in r3.get("body", "").lower(), "3 Hindi messages in a row -> switch to Hinglish")

    s, r, _ = call("POST", "/v1/teardown", {})
    s, h, _ = call("GET", "/v1/healthz")
    ok(sum(h["contexts_loaded"].values()) == 0, "teardown wipes state")
    print(f"\n{'ALL CHECKS PASSED' if not FAILS else str(len(FAILS)) + ' FAILED: ' + '; '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
