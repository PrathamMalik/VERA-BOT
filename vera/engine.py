"""Request handling for the 5 (+teardown) endpoints — transport-agnostic (used by both the stdlib server and ASGI app)."""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone

from . import llm
from .composer import compose
from .templates import draft_for
from .util import parse_dt
from .conversation import respond
from .store import Store, VALID_SCOPES

STORE = Store()
POOL = ThreadPoolExecutor(max_workers=int(os.environ.get("WORKERS", "12")))
TICK_BUDGET = float(os.environ.get("TICK_BUDGET", "10"))     # seconds we allow LLM calls inside one /tick
MAX_ACTIONS = 20
MIN_GAP_MIN = float(os.environ.get("MIN_GAP_MINUTES", "30"))  # simulated minutes between two proactive sends to one merchant (unless they reply)

METADATA = {
    "team_name": os.environ.get("TEAM_NAME", "<TEAM NAME>"),
    "team_members": [m.strip() for m in os.environ.get("TEAM_MEMBERS", "<YOUR NAME>").split(",")],
    "model": None,
    "approach": ("Per-trigger decisions (rules / hybrid / AI-first): code computes every fact and number (peer averages from >=3 "
                 "merchants, data-backed case studies), at most one validated AI call per message, rules-only fallback for "
                 "speed and zero fabrication; rule-first conversation router (auto-reply, opt-out, yes->action, off-topic, honest answers)."),
    "contact_email": os.environ.get("CONTACT_EMAIL", "<EMAIL>"),
    "version": "2.0.0",
    "submitted_at": os.environ.get("SUBMITTED_AT", "2026-09-26T00:00:00Z"),
}


def _now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ------------------------------------------------------------------ endpoints
def healthz():
    return 200, {"status": "ok", "uptime_seconds": int(time.time() - STORE.started), "contexts_loaded": STORE.counts()}


def metadata():
    md = dict(METADATA)
    md["model"] = (f"{llm.provider()}:{llm.model_name()}" if llm.enabled() else "rules-only (no LLM key set)")
    return 200, md


def push_context(body: dict):
    scope, cid, version, payload = body.get("scope"), body.get("context_id"), body.get("version"), body.get("payload")
    if scope not in VALID_SCOPES:
        return 400, {"accepted": False, "reason": "invalid_scope", "details": f"scope must be one of {VALID_SCOPES}"}
    if not cid or not isinstance(payload, dict):
        return 400, {"accepted": False, "reason": "invalid_payload", "details": "context_id and object payload required"}
    try:
        version = int(version)
    except (TypeError, ValueError):
        return 400, {"accepted": False, "reason": "invalid_version", "details": "version must be an integer"}
    ok, cur = STORE.put_context(scope, cid, version, payload)
    if not ok:
        return 409, {"accepted": False, "reason": "stale_version", "current_version": cur}
    return 200, {"accepted": True, "ack_id": f"ack_{cid}_v{version}", "stored_at": _now_iso()}


def _consented(customer: dict | None) -> bool:
    if not customer:
        return True  # handled by merchant-routing fallback
    prefs = customer.get("preferences") or {}
    scope = (customer.get("consent") or {}).get("scope") or []
    return bool(scope) and prefs.get("reminder_opt_in", True) is not False


def _minutes_between(a, b):
    da, db = parse_dt(a), parse_dt(b)
    if not isinstance(da, datetime) or not isinstance(db, datetime):
        return None
    if da.tzinfo is None:
        da = da.replace(tzinfo=timezone.utc)
    if db.tzinfo is None:
        db = db.replace(tzinfo=timezone.utc)
    return (db - da).total_seconds() / 60


def _recently_in_touch(merchant: dict, now: str) -> bool:
    """#30: merchant talked to Vera in the last 48h (history timestamps or a real reply this session)."""
    if STORE.replied_since.get(merchant.get("merchant_id")):
        return True
    for h in merchant.get("conversation_history") or []:
        if h.get("from") == "merchant":
            mins = _minutes_between(h.get("ts"), now)
            if mins is not None and 0 <= mins <= 48 * 60:
                return True
    return False


def tick(body: dict):
    now = body.get("now") or _now_iso()
    available = list(dict.fromkeys(body.get("available_triggers") or []))
    universe = STORE.merchants()
    cands = []
    for tid in available:
        trg = STORE.get("trigger", tid)
        if not trg:
            continue
        mid = trg.get("merchant_id") or (trg.get("payload") or {}).get("merchant_id")
        merchant = STORE.get("merchant", mid)
        if not merchant:
            continue
        flags = STORE.merchant_flags.get(mid, {})
        kind = trg.get("kind", "")
        if flags.get("opted_out") or kind in flags.get("paused_kinds", set()):
            continue
        sk = trg.get("suppression_key") or f"{kind}:{mid}:{tid}"
        if sk in STORE.sent_suppression or sk in STORE.considered:
            continue
        cid = trg.get("customer_id")
        customer = STORE.get("customer", cid) if cid else None
        if cid and customer and not _consented(customer):
            continue
        if kind == "scheduled_recurring" and _recently_in_touch(merchant, now):
            STORE.considered.add(sk)
            continue
        if flags.get("tagline"):
            merchant = {**merchant, "_tagline": flags["tagline"]}
        d = draft_for(STORE.category_for(merchant) or {}, merchant, trg, customer, universe=universe,
                      rotation_index=STORE.rotation[mid], now=now)
        if d.skip and not (d.ai and d.ai.kind == "relevance" and llm.enabled()):
            STORE.considered.add(sk)  # deliberate restraint (decided in DECISIONS.md)
            continue
        target = ("customer", cid) if d.send_as == "merchant_on_behalf" else ("merchant", mid)
        cands.append({"urg": int(trg.get("urgency") or 1), "exp": str(trg.get("expires_at") or "9999"), "tid": tid, "trg": trg,
                      "m": merchant, "cu": customer, "sk": sk, "target": target, "kind": kind, "prio": d.ai.priority if d.ai else 0})
    cands.sort(key=lambda x: (-x["urg"], x["exp"]))

    merchant_kinds = {}
    for x in cands:
        if x["target"][0] == "merchant":
            merchant_kinds.setdefault(x["m"]["merchant_id"], []).append(x["kind"])
    chosen, targets = [], set()
    for x in cands:
        mid = x["m"]["merchant_id"]
        if x["target"] in targets:
            continue  # one new conversation per recipient per tick
        if x["target"][0] == "merchant":
            # #3: CDE is filler — only when nothing else is waiting for this merchant
            if x["kind"] == "cde_opportunity" and any(k != "cde_opportunity" for k in merchant_kinds.get(mid, [])):
                continue
            # overlap rule: next trigger for the same merchant only after a reply or a gap
            last = STORE.last_sent.get(mid)
            if last and not STORE.replied_since.get(mid):
                gap = _minutes_between(last, now)
                if gap is not None and gap < MIN_GAP_MIN:
                    continue
        targets.add(x["target"])
        chosen.append(x)
        if len(chosen) >= MAX_ACTIONS:
            break

    # compose in parallel; AI-heavy kinds first so they get the free-tier budget; slow -> rules-only version
    chosen.sort(key=lambda x: -x["prio"])
    def _compose(x, use_llm=True):
        mid = x["m"]["merchant_id"]
        prefix = "(For the owner, not the auto-reply)" if STORE.merchant_flags.get(mid, {}).get("auto_reply") and x["target"][0] == "merchant" else ""
        return compose(STORE.category_for(x["m"]) or {}, x["m"], x["trg"], x["cu"], use_llm, universe=universe,
                       rotation_index=STORE.rotation[mid], prefix=prefix, now=now)
    futures = {POOL.submit(_compose, x): x for x in chosen}
    done, _ = wait(futures, timeout=TICK_BUDGET)
    actions = []
    for fut, x in futures.items():
        try:
            msg = fut.result() if fut in done else _compose(x, False)
        except Exception:
            msg = _compose(x, False)
        tid, t, m, cu = x["tid"], x["trg"], x["m"], x["cu"]
        mid = m["merchant_id"]
        if msg.get("skip") or not msg.get("body"):
            STORE.considered.add(x["sk"])
            continue
        conv_id = f"conv_{mid}_{tid}"
        n = 2
        while STORE.conv(conv_id):
            conv_id = f"conv_{mid}_{tid}_{n}"; n += 1
        routed_customer = cu.get("customer_id") if (cu and msg["send_as"] == "merchant_on_behalf") else None
        conv = STORE.new_conv(conv_id, merchant_id=mid, customer_id=routed_customer, trigger_id=tid,
                              send_as=msg["send_as"], kind=t.get("kind"), offer=msg.get("_offer"))
        with STORE.lock:
            conv["followup"] = msg.get("_followup") or {}
            conv["turns"].append({"from": "bot", "body": msg["body"]})
            STORE.bodies_sent[conv_id].add(msg["body"])
            STORE.sent_suppression.add(msg["suppression_key"])
            if x["target"][0] == "merchant":
                STORE.last_sent[mid] = now
                STORE.replied_since[mid] = False
            if t.get("kind") == "scheduled_recurring":
                STORE.rotation[mid] += 1
        actions.append({
            "conversation_id": conv_id, "merchant_id": mid, "customer_id": routed_customer,
            "send_as": msg["send_as"], "trigger_id": tid,
            "template_name": msg["template_name"], "template_params": msg["template_params"],
            "body": msg["body"], "cta": msg["cta"], "suppression_key": msg["suppression_key"],
            "rationale": msg["rationale"],
        })
    return 200, {"actions": actions}


def reply(body: dict):
    if not body.get("conversation_id"):
        return 400, {"error": "conversation_id required"}
    fut = POOL.submit(respond, STORE, body)
    try:
        return 200, fut.result(timeout=float(os.environ.get("REPLY_BUDGET", "12")))
    except Exception as e:
        # never time out the judge: safe, honest holding reply
        return 200, {"action": "wait", "wait_seconds": 600, "rationale": f"Internal delay ({type(e).__name__}); backing off briefly."}


def teardown(_body=None):
    STORE.reset()
    return 200, {"ok": True, "wiped_at": _now_iso()}


ROUTES = {
    ("GET", "/v1/healthz"): lambda b: healthz(),
    ("GET", "/v1/metadata"): lambda b: metadata(),
    ("POST", "/v1/context"): push_context,
    ("POST", "/v1/tick"): tick,
    ("POST", "/v1/reply"): reply,
    ("POST", "/v1/teardown"): teardown,
    ("GET", "/"): lambda b: (200, {"service": "vera-bot", "endpoints": ["/v1/healthz", "/v1/metadata", "/v1/context", "/v1/tick", "/v1/reply", "/v1/teardown"]}),
}


def handle(method: str, path: str, body: dict | None):
    path = path.split("?")[0].rstrip("/") or "/"
    fn = ROUTES.get((method.upper(), path))
    if fn is None:
        return 404, {"error": "not_found", "path": path}
    try:
        return fn(body or {})
    except Exception as e:  # malformed input must never crash the server
        return 500, {"error": type(e).__name__, "details": str(e)[:300]}
