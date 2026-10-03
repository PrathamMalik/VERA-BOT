"""compose(category, merchant, trigger, customer) -> message dict.

Pipeline:  handler (code facts + rules-only body)  ->  at most ONE AI call (line / picks / full / relevance / label)
           ->  validator  ->  render.  Any AI failure, timeout, rate-limit or rejected output -> rules-only body.
"""
from __future__ import annotations

import hashlib
import json
import threading

from . import llm
from .templates import draft_for, Ctx
from .util import customer_first, owner_first
from .validator import check
from .quality import floor_check

VOICE = {
    "dentists": "clinical peer-to-peer (colleague, not salesperson), address as 'Dr. <name>', technical terms welcome, no hype",
    "salons": "warm, practical, friendly expert",
    "restaurants": "fellow operator talking shop (covers, footfall, delivery), busy-practical",
    "gyms": "coach energy — disciplined, motivating, data-aware; never promise weight loss",
    "pharmacies": "trustworthy neighbourhood pharmacist — precise, calm, never alarmist",
}
LANG = {"hinglish": "Natural Hindi-English code-mix in Roman script; Vera speaks as a woman ('kar deti hoon').",
        "hindi": "Simple Roman-script Hindi, respectful ('ji', 'aap').", "en": "Simple Indian English."}

SYSTEM = """You help Vera, magicpin's WhatsApp assistant for Indian local merchants.
Absolute rules:
1. Use ONLY facts in FACTS/DRAFT. Never invent numbers, names, competitors, studies, prices, dates, links or claims.
2. Never promise results ("guaranteed", "cures", "proves"). Respect taboo words.
3. Match the voice and language instructions. Max one emoji.
4. Output ONLY the JSON asked for — nothing else."""

TASK_FORMAT = {
    "line": 'Return {"line": "<one sentence>"}',
    "picks_line": 'Return {"picks": ["<candidate id>", ...], "line": "<optional sentence or empty>"}',
    "full": 'Return {"body": "<the full WhatsApp message, bullets with •, ONE CTA as the last line>", "rationale": "<one sentence>"}',
    "relevance": 'Return {"relevant": true|false, "line": "<1-2 sentences if relevant, else empty>"}',
    "label": 'Return {"label": "<one of the options>"}',
}

COACH_LINES = ["Coach's note: one focused move this week beats five half-done ones 💪",
               "Coach's note: consistency wins — small fix now, stronger month ahead 💪",
               "Coach's note: treat this like a training split — one clear target this week 💪"]

_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()


def _key(*objs) -> str:
    return hashlib.sha256(json.dumps(objs, sort_keys=True, default=str).encode()).hexdigest()


def _run_ai(d, c: Ctx, lang: str, taboos: list, who: list):
    """Do the one AI call for this draft. Returns (ai_output or None, note)."""
    t = d.ai
    prompt = json.dumps({
        "TASK": t.instruction, "FORMAT": TASK_FORMAT[t.kind],
        "OPTIONS": t.options or None, "CANDIDATES": t.candidates or None, "MAX_PICKS": t.max_picks if t.kind == "picks_line" else None,
        "NO_NUMBERS": t.no_numbers, "VOICE": VOICE.get(c.slug, "friendly professional"), "LANGUAGE": LANG.get(lang, LANG["en"]),
        "TABOO": taboos, "FACTS": d.facts, "DRAFT": d.body,
    }, ensure_ascii=False, default=str)
    try:
        out = llm.parse_json(llm.complete(SYSTEM, prompt, max_tokens=900 if t.kind == "full" else 300))
    except Exception as e:
        return None, f"rules-only ({type(e).__name__})"
    if t.kind == "label":
        return (out, "ai label") if out.get("label") in t.options else (None, "label rejected")
    if t.kind == "picks_line":
        ids = {x["id"] for x in t.candidates}
        out["picks"] = [p for p in out.get("picks", []) if p in ids][: t.max_picks]
    line = str(out.get("line", "") or "").strip()
    if line:
        probs = check(line, d.facts, d.body, taboos, max_len=320, no_numbers=t.no_numbers, max_emoji=0)
        if probs:
            out["line"] = ""
            if t.kind == "relevance":
                return None, f"relevance line rejected: {'; '.join(probs)}"
    if t.kind == "full":
        body = str(out.get("body", "")).strip()
        probs = check(body, d.facts, d.body, taboos, must_mention=who, need_bullets=2, max_emoji=1,
                      action_mode=(c.kind == "active_planning_intent"))
        if probs:
            return None, f"AI draft rejected: {'; '.join(probs)}"
    return out, "ai"


def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None, use_llm: bool = True,
            universe: list | None = None, rotation_index: int = 0, prefix: str = "", now: str | None = None) -> dict:
    ck = _key(category, merchant, trigger, customer, use_llm and llm.enabled(), rotation_index, prefix,
              [m.get("merchant_id") for m in universe or []], (now or "")[:10])
    with _cache_lock:
        if ck in _cache:
            return dict(_cache[ck])

    d = draft_for(category, merchant, trigger, customer, universe=universe, rotation_index=rotation_index, now=now)
    c = Ctx(category, merchant, trigger, customer, universe, now)
    lang = c.clang if d.send_as == "merchant_on_behalf" else c.lang
    taboos = (category.get("voice") or {}).get("vocab_taboo", [])
    who = [customer_first(customer)] if d.send_as == "merchant_on_behalf" and customer_first(customer) else [owner_first(merchant) or c.sal]

    body, source, note, rationale = d.body, "rules", "", d.rationale
    if d.ai and use_llm and llm.enabled():
        ai, note = _run_ai(d, c, lang, taboos, who)
        if ai is not None:
            if d.ai.kind == "full":
                body, source = ai["body"].strip(), "ai"
                rationale = str(ai.get("rationale") or rationale)
            elif d.render:
                rendered = d.render(ai)
                if rendered:
                    body, source = rendered, "hybrid"
                    d.skip = False
    elif d.ai:
        note = "rules-only (no AI key)"
    if d.ai and d.ai.kind == "relevance" and source == "rules":
        d.skip, body = True, ""
    # gym voice layer (category voice: energetic_disciplined, coach register) — merchant-facing only, no promises
    if body and not d.skip and c.slug == "gyms" and d.send_as == "vera" and "\n> " not in body:
        lines = body.split("\n")
        coach = COACH_LINES[sum(map(ord, merchant.get("merchant_id", ""))) % len(COACH_LINES)]
        if any(ord(ch) > 0x2600 for ch in body):  # keep to one emoji per message
            coach = coach.replace(" 💪", "")
        if len(lines) > 1 and coach not in body:
            body = "\n".join(lines[:-1] + [coach, lines[-1]])
    # quality floor (decision, eval round 1): AI/hybrid text that fails falls back to the rules text; rules text that fails -> skip
    floor = floor_check(body, merchant, trigger, customer, d.send_as) if body and not d.skip else []
    if floor and source != "rules" and d.body:
        body, source = d.body, "rules"
        note = (note + "; " if note else "") + f"AI text failed floor ({', '.join(floor)})"
        floor = floor_check(body, merchant, trigger, customer, d.send_as)
    if floor and not d.skip:
        d.skip, body = True, ""
        rationale = f"Skipped: message failed the quality floor ({', '.join(floor)}) — restraint over a weak send."
    if body and prefix:
        body = f"{prefix}\n{body}"
    kind = trigger.get("kind", "generic")
    result = {
        "body": "" if d.skip else body,
        "cta": d.cta, "send_as": d.send_as,
        "suppression_key": trigger.get("suppression_key") or f"{kind}:{merchant.get('merchant_id')}:{trigger.get('id')}",
        "rationale": rationale + (f" [{note}]" if note else ""),
        "template_name": f"vera_{kind}_v1" if d.send_as == "vera" else f"merchant_{kind}_v1",
        "template_params": [str(x) for x in d.template_params if x is not None],
        "skip": d.skip,
        "_offer": d.offer, "_followup": d.followup, "_source": source, "_facts": d.facts,
        "_ai_priority": d.ai.priority if d.ai else 0,
    }
    with _cache_lock:
        _cache[ck] = dict(result)
    return result
