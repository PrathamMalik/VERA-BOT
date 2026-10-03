"""One handler per trigger kind, implementing DECISIONS.md.

Every handler returns a Draft:
  body       the rules-only version (final format, always safe to send)
  ai         optional AITask: the ONE AI call this message may use (line / picks_line / full / relevance / label)
  render     render(ai_output_or_None) -> body   (for hybrid messages: code facts + the AI part)
  skip       True when the decision is "send nothing" (restraint)
  followup   data the conversation handler needs if the merchant says YES
The AI never produces a number the code didn't supply (enforced in composer/validator).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from .peers import PeerGroup, weak_spots, positives, case_patterns, noun
from .util import (L, inr, num, pct, nice_date, nice_time, humanize, first_sentence, active_offers,
                   salutation, owner_first, merchant_lang, customer_lang, customer_first,
                   parent_name, child_name, name_from_customer_id, parse_dt, WEEKDAYS)


@dataclass
class AITask:
    kind: str                      # line | picks_line | full | relevance | label
    instruction: str
    candidates: list = field(default_factory=list)   # [{"id", "text"}]
    max_picks: int = 3
    options: list = field(default_factory=list)      # for label
    no_numbers: bool = False
    priority: int = 1                                 # limiter priority (higher first)


@dataclass
class Draft:
    body: str
    cta: str = "binary_yes_stop"
    send_as: str = "vera"
    facts: dict = field(default_factory=dict)
    offer: str = ""
    rationale: str = ""
    template_params: list = field(default_factory=list)
    ai: AITask | None = None
    render: Callable | None = None
    skip: bool = False
    followup: dict = field(default_factory=dict)


class Ctx:
    def __init__(self, category, merchant, trigger, customer=None, universe=None, now=None):
        self.category = category or {}
        self.merchant = merchant or {}
        self.trigger = trigger or {}
        self.customer = customer
        self.universe = universe
        self.slug = self.merchant.get("category_slug") or self.category.get("slug") or ""
        self.ident = self.merchant.get("identity", {}) or {}
        self.mname = self.ident.get("name", "your business")
        self.locality = self.ident.get("locality", "")
        self.city = self.ident.get("city", "")
        self.sal = salutation(self.merchant, self.slug)
        self.first = owner_first(self.merchant)
        self.lang = merchant_lang(self.merchant, self.category)
        self.clang = customer_lang(customer, self.merchant)
        self.perf = self.merchant.get("performance", {}) or {}
        self.delta = self.perf.get("delta_7d", {}) or {}
        self.bench = self.category.get("peer_stats", {}) or {}
        self.agg = self.merchant.get("customer_aggregate", {}) or {}
        self.offers = active_offers(self.merchant)
        self.payload = self.trigger.get("payload", {}) or {}
        self.placeholder = bool(self.payload.get("placeholder"))
        self.kind = self.trigger.get("kind", "")
        self.history = self.merchant.get("conversation_history") or []
        self._pg = None
        self.now = _as_dt(now)

    def days_until(self, iso, fallback=None):
        """Whole days from the judge's clock to `iso` (negative = past). Falls back to the payload's own count when no clock is given."""
        d = _as_dt(iso)
        if self.now is None or d is None:
            return fallback
        return (d.date() - self.now.date()).days

    def days_since(self, iso, fallback=None):
        v = self.days_until(iso, None)
        return -v if v is not None else fallback

    def ist_date(self, iso):
        d = _as_dt(iso)
        return d.astimezone(IST).date() if d else None

    @property
    def pg(self):
        if self._pg is None:
            self._pg = PeerGroup(self.merchant, self.category, self.universe)
        return self._pg

    def digest(self, item_id=None, kinds=None):
        items = self.category.get("digest") or []
        if item_id:
            for d in items:
                if d.get("id") == item_id:
                    return d
        for k in kinds or []:
            for d in items:
                if d.get("kind") == k:
                    return d
        return None

    def beat(self, *keywords):
        for kw in keywords:
            for b in self.category.get("seasonal_beats") or []:
                if kw.lower() in (b.get("note", "") + " " + b.get("month_range", "")).lower():
                    return b
        return None

    def pos_theme(self):
        th = sorted([t for t in self.merchant.get("review_themes") or [] if t.get("sentiment") == "pos"],
                    key=lambda t: -int(t.get("occurrences_30d") or 0))
        return th[0] if th else None

    def neg_theme(self, name=None):
        th = [t for t in self.merchant.get("review_themes") or [] if t.get("sentiment") == "neg"]
        if name:
            th = [t for t in th if t.get("theme") == name]
        th.sort(key=lambda t: -int(t.get("occurrences_30d") or 0))
        return th[0] if th else None

    def catalog_offer(self, kinds=("service_at_price",), keywords=()):
        for o in self.category.get("offer_catalog") or []:
            t = o.get("title", "")
            if keywords and not any(k in t.lower() for k in keywords):
                continue
            if o.get("type") in kinds:
                return t
        return None

    def trends(self):
        return self.category.get("trend_signals") or []

    def best_trend(self, focus=""):
        ts = self.trends()
        if not ts:
            return None
        themes = []
        for t in self.merchant.get("review_themes") or []:
            themes += [f"{t.get('theme', '')} {t.get('common_quote', '')}"] * max(1, int(t.get("occurrences_30d") or 1) // 3)
        words = " ".join([focus] * 3 + self.offers + themes + [self.mname, self.history_text()]).lower()
        cities = {"delhi", "mumbai", "bangalore", "bengaluru", "hyderabad", "chennai", "pune", "jaipur", "lucknow", "chandigarh", "ahmedabad"}
        ts = [t for t in ts if not (set(re.findall(r"[a-z]+", t.get("query", "").lower())) & (cities - {self.city.lower()}))] or ts

        def score(t):
            q = [w for w in re.findall(r"[a-z]+", t.get("query", "").lower()) if len(w) > 3 and w not in ("near", "price", "classes", "program")]
            return (sum(words.count(w[:6]) for w in q), float(t.get("delta_yoy") or 0))
        return sorted(ts, key=score, reverse=True)[0]

    def history_text(self):
        return " ".join(str(h.get("body", "")) for h in self.history)

    def last_vera_body(self):
        return next((h.get("body", "") for h in reversed(self.history) if h.get("from") == "vera"), "")

    def last_merchant_body(self):
        return next((h.get("body", "") for h in reversed(self.history) if h.get("from") == "merchant"), "")

    def base_facts(self):
        return {
            "merchant_name": self.mname, "owner": self.sal, "locality": self.locality, "city": self.city,
            "category": self.slug, "language": self.lang,
            "performance_30d": {k: self.perf.get(k) for k in ("views", "calls", "directions", "ctr", "leads") if k in self.perf},
            "change_7d": self.delta, "active_offers": self.offers, "verified": self.ident.get("verified"),
            "customer_aggregate": self.agg, "review_themes": self.merchant.get("review_themes") or [],
            "subscription": self.merchant.get("subscription") or {}, "trigger_kind": self.kind,
            "trigger_payload": self.payload, "recent_conversation": self.history[-4:],
        }


# =============================================================== small helpers
from datetime import datetime, timezone, timedelta
IST = timezone(timedelta(hours=5, minutes=30))


def _as_dt(x):
    d = parse_dt(x) if x else None
    if d is None:
        return None
    if not isinstance(d, datetime):
        d = datetime(d.year, d.month, d.day)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


AUDIENCE = {"dentists": "patient", "gyms": "member", "salons": "client", "restaurants": "customer", "pharmacies": "customer"}


def yes(lang, en_action, hi_action=None):
    if lang in ("hinglish", "hindi") and hi_action:
        return f"{hi_action} Reply YES."
    return f"Want me to {en_action}? Reply YES."


def bullets(items):
    return "\n".join(f"• {i}" for i in items if i)


def _price(title):
    m = re.search(r"₹\s?([\d,]+)", title or "")
    return int(m.group(1).replace(",", "")) if m else None


def _round5(x):
    return int(5 * round(float(x) / 5))


def clean_dates(text):
    return re.sub(r"\b(20\d\d-\d\d-\d\d)\b", lambda m: nice_date(m.group(1), True), str(text or ""))


def sentences(text):
    from .util import split_sentences
    return split_sentences(text)


def _metric_label(m):
    return {"calls": "calls", "views": "profile views", "directions": "direction requests", "ctr": "CTR",
            "review_count": "Google reviews"}.get(m, humanize(m))


def _moves(delta, sign):
    out = []
    for k, v in (delta or {}).items():
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if sign * v > 0:
            out.append((k.replace("_pct", ""), v))
    out.sort(key=lambda x: -abs(x[1]))
    return out


# =============================================================== strengths-first pattern (shared by #6 #7 #8 #10 #15)
SRC_PEERS = ("magicpin data, last 30 days", "magicpin data, pichhle 30 din")


def own_numbers(c: Ctx) -> str:
    """'Your last 30 days: 5,934 profile views, 65 calls (magicpin dashboard)' — the merchant's own reflection."""
    v, k = c.perf.get("views"), c.perf.get("calls")
    if not v and not k:
        return ""
    parts = ([f"{num(v)} profile views"] if v else []) + ([f"{num(k)} calls"] if k else [])
    line = L(c.lang, f"Your last 30 days: {', '.join(parts)} (magicpin dashboard)", f"Aapke pichhle 30 din: {', '.join(parts)} (magicpin dashboard)")
    if c.slug == "gyms":  # the number a gym owner thinks in: members
        mem, ytd = c.agg.get("total_active_members"), c.agg.get("total_unique_ytd")
        who = f"{num(mem)} active members" if mem else f"{num(ytd)} members this year" if ytd else ""
        if who:
            line = f"{who} (your magicpin customer data) · " + line[0].lower() + line[1:]
    return line


SINGULAR = {"dentists": "dental clinic", "salons": "salon", "restaurants": "restaurant", "gyms": "gym", "pharmacies": "pharmacy"}


def weak_spot_draft(c: Ctx, head: str, rationale: str, extra_after: str = "", exclude_types=(), min_spots=0) -> Draft:
    """Strengths first (decision, eval round 1): where they already beat similar merchants, then 1-2 places to win more.
    Every comparison carries its source. One story, one CTA."""
    ws = [w for w in weak_spots(c.merchant, c.pg, c.lang) if w["type"] not in exclude_types]
    pos = [p for p in positives(c.merchant, c.pg) if p["id"] != "verified"]
    cands = [{"id": w["id"], "text": w["text"]} for w in ws[:8]]
    follow = {"type": "positives_strategies", "weak_types": [], "weak_texts": [], "positives": [p["text"] for p in pos[:3]],
              "noun": noun(c.slug)}
    src = L(c.lang, *SRC_PEERS)

    def render(ai):
        ids = [w["id"] for w in ws]
        picks = [i for i in (ai or {}).get("picks", []) if i in ids][:2] if ai else []
        chosen = [w for w in ws if w["id"] in picks] or ws[:2]
        follow["weak_types"] = [w["type"] for w in chosen]
        follow["weak_texts"] = [w["text"] for w in chosen]
        parts = [head]
        if pos:
            parts.append(L(c.lang, f"Where you're already ahead of {c.pg.label} ({src}):", f"Jahan aap {c.pg.label} se aage ho ({src}):"))
            parts.append(bullets(p["text"] for p in pos[:2]))
        if chosen:
            parts.append(L(c.lang, "Where you can win more:", "Jahan aur jeet sakte ho:") if pos else
                         L(c.lang, f"Where you can win more (you vs {c.pg.label}, {src}):", f"Jahan aur jeet sakte ho (aap vs {c.pg.label}, {src}):"))
            parts.append(bullets(w["text"] for w in chosen))
        if extra_after:
            parts.append(extra_after)
        if chosen:
            parts.append(yes(c.lang, f"share the 3 ways {noun(c.slug)} most often close these gaps",
                             f"{noun(c.slug).capitalize()} yeh gaps kaise close karte hain — top 3 tareeke bhej doon?"))
        else:
            parts.append(yes(c.lang, "show how to keep that lead", "Yeh lead kaise bani rahe, dikha doon?"))
        return "\n".join(p for p in parts if p)

    f = c.base_facts()
    f["weak_spot_candidates"] = [w["text"] for w in ws[:8]]
    f["peer_label"] = c.pg.label
    if not ws and not pos:
        return Draft("", skip=True, rationale=rationale + " — but no strengths or gaps in the data, so nothing honest to add.")
    task = AITask("picks_line", "Pick the 1-2 gaps that matter most for this merchant right now, given the trigger. Return picks only; line may be empty.",
                  candidates=cands, max_picks=2, priority=2) if len(cands) > 2 else None
    return Draft(render(None), facts=f, offer="positives + strategies", rationale=rationale, ai=task, render=render, followup=follow,
                 template_params=[c.sal, c.mname])


# =============================================================== merchant-facing handlers
def h_research_digest(c: Ctx) -> Draft:
    items = [d for d in c.category.get("digest") or [] if d.get("kind") in ("research", "trend", "tech")]
    item = c.digest(c.payload.get("top_item_id"))
    empty = item is None
    fresh_ok = set()
    if empty:
        words = (" ".join(c.offers) + " " + c.history_text() + " " + " ".join(t.get("theme", "") for t in c.merchant.get("review_themes") or [])).lower()
        rel = {d["id"]: sum(1 for w in re.findall(r"[a-z]{5,}", (d.get("title", "") + d.get("summary", "")).lower()) if w in words) for d in items}
        scored = sorted(items, key=lambda d: -rel[d["id"]])
        item = scored[0] if scored else None
        # newest-relevant with a floor: a newly pushed item wins only if it is clearly relevant (>=1 match and at least
        # half as relevant as the best item). Otherwise keep the current (most relevant) pick.
        best = rel[item["id"]] if item else 0
        fresh_ok = {d["id"] for d in items if d.get("_new") and rel[d["id"]] >= 1 and rel[d["id"]] * 2 >= best}
        if fresh_ok:
            item = max((d for d in items if d["id"] in fresh_ok), key=lambda d: rel[d["id"]])
    if not item:
        return h_generic(c)
    aud = {"dentists": "patient", "gyms": "member", "salons": "client"}.get(c.slug, "customer")

    def build(it, line):
        head = L(c.lang, f"{c.sal}, {it.get('source')} — worth 2 minutes:", f"{c.sal}, {it.get('source')} — 2 minute ka item:")
        facts = [it.get("title")]
        if it.get("trial_n"):
            facts.append(f"{num(it['trial_n'])}-patient trial")
        facts.append(first_sentence(clean_dates(it.get("summary", ""))))
        rel = line
        if not rel:
            if it.get("patient_segment", "").startswith("high_risk") and c.agg.get("high_risk_adult_count"):
                rel = L(c.lang, f"Directly relevant to your {c.agg['high_risk_adult_count']} high-risk adult patients.",
                        f"Aapke {c.agg['high_risk_adult_count']} high-risk adult patients pe seedha lagta hai.")
            elif it.get("actionable"):
                rel = it["actionable"].rstrip(".") + "."
        cta = yes(c.lang, f"pull the abstract and draft a {aud}-education WhatsApp you can forward",
                  f"Abstract + ek {aud}-education WhatsApp draft bhej doon jo aap forward kar sakein?")
        return "\n".join([head, bullets(facts), rel, cta])

    by_id = {d["id"]: d for d in items}

    def render(ai):
        it = item
        if empty and ai and ai.get("picks") and ai["picks"][0] in by_id:
            pick = ai["picks"][0]
            if not fresh_ok or pick in fresh_ok:   # floor: with a qualifying new item, the AI may only choose among those
                it = by_id[pick]
        return build(it, (ai or {}).get("line", "").strip())

    f = c.base_facts(); f["digest_items"] = items if empty else [item]
    task = AITask("picks_line" if empty else "line",
                  ("Pick the ONE digest item most relevant to this merchant, and write one sentence on why it matters to THEM "
                   if empty else "Write one sentence on why this item matters to THIS merchant ")
                  + "(their patients/customers, offers, past requests). Never overstate the study (no 'proves', 'cures', 'guaranteed').",
                  candidates=[{"id": d["id"], "text": d["title"]} for d in items if not fresh_ok or d["id"] in fresh_ok] if empty else [],
                  max_picks=1, priority=2)
    return Draft(render(None), facts=f, offer=f"send the abstract + a {aud}-education WhatsApp", ai=task, render=render,
                 rationale=f"Research digest ({item.get('source')}): exact study facts from code; AI only explains relevance.",
                 followup={"type": "abstract", "item_id": item["id"]}, template_params=[c.sal, item.get("title"), item.get("source")])


def h_regulation_change(c: Ctx) -> Draft:
    item = c.digest(c.payload.get("top_item_id")) or c.digest(kinds=["compliance"])
    if not item:
        return h_generic(c)
    deadline = c.payload.get("deadline_iso")
    lines = [L(c.lang, f"{c.sal}, compliance update — {clean_dates(item.get('source'))}:",
               f"{c.sal}, compliance update — {clean_dates(item.get('source'))}:"),
             bullets([s.rstrip(".") for s in sentences(clean_dates(item.get("summary", "")))] +
                     ([(f"In force since {nice_date(deadline, True)}" if (c.days_until(deadline, 1) or 0) < 0 else f"Effective: {nice_date(deadline, True)}")] if deadline else [])),
             yes(c.lang, "send a 1-page audit checklist your assistant can run this week",
                 "Ek 1-page audit checklist bhej doon jo aapka assistant is hafte check kar le?")]
    f = c.base_facts(); f["digest_item"] = item
    return Draft("\n".join(lines), facts=f, offer="send a 1-page compliance audit checklist",
                 rationale="Regulation change: exact rule + deadline as bullets, no AI paraphrase (legal content).",
                 followup={"type": "checklist", "item_id": item["id"]}, template_params=[c.sal, item.get("title")])


def h_cde_opportunity(c: Ctx) -> Draft:
    item = c.digest(c.payload.get("digest_item_id")) or c.digest(kinds=["cde"])
    if not item:
        return h_generic(c)
    when = item.get("date")
    if when and (c.days_until(when, 0) or 0) < 0:
        return Draft("", skip=True, rationale="CDE event date has already passed on the judge's clock — skipped.")
    when_s = nice_date(when, with_weekday=True) + (f", {nice_time(when)}" if nice_time(when) else "")
    credits = c.payload.get("credits") or item.get("credits")
    body = "\n".join([
        L(c.lang, f"{c.sal}, a CDE session you may want:", f"{c.sal}, ek CDE session aapke liye:"),
        bullets([f"\"{item.get('title')}\"", f"When: {when_s}", f"{credits} CDE credits" if credits else None,
                 f"Fee: {(item.get('actionable') or '').rstrip('.')}" if item.get("actionable") else None]
                + [x.rstrip(".") for x in sentences(item.get("summary", ""))[:2]]),
        yes(c.lang, "send the registration details and a reminder on the day", "Registration details + us din ka reminder bhej doon?")])
    f = c.base_facts(); f["digest_item"] = item
    return Draft(body, facts=f, offer="send CDE registration details + reminder",
                 rationale="CDE opportunity (filler, urgency 1): exact event facts as bullets.",
                 followup={"type": "registration", "item_id": item["id"]}, template_params=[c.sal, item.get("title"), when_s])


SEVERITY = [("🔴", "HIGH", r"contaminat|toxic|death|serious adverse|carcinogen|life[- ]threat|nitrosamine"),
            ("🟢", "LOW", r"label|packag|cosmetic|printing"),
            ("🟠", "MEDIUM", r".")]
MED_LABELS = ["life-critical", "chronic-important", "routine"]
MED_FALLBACK = {"insulin": "life-critical", "warfarin": "life-critical", "atorvastatin": "chronic-important",
                "metformin": "chronic-important", "telmisartan": "chronic-important", "amlodipine": "chronic-important",
                "levothyroxine": "chronic-important", "pantoprazole": "routine", "paracetamol": "routine", "cetirizine": "routine"}


def h_supply_alert(c: Ctx) -> Draft:
    p = c.payload
    dig = c.digest(p.get("alert_id")) or c.digest(kinds=["alert"])
    mol = p.get("molecule") or "the affected medicine"
    reason = re.sub(r" ?\([^)]*\)", "", first_sentence(dig.get("summary", ""))) if dig else ""
    dot, level = next((d, lv) for d, lv, rx in SEVERITY if re.search(rx, (dig or {}).get("summary", "").lower()))
    replacement = bool(dig and "replacement" in dig.get("summary", "").lower())
    asked = "list" in c.last_merchant_body().lower()

    def render(ai):
        label = (ai or {}).get("label") if (ai or {}).get("label") in MED_LABELS else MED_FALLBACK.get(str(mol).lower())
        what = [f"Medicine: {mol}" + (f" — {label}" if label else ""),
                f"Batches: {', '.join(p.get('affected_batches') or [])}" + (f" ({p['manufacturer']})" if p.get("manufacturer") else ""),
                f"Reason: {reason.rstrip('.')}" if reason else None,
                f"Source: {dig.get('source')}" if dig else None]
        do = ["Pull both batches from the shelf" if len(p.get("affected_batches") or []) == 2 else "Pull the affected batches from the shelf",
              "Return via distributor — replacement is available" if replacement else "Return via your distributor",
              "Inform affected customers — draft below"]
        cust = (f"\"Namaste, {c.mname} here. One batch of your {mol} is under a voluntary recall. "
                f"Please bring or send the strip — we'll replace it at no cost.\"")
        exposure = [
            "Shelf stock: recoverable via distributor replacement" if replacement else "Shelf stock: check return terms with your distributor",
            "Share strips sold from these batches, strips on shelf, cost price and GST rate — I'll calculate refunds to reverse, GST impact and any non-recoverable costs (delivery/handling)"]
        intro = (L(c.lang, f"{c.sal}, here's the {mol} action plan you asked for:", f"{c.sal}, yeh raha {mol} ka action plan jo aapne maanga tha:")
                 if asked else L(c.lang, f"{c.sal}, action needed on a {mol} recall:", f"{c.sal}, {mol} recall pe action chahiye:"))
        return "\n".join([f"{dot} *Recall alert — {level}*", intro, "", "*What's recalled*", bullets(what), "",
                          "*Do now*", bullets(do), "", cust, "", "*Your exposure*", bullets(exposure), "",
                          L(c.lang, f"Send this to your {mol} customers? Reply YES.", f"Yeh message aapke {mol} customers ko bhej doon? Reply YES.")])

    f = c.base_facts(); f["digest_item"] = dig
    task = AITask("label", f"Classify how serious it is for a patient to be without '{mol}' (not the recall itself).",
                  options=MED_LABELS, priority=1)
    return Draft(render(None), facts=f, offer=f"send the recall message to {mol} customers", ai=task, render=render,
                 rationale="Supply recall (urgency 5): action mode — merchant already asked; exact batch facts, severity tag, exposure calculator offer.",
                 followup={"type": "recall_send", "molecule": mol}, template_params=[c.sal, mol])


def h_category_seasonal(c: Ctx) -> Draft:
    p = c.payload
    trends = []
    for t in p.get("trends") or []:
        m = re.match(r"(.+?)_demand_([+-]\d+)", t)
        if m:
            nm = m.group(1).replace('_', '/')
            trends.append(f"{nm[0].upper() + nm[1:]}: {m.group(2)}%")
    dig = c.digest(kinds=["seasonal"])
    reach = []
    if c.agg.get("total_unique_ytd"):
        reach.append(f"{num(c.agg['total_unique_ytd'])} customers this year" + (f", {pct(c.agg['repeat_customer_pct'])} repeat" if c.agg.get("repeat_customer_pct") else ""))
    body = "\n".join([
        L(c.lang, f"{c.sal}, the {humanize(p.get('season', 'seasonal'))} demand shift is here:", f"{c.sal}, {humanize(p.get('season', 'seasonal'))} demand shift shuru:"),
        bullets(trends),
        f"Shelf: {dig['actionable'].rstrip('.')}." if dig and dig.get("actionable") else "",
        L(c.lang, f"Your reach: {reach[0]}." if reach else "", f"Aapki reach: {reach[0]}." if reach else ""),
        yes(c.lang, "draft a 'summer essentials' Google post + a WhatsApp for your repeat customers",
            "Ek 'summer essentials' Google post + repeat customers ke liye WhatsApp draft kar doon?")])
    f = c.base_facts(); f["trends"] = trends; f["digest_item"] = dig
    task = AITask("full", "Rewrite into the best WhatsApp message for THIS shop: keep the trend numbers as bullets, tie them to the shop's own "
                  "offers/customers, suggest shelf/stock actions, end with one YES CTA. Only numbers from the facts.", priority=3)
    return Draft(body, facts=f, offer="draft a seasonal-essentials Google post + customer WhatsApp", ai=task,
                 rationale="Category seasonal shift: AI-first (fact-checked) tailoring; bullets fallback.",
                 followup={"type": "seasonal_post"}, template_params=[c.sal, ", ".join(trends)])


def h_competitor_opened(c: Ctx) -> Draft:
    p = c.payload
    if not c.placeholder and p.get("competitor_name"):
        facts = [f"{p['competitor_name']} opened {p.get('distance_km')} km away" + (f" on {nice_date(p['opened_date'])}" if p.get("opened_date") else "")]
        if p.get("their_offer"):
            own = next((o for o in c.offers if _price(o) and _price(p["their_offer"]) and o.split("@")[0].strip().lower() == p["their_offer"].split("@")[0].strip().lower()), None)
            facts.append(f"Their offer: {p['their_offer']}" + (f" (yours: {inr(_price(own))})" if own else ""))
        head = f"{c.sal}, heads-up — a new competitor nearby:\n" + bullets(facts) + "\n" + \
            L(c.lang, "Here's how you stack up:", "Aap kahan khade ho:")
    else:
        return _competitor_compare(c)
    d = weak_spot_draft(c, head, "Competitor opened: exact competitor facts (never invented) + code-computed weak spots vs peers; closing offer = positives + strategies.")
    d.facts["competitor"] = p
    return d


def _competitor_compare(c: Ctx) -> Draft:
    """Competitor opened, no details: stay on the competitor topic — what nearby customers comparing the two will see."""
    one = SINGULAR.get(c.slug, "business")
    pos = c.pos_theme()
    see = []
    if pos:
        see.append(f"Your Google reviews: {pos['occurrences_30d']} this month praise your {humanize(pos['theme'])}" +
                   (f" (\"{pos['common_quote']}\")" if pos.get("common_quote") else ""))
    v, avg = c.perf.get("views"), c.pg.avg("views")
    if v and avg:
        see.append(f"Your visibility: {num(v)} profile views vs {num(avg)} avg for {c.pg.label} ({SRC_PEERS[0]})")
    see.append(f"Your live offer: {c.offers[0]}" if c.offers else "Your live offer: none on Google yet")
    gap_fix = (L(c.lang, f"Before they compare, add one offer — e.g. \"{c.catalog_offer()}\".", f"Compare karne se pehle ek offer daal dijiye — jaise \"{c.catalog_offer()}\".")
               if not c.offers and c.catalog_offer() else "")
    head = L(c.lang, f"{c.sal}, a new {one} has opened near {c.locality}. Customers nearby will compare the two — here's what they'll see for {c.mname}:",
             f"{c.sal}, {c.locality} ke paas naya {one} khula hai. Aas-paas ke customers dono ko compare karenge — {c.mname} ke baare mein unhe yeh dikhega:")
    cta = (yes(c.lang, "put that offer live", "Woh offer live kar doon?") if gap_fix else
           yes(c.lang, f"share 3 ways {noun(c.slug)} keep regulars when a new place opens", f"Naya place khulne pe {noun(c.slug)} regulars kaise rokte hain — 3 tareeke bhej doon?"))
    body = "\n".join(x for x in [head, bullets(see), gap_fix, cta] if x)
    return Draft(body, facts=c.base_facts(), offer="competitive edge", followup={"type": "offer_live", "offer": c.catalog_offer()} if gap_fix else
                 {"type": "positives_strategies", "weak_types": [], "weak_texts": [], "positives": see[:2], "noun": noun(c.slug)},
                 rationale="Competitor opened (no details): stays on the competitor topic — what customers comparing will see, sourced; one action.",
                 template_params=[c.sal])


def h_review_theme(c: Ctx) -> Draft:
    p = c.payload
    if not c.placeholder and p.get("theme"):
        th = p
        facts = [f"{humanize(th['theme']).capitalize()}: {th.get('occurrences_30d')} reviews in 30 days" + (" — and rising" if th.get("trend") == "rising" else ""),
                 f"Common quote: \"{th['common_quote']}\"" if th.get("common_quote") else None]
        pos = c.pos_theme()
        if pos and pos.get("theme") != th["theme"]:
            facts.append(f"Still strong: {pos['occurrences_30d']} reviews praise your {humanize(pos['theme'])}")
        body = "\n".join([L(c.lang, f"{c.sal}, a review pattern worth fixing:", f"{c.sal}, reviews mein ek pattern jo fix karna chahiye:"),
                          bullets(facts),
                          yes(c.lang, f"show you what's working in your favour, plus the strategies {noun(c.slug)} commonly use to fix this",
                              f"Kya main dikha doon kya aapke favour mein kaam kar raha hai, aur {noun(c.slug)} ise fix karne ke common tareeke?")])
        pg = c.pg
        f = c.base_facts(); f["theme"] = th
        return Draft(body, facts=f, offer="positives + strategies", rationale="Review theme: exact count/quote/trend from code; consultative close.",
                     followup={"type": "positives_strategies", "weak_types": [f"theme_{th['theme']}"], "weak_texts": [facts[0]],
                               "positives": [x["text"] for x in positives(c.merchant, pg)[:3]], "noun": noun(c.slug)},
                     template_params=[c.sal, humanize(th["theme"])])
    themes = sorted([t for t in c.merchant.get("review_themes") or [] if t.get("occurrences_30d")], key=lambda t: -int(t["occurrences_30d"]))
    neg = next((t for t in themes if t.get("sentiment") == "neg"), None)
    pos = next((t for t in themes if t.get("sentiment") == "pos"), None)
    if not neg and not pos:
        return Draft("", skip=True, rationale="Review-theme trigger, but no review themes in the data — nothing honest to say about reviews (restraint).")
    q = lambda t: f" (\"{t['common_quote']}\")" if t.get("common_quote") else ""
    if neg:
        facts = [f"{humanize(neg['theme']).capitalize()}: {neg['occurrences_30d']} Google reviews this month{q(neg)}"]
        if pos:
            facts.append(f"Still strong: {pos['occurrences_30d']} praise your {humanize(pos['theme'])}{q(pos)}")
        body = "\n".join([L(c.lang, f"{c.sal}, a pattern just showed up in your recent Google reviews:", f"{c.sal}, aapke recent Google reviews mein ek pattern dikha:"),
                          bullets(facts),
                          yes(c.lang, f"share the 3 fixes {noun(c.slug)} most often use for this", f"{noun(c.slug).capitalize()} ise kaise fix karte hain — top 3 tareeke bhej doon?")])
        follow = {"type": "positives_strategies", "weak_types": [f"theme_{neg['theme']}"], "weak_texts": facts[:1],
                  "positives": [x["text"] for x in positives(c.merchant, c.pg)[:3]], "noun": noun(c.slug)}
    else:
        facts = [f"{pos['occurrences_30d']} Google reviews this month praise your {humanize(pos['theme'])}{q(pos)}"]
        body = "\n".join([L(c.lang, f"{c.sal}, your recent Google reviews have a clear favourite:", f"{c.sal}, aapke recent Google reviews ka ek clear favourite hai:"),
                          bullets(facts),
                          yes(c.lang, "turn this into a Google post that brings in new customers", "Isse ek Google post bana doon jo naye customers laaye?")])
        follow = {"type": "plan_post"}
    return Draft(body, facts=c.base_facts(), offer="review-theme fixes", followup=follow,
                 rationale="Review-theme trigger without details: the merchant's own review themes (count + quote), strengths kept visible.",
                 template_params=[c.sal])


def h_perf_dip(c: Ctx) -> Draft:
    p = c.payload
    if not c.placeholder and p.get("metric"):
        metric, d, base = p["metric"], float(p.get("delta_pct", 0)), p.get("vs_baseline")
    else:
        mv = _moves(c.delta, -1)
        if not mv:
            return _no_dip_check(c)
        (metric, d), base = mv[0], None
    head = L(c.lang, f"{c.sal}, your {_metric_label(metric)} dropped {pct(d)} this week" + (f" (usually ~{base})" if base else "") + " (magicpin dashboard).",
             f"{c.sal}, is hafte aapke {_metric_label(metric)} {pct(d)} gire" + (f" (usually ~{base})" if base else "") + " (magicpin dashboard).")
    fix = ""
    if not c.offers:
        sug = c.catalog_offer()
        fix = L(c.lang, f"Quick fix: no active offer on Google — e.g. \"{sug}\".",
                f"Quick fix: Google pe koi active offer nahi — jaise \"{sug}\".") if sug else ""
    elif c.ident.get("verified") is False:
        fix = L(c.lang, "Quickest fix: your Google profile is still unverified.", "Sabse jaldi fix: aapka Google profile abhi unverified hai.")
    excl = (f"drop_{metric}",)
    dr = weak_spot_draft(c, head, f"Performance dip ({metric} {pct(d, signed=True)}): weak spots vs peers + one quick fix; consultative close.",
                         extra_after=fix, exclude_types=excl + (("no_offer",) if not c.offers else ()))
    return dr


def _no_dip_check(c: Ctx) -> Draft:
    """Dip trigger, but nothing is down this week. Framed as a dip CHECK with a clear all-clear (no contradiction),
    then ONE forward-looking item. Decision: send this (not skip) — it scored 40 on unseen data."""
    ups = _moves(c.delta, +1)[:2]
    ws = [w for w in weak_spots(c.merchant, c.pg, c.lang) if not w["type"].startswith("drop_")]
    one = next((w for w in ws if w["type"] != "no_offer"), None)
    if not one and c.offers:
        return Draft("", skip=True, rationale="Dip trigger, nothing down, no gap to work on — nothing useful to add (restraint).")
    # Positive framing (decision after eval round 3: "nothing is down" + weekly % read as contradiction/fabrication).
    # Lead with the merchant's own 30-day totals (the numbers they see on their dashboard) and "holding steady".
    v, k = c.perf.get("views"), c.perf.get("calls")
    tot = ", ".join(x for x in [f"{num(v)} profile views" if v else "", f"{num(k)} calls" if k else ""] if x)
    head = L(c.lang, f"{c.sal}, quick health check on {c.mname}: your numbers are holding steady ✅" +
             (f" — {tot} in the last 30 days (magicpin dashboard)." if tot else "."),
             f"{c.sal}, {c.mname} ka quick health check: aapke numbers steady hain ✅" +
             (f" — pichhle 30 din mein {tot} (magicpin dashboard)." if tot else "."))
    head += L(c.lang, " A good moment to grow from here:", " Yahan se aur badhne ka achha mauka hai:")
    if not c.offers and c.catalog_offer():
        nxt = L(c.lang, f"• Put one live offer on your Google profile, e.g. \"{c.catalog_offer()}\"",
                f"• Google profile pe ek live offer daaliye, jaise \"{c.catalog_offer()}\"")
        cta = yes(c.lang, "put it live for you", "Main live kar doon?")
        follow = {"type": "offer_live", "offer": c.catalog_offer()}
    elif one:
        nxt = L(c.lang, f"• One place to win more ({SRC_PEERS[0]}): {one['text']}", f"• Jahan aur jeet sakte ho ({SRC_PEERS[1]}): {one['text']}")
        cta = yes(c.lang, "share the quickest fix for that", "Iska sabse jaldi fix bhej doon?")
        follow = {"type": "positives_strategies", "weak_types": [one["type"]], "weak_texts": [one["text"]], "positives": [], "noun": noun(c.slug)}
    else:
        nxt, cta, follow = "", yes(c.lang, "keep watching and flag the first real dip", "Main nazar rakhun aur pehla asli dip aate hi bataun?"), {"type": "reminder_set"}
    return Draft("\n".join(x for x in [head, nxt, cta] if x), facts=c.base_facts(), offer="stay-ahead step", followup=follow,
                 rationale="Dip trigger, but nothing is down: positive health check with the merchant's own 30-day totals + one growth step.",
                 template_params=[c.sal])


def h_perf_spike(c: Ctx) -> Draft:
    p = c.payload
    if not c.placeholder and p.get("metric"):
        metric, d, base, driver = p["metric"], float(p.get("delta_pct", 0)), p.get("vs_baseline"), p.get("likely_driver")
    else:
        mv = _moves(c.delta, +1)
        if not mv:
            return Draft("", skip=True, rationale="Empty perf_spike trigger with no rise in the data — nothing to report.")
        (metric, d), base, driver = mv[0], None, None
    f = c.base_facts()
    if d >= 0.10:
        lead = L(c.lang, f"{c.sal}, great week — {_metric_label(metric)} up {pct(d)}" + (f" (baseline ~{base})" if base else "") + " 🎉 (magicpin dashboard)" +
                 (f", likely from your {humanize(driver)}" if driver else "") + ". One way to keep the momentum:",
                 f"{c.sal}, badhiya hafta — {_metric_label(metric)} {pct(d)} upar" + (f" (aam taur pe ~{base})" if base else "") + " 🎉 (magicpin dashboard)" +
                 (f", shayad aapke {humanize(driver)} ki wajah se" if driver else "") + ". Momentum banaye rakhne ka ek tareeka:")
        if c.offers:
            tips = [f"Mention \"{c.offers[0]}\" to every new enquiry — reply the same day"]
            cta = yes(c.lang, "draft a 2-line reply you can send to every new enquiry", "Har nayi enquiry ke liye 2-line reply draft kar doon?")
            follow = {"type": "enquiry_reply"}
        else:
            sug = c.catalog_offer(kinds=("free_service", "service_at_price"))
            tips = [f"Give the new visitors a reason to book now — a live offer like \"{sug}\""]
            cta = yes(c.lang, "put it live on your profile", "Ise profile pe live kar doon?")
            follow = {"type": "offer_live", "offer": sug}
        body = "\n".join([lead, bullets(tips), cta])
        return Draft(body, facts=f, offer="enquiry reply draft", followup=follow,
                     rationale="Big spike (≥10%): celebrate + one way to keep the momentum (one story, no undercutting the good news).", template_params=[c.sal, pct(d)])
    # small spike: compare with same-category merchants in our own data
    pg = c.pg
    mkt = pg.avg_delta(metric)
    cands = []
    for m_, label in (("views", "Views"), ("calls", "Calls"), ("ctr", "CTR")):
        mine, avg = c.perf.get(m_), pg.avg(m_)
        if mine is not None and avg:
            g = (mine - avg) / avg
            if abs(g) >= 0.10:
                cands.append({"id": m_, "text": f"{label}: {pct(mine) if m_ == 'ctr' else num(mine)} vs {pct(avg) if m_ == 'ctr' else num(avg)} ({pct(g, signed=True)})", "g": g})
    ver_share = pg.share(lambda x: x.get("identity", {}).get("verified"))
    if c.ident.get("verified") is False and ver_share and ver_share >= 0.5:
        cands.append({"id": "verified", "text": f"Verified profile: {pct(ver_share)} of them are, you're not", "g": -1})
    cands.sort(key=lambda x: x["g"])
    if mkt is not None:
        lead = L(c.lang, f"{c.sal}, {_metric_label(metric)} up {pct(d)} this week (magicpin dashboard) — " + (f"same as {pg.label} ({pct(mkt, signed=True)}), so it's the market, not just you." if abs(d - mkt) < 0.05 else f"vs {pct(mkt, signed=True)} for {pg.label}."),
                 f"{c.sal}, is hafte {_metric_label(metric)} {pct(d)} upar (magicpin dashboard) — " + (f"{pg.label} jaisa hi ({pct(mkt, signed=True)}), yani market ka move hai." if abs(d - mkt) < 0.05 else f"{pg.label} ka {pct(mkt, signed=True)}."))
    else:
        lead = L(c.lang, f"{c.sal}, {_metric_label(metric)} up {pct(d)} this week — a small move.", f"{c.sal}, is hafte {_metric_label(metric)} {pct(d)} upar — chhota move.")

    def render(ai):
        ids = [x["id"] for x in cands]
        picks = [i for i in (ai or {}).get("picks", []) if i in ids][:2] or ids[:2]
        chosen = [x for x in cands if x["id"] in picks]
        parts = [lead]
        if chosen:
            parts += [L(c.lang, f"Where you differ (you vs their avg, {SRC_PEERS[0]}):", f"Aap kahan alag ho (aap vs unka avg, {SRC_PEERS[1]}):"), bullets(x["text"] for x in chosen)]
        parts.append(yes(c.lang, "break down what they do differently + what switching would cost",
                         "Woh kya alag karte hain + switch ka kharcha, bata doon?"))
        return "\n".join(parts)
    task = AITask("picks_line", "Pick the 1-3 benchmark differences most relevant to this merchant. Return picks only.",
                  candidates=[{"id": x["id"], "text": x["text"]} for x in cands], priority=1) if len(cands) > 1 else None
    return Draft(render(None), facts=f, offer="peer breakdown + cost of switching", ai=task, render=render,
                 followup={"type": "peer_breakdown", "differences": [x["text"] for x in cands]},
                 rationale="Small spike (<10%): data-backed comparison with same-category merchants (anonymous, ≥3) instead of celebrating a blip.",
                 template_params=[c.sal, pct(d)])


def h_seasonal_perf_dip(c: Ctx) -> Draft:
    p = c.payload
    metric, d = p.get("metric", "views"), float(p.get("delta_pct", 0) or 0)
    mkt = c.pg.avg_delta(metric)
    if mkt is not None and mkt >= 0:
        # peers are NOT down -> not seasonal -> treat as a real dip (#8)
        c.payload = {"metric": metric, "delta_pct": d, "window": p.get("window")}
        c.placeholder = False
        dr = h_perf_dip(c)
        dr.rationale = f"Seasonal dip claimed, but {c.pg.label} are not down ({pct(mkt, signed=True)}) — handled as a real dip. " + dr.rationale
        return dr
    beat = c.beat("Apr-Jun", "acquisition") or c.beat()
    dig = c.digest(kinds=["seasonal"])
    peer_line = (f" — {c.pg.label} are down {pct(mkt)} too ({SRC_PEERS[0]})" if mkt is not None else "")
    head = L(c.lang, f"{c.sal}, {_metric_label(metric)} down {pct(d)} this week — that's the season, not you{peer_line}."
             + (f" ({beat['month_range']}: {beat['note']}.)" if beat else ""),
             f"{c.sal}, is hafte {_metric_label(metric)} {pct(d)} neeche — yeh season hai, aap nahi{peer_line}."
             + (f" ({beat['month_range']}: {beat['note']}.)" if beat else ""))
    tip = (L(c.lang, f"One way to save during the slow weeks: {dig['actionable'].rstrip('.')}.", f"Slow hafton mein bachat ka ek tareeka: {dig['actionable'].rstrip('.')}.")
           if dig and dig.get("actionable") else
           L(c.lang, "One way to use the slow weeks: get a comeback offer ready for past customers, so it's live when demand returns.",
             "Slow hafton ka ek sahi use: purane customers ke liye comeback offer ready rakhiye, taaki demand lautte hi live ho."))
    cta = yes(c.lang, "set that up for you", "Main set up kar doon?")
    return Draft("\n".join([head, tip, cta]), facts=c.base_facts(), offer="slow-season step", followup={"type": "package"},
                 rationale="Seasonal dip confirmed by peers: reassurance only + one cost-saving step (one story — no gap list).", template_params=[c.sal])


def h_milestone(c: Ctx) -> Draft:
    p = c.payload
    if c.placeholder or not p.get("metric"):
        # only a REAL, calculated milestone: the largest round number actually crossed in the merchant's data
        nice = [100, 250, 500, 750, 1000, 1500, 2000, 2500, 5000, 10000]
        found = None
        for val, label in ((c.agg.get("total_unique_ytd"), "customers this year (your magicpin customer data)"), (c.perf.get("views"), "profile views in 30 days (magicpin dashboard)")):
            if val:
                crossed = [n for n in nice if n <= val]
                if crossed and (found is None or crossed[-1] / val > found[0] / found[1]):
                    found = (crossed[-1], val, label)
        if not found:
            return Draft("", skip=True, rationale="Empty milestone trigger and no round milestone in the data — nothing to celebrate honestly.")
        mark, val, label = found
        facts = [f"{num(val)} {label} — past the {num(mark)} mark"]
        up = _moves(c.delta, +1)
        if up:
            facts.append(f"{_metric_label(up[0][0]).capitalize()} up {pct(up[0][1])} this week")
        body = "\n".join([L(c.lang, f"{c.sal}, {c.mname} just crossed a milestone:", f"{c.sal}, {c.mname} ne abhi ek milestone cross kiya:"), bullets(facts),
                          yes(c.lang, "draft a thank-you post + a review-request message for your regulars",
                              "Regulars ke liye thank-you post + review-request message draft kar doon?")])
        return Draft(body, facts=c.base_facts(), offer="thank-you post + review request", followup={"type": "review_request"},
                     rationale="Milestone trigger without details: a real, calculated round-number milestone from the merchant's own data.",
                     template_params=[c.sal, num(mark)])
    now, target = p.get("value_now"), p.get("milestone_value")
    label = _metric_label(p["metric"])
    facts = [f"You: {now} {label}" + (f" — {target - now} away from {target}" if p.get("is_imminent") and target else "")]
    if p["metric"] == "review_count" and c.bench.get("avg_review_count"):
        facts.append(f"Similar {noun(c.slug)}: {c.bench['avg_review_count']} avg")
    pos = c.pos_theme()
    if pos:
        facts.append(f"{pos['occurrences_30d']} reviews this month praise your {humanize(pos['theme'])}")
    body = "\n".join([L(c.lang, f"{c.sal}, {c.mname} is close to a milestone:", f"{c.sal}, {c.mname} ek milestone ke kareeb hai:"),
                      bullets(facts),
                      yes(c.lang, "draft a short review-request message for your regulars", "Regulars ke liye ek chhota review-request message draft kar doon?")])
    return Draft(body, facts=c.base_facts(), offer="review-request message", followup={"type": "review_request"},
                 rationale="Milestone: goal-gradient + position vs peers + real praise count.", template_params=[c.sal, str(target)])


def h_renewal_due(c: Ctx) -> Draft:
    p = c.payload
    sub = c.merchant.get("subscription") or {}
    if not c.placeholder and p.get("days_remaining") is not None:
        problems = [w for w in weak_spots(c.merchant, c.pg, c.lang) if w["type"] in ("low_calls", "drop_calls", "unverified", "no_offer", "low_views")][:3]
        lead = L(c.lang, f"{c.sal}, your {p.get('plan')} plan renews in {p['days_remaining']} days" + (f" ({inr(p['renewal_amount'])})." if p.get("renewal_amount") else "."),
                 f"{c.sal}, aapka {p.get('plan')} plan {p['days_remaining']} din mein renew hona hai" + (f" ({inr(p['renewal_amount'])})." if p.get("renewal_amount") else "."))
        if problems:
            fixable = [w for w in problems if w["type"] in ("unverified", "no_offer")]
            fx = " + ".join({"unverified": "verification", "no_offer": "a live offer"}[w["type"]] for w in fixable) or "your profile"
            body = "\n".join([lead, L(c.lang, "Straight talk — this month hasn't delivered:", "Seedhi baat — yeh mahina accha nahi gaya:"),
                              bullets(w["text"] for w in problems),
                              yes(c.lang, f"fix what's in our control first — {fx} — before you decide on renewal",
                                  f"Renewal decide karne se pehle, jo hamare control mein hai woh fix kar doon — {fx}?")])
            follow = {"type": "fix_first", "fixes": [w["type"] for w in fixable]}
        else:
            body = "\n".join([lead, L(c.lang, "What your profile delivered in the last 30 days:", "Pichhle 30 din mein profile ne kya diya:"),
                              bullets([f"{num(c.perf.get('views', 0))} views", f"{c.perf.get('calls', 0)} calls", f"{c.perf.get('directions', 0)} direction requests"]),
                              yes(c.lang, "send the renewal link", "Renewal link bhej doon?")])
            follow = {"type": "renewal_link"}
        return Draft(body, facts=c.base_facts(), offer="fix first, renewal later", followup=follow,
                     rationale="Renewal due with weak results: honest bullets + fix-first before asking for money.", template_params=[c.sal])
    if p.get("rotation"):
        return _case_study(c)  # #30 scheduled check-in rotation keeps the case-study format
    # empty -> stay on the renewal: what the plan delivered (sourced) + at most one data-backed step
    dl, plan = sub.get("days_remaining"), sub.get("plan")
    why = (L(c.lang, f"{c.sal}, your {plan} plan renews in {dl} days (magicpin subscription). What it delivered in the last 30 days (magicpin dashboard):",
             f"{c.sal}, aapka {plan} plan {dl} din mein renew hoga (magicpin subscription). Pichhle 30 din mein isne kya diya (magicpin dashboard):")
           if dl is not None and plan else L(c.lang, f"{c.sal}, ahead of your plan renewal — what it delivered in the last 30 days (magicpin dashboard):",
                                              f"{c.sal}, plan renewal se pehle — pichhle 30 din mein isne kya diya (magicpin dashboard):"))
    got = [f"{num(c.perf['views'])} profile views" if c.perf.get("views") else None, f"{c.perf['calls']} calls" if c.perf.get("calls") else None,
           f"{c.perf['directions']} direction requests" if c.perf.get("directions") else None]
    if not any(got):
        return h_generic(c)
    apply = [x for x in case_patterns(c.universe) if x["applies"](c.merchant)]
    step = ""
    if apply:
        x = apply[0]
        ex = c.catalog_offer() or "a service @ price"
        act = {"verified": ("verify your Google profile", "Google profile verify kar lijiye"),
               "has_offer": (f"put one offer live (e.g. \"{ex}\")", f"ek offer live kijiye (jaise \"{ex}\")")}[x["id"]]
        step = L(c.lang, f"To get more from it before renewal: {act[0]} — {x['text'][0].lower() + x['text'][1:]} (magicpin data).",
                 f"Renewal se pehle isse zyada nikalne ke liye: {act[1]} — {x['text'][0].lower() + x['text'][1:]} (magicpin data).")
    cta = yes(c.lang, "do that first step now", "Pehla step abhi kar doon?") if apply else yes(c.lang, "send the renewal link", "Renewal link bhej doon?")
    body = "\n".join(x for x in [why, bullets(got), step, cta] if x)
    return Draft(body, facts=c.base_facts(), offer="renewal value + one step", followup={"type": "fix_first", "fixes": [x["id"] for x in apply[:1]]} if apply else {"type": "renewal_link"},
                 rationale="Renewal (no details): stays on the renewal — what the plan delivered (sourced) + at most one data-backed step.",
                 template_params=[c.sal])


def _case_study(c: Ctx) -> Draft:
    """#12 data-backed case study (used by the #30 scheduled check-in rotation)."""
    pats = case_patterns(c.universe)
    apply = [x for x in pats if x["applies"](c.merchant)]
    if not pats:
        return h_generic(c)
    use = apply or pats[:1]
    steps = [{"verified": "Verify your Google profile (postcard or phone call)",
              "has_offer": f"Put one service+price offer live, e.g. \"{c.catalog_offer() or 'a service @ price'}\""}[x["id"]] for x in apply]
    body = "\n".join([L(c.lang, f"{c.sal}, a quick case study from magicpin merchants (magicpin data, last 30 days):",
                        f"{c.sal}, magicpin merchants se ek quick case study (magicpin data, pichhle 30 din):"),
                      bullets(x["text"] + (f" — {x['gap_text']}" if x in apply else "") for x in use),
                      (L(c.lang, "Steps:", "Steps:") + "\n" + bullets(steps)) if steps else L(c.lang, "You're already set up this way — keep it that way.", "Aap pehle se aise set ho — bas yeh bana rahe."),
                      yes(c.lang, "start with the first step", "Pehla step shuru kar doon?") if steps else yes(c.lang, "share one more idea for this month", "Is mahine ke liye ek aur idea bhej doon?")])
    return Draft(body, facts=c.base_facts(), offer="case-study steps", followup={"type": "fix_first", "fixes": [x["id"] for x in apply]},
                 rationale="Data-backed case study (patterns across ≥3 merchants per group, framed as tendencies).", template_params=[c.sal])


def h_winback(c: Ctx) -> Draft:
    p = c.payload
    days = p.get("days_since_expiry") or (c.merchant.get("subscription") or {}).get("days_since_expiry")
    n = p.get("lapsed_customers_added_since_expiry")
    facts = [f"Calls: down {pct(p['perf_dip_pct'])}" if p.get("perf_dip_pct") else None,
             f"Customers gone quiet: {n} more" + (f" ({c.agg['lapsed_90d_plus']} total not back in 90+ days)" if c.agg.get("lapsed_90d_plus") else "") if n else None]
    for key, label in (("retention_3mo_pct", "3-month retention"), ("retention_6mo_pct", "6-month retention")):
        if c.agg.get(key) is not None and c.bench.get(key) is not None:
            facts.append(f"{label}: {pct(c.agg[key])} vs {pct(c.bench[key])} avg for similar {noun(c.slug)}")
            break
    body = "\n".join([L(c.lang, f"{c.sal}, since your plan paused {days} days ago:", f"{c.sal}, {days} din pehle plan pause hone ke baad se:"),
                      bullets(facts),
                      L(c.lang, f"No pressure to renew today — let me first send a comeback message to those {n} customers for free, and you judge the result."
                        + " (Share your average bill size and I'll work out what they're worth to you each month.)",
                        f"Aaj renew ka koi pressure nahi — pehle main un {n} customers ko free mein ek comeback message bhejti hoon, result aap dekhiye."
                        + " (Apna average bill batayein, main calculate kar dungi ki woh har mahine kitne ke hain.)") if n else "",
                      L(c.lang, "Want me to? Reply YES.", "Bhej doon? Reply YES.")])
    return Draft(body, facts=c.base_facts(), offer="free comeback message to lapsed customers", followup={"type": "comeback", "n": n},
                 rationale="Win-back: honest since-expiry bullets, free value first, loss calculator from the merchant's own numbers.", template_params=[c.sal, str(days)])


def h_gbp_unverified(c: Ctx) -> Draft:
    p = c.payload
    views, avg = c.perf.get("views"), c.pg.avg("views")
    facts = []
    if views and avg:
        facts.append(f"Views: {num(views)}/month vs {num(avg)} avg for {c.pg.label}")
    if p.get("estimated_uplift_pct"):
        facts.append(f"Verifying: ~{pct(p['estimated_uplift_pct'])} more visibility (magicpin estimate)")
    if any(x["id"] == "verified" for x in case_patterns(c.universe)):
        facts.append("Verified merchants tend to get more views and calls")
    path = humanize(p.get("verification_path", "")) or "postcard or phone call"
    body = "\n".join([L(c.lang, f"{c.sal}, your Google profile is still unverified:", f"{c.sal}, aapka Google profile abhi unverified hai:"),
                      bullets(facts),
                      "Steps: Google Business Profile → \"Get verified\" → " + path + " → send me the code.",
                      yes(c.lang, "start now", "Abhi shuru karein?")])
    return Draft(body, facts=c.base_facts(), offer="start verification", followup={"type": "verification"},
                 rationale="Unverified GBP: official ~30% estimate + peer gap + bullet steps.", template_params=[c.sal, c.mname])


def h_dormant(c: Ctx) -> Draft:
    last_ts = next((h.get("ts") for h in reversed(c.history) if h.get("from") == "merchant"), None)
    days = c.days_since(last_ts, None) if last_ts else None
    if days is None:
        days = c.payload.get("days_since_last_merchant_message")  # the trigger's own count, stated at trigger time
    head = (L(c.lang, f"{c.sal}, it's been {days} days — a quick check on {c.mname}.", f"{c.sal}, {days} din ho gaye — {c.mname} pe ek quick check.")
            if days else L(c.lang, f"{c.sal}, it's been a while since we last spoke — a quick check on {c.mname}:",
                           f"{c.sal}, kaafi time se baat nahi hui — {c.mname} pe ek quick check:"))
    return weak_spot_draft(c, head, "Dormant merchant: re-open with something useful about their business (weak spots vs peers).")


def h_curious_ask(c: Ctx) -> Draft:
    tr = c.best_trend()
    pos = c.pos_theme()
    give = []
    if tr:
        give.append(f"'{tr['query']}' searches: {pct(tr['delta_yoy'], signed=True)} YoY")
    if pos:
        give.append(f"{pos['occurrences_30d']} reviews this month praise your {humanize(pos['theme'])}" + (f" (\"{pos['common_quote']}\")" if pos.get("common_quote") else ""))
    q = {"dentists": L(c.lang, "Which treatment did patients ask about most this week?", "Is hafte patients ne sabse zyada kis treatment ke baare mein poocha?"),
         "restaurants": L(c.lang, "Which dish moved fastest this week?", "Is hafte sabse zyada kaunsi dish chali?"),
         "pharmacies": L(c.lang, "What did customers ask for most at the counter this week?", "Is hafte counter pe sabse zyada kya maanga gaya?"),
         }.get(c.slug, L(c.lang, "Which service did customers ask about most this week?", "Is hafte customers ne sabse zyada kaunsi service poochi?"))
    if len(give) < 2 and own_numbers(c):
        give.append(own_numbers(c))
    body = "\n".join([L(c.lang, f"Hi {c.sal}! A quick one — and a freebie first:", f"Hi {c.sal}! Ek quick sawaal — pehle ek freebie:"),
                      bullets(give), q,
                      L(c.lang, "Reply with just the name and I'll send back a ready price-reply for WhatsApp.",
                        "Bas naam reply kariye, main WhatsApp ke liye ready price-reply bhej dungi.")])
    return Draft(body, "open_ended", facts=c.base_facts(), offer="ready WhatsApp price-reply", followup={"type": "price_reply"},
                 rationale="Curious ask: give first (trend + review fact), then one easy question (reciprocity + asking the merchant).",
                 template_params=[c.sal])


def h_festival(c: Ctx) -> Draft:
    p = c.payload
    if not c.placeholder and p.get("festival"):
        fest = p["festival"]
        du = c.days_until(p.get("date"), p.get("days_until"))
        if du is not None and du < 0:
            return Draft("", skip=True, rationale=f"{fest} ({nice_date(p.get('date'))}) has already passed on the judge's clock — skipped.")
        beat = c.beat("festival", "wedding", "Oct")
        stat = f"{beat['month_range']} is the {beat['note']}" if beat else ""
        if du is not None and du > 45:
            body = "\n".join([L(c.lang, f"{c.sal}, {fest} is on {nice_date(p.get('date'))} — {du} days out. Save the date:",
                                f"{c.sal}, {fest} {nice_date(p.get('date'))} ko hai — {du} din baaki. Date save kar lijiye:"),
                              bullets([f"For {c.slug}, {stat}" if stat else None]),
                              L(c.lang, "I'll check in closer to the date with a package plan.", "Date ke kareeb main package plan ke saath aaungi."),
                              yes(c.lang, "send you an early reminder 6 weeks before", "6 hafte pehle ek early reminder bhej doon?")])
            follow = {"type": "reminder_set"}
            why = "Festival far away (>45 days): short save-the-date + seasonal stat (restraint)."
        else:
            own = sorted(c.offers, key=lambda o: -(_price(o) or 0))
            body = "\n".join([L(c.lang, f"{c.sal}, {fest} is on {nice_date(p.get('date'))}" + (f" — {du} days away." if du is not None else "."),
                                f"{c.sal}, {fest} {nice_date(p.get('date'))} ko hai" + (f" — {du} din baaki." if du is not None else ".")),
                              bullets([f"For {c.slug}, {stat}" if stat else None, f"Build the package around: {', '.join(own[:2])}" if own else None]),
                              yes(c.lang, f"draft the {fest} package + a Google post", f"{fest} package + Google post draft kar doon?")])
            follow = {"type": "package"}
            why = "Festival within 45 days: package pitch built on the merchant's real offers."
        return Draft(body, facts=c.base_facts(), offer=follow["type"], followup=follow, rationale=why, template_params=[c.sal, fest])
    beat = c.beat("festival", "wedding") or c.beat()
    body = "\n".join([L(c.lang, f"{c.sal}, festival season is next on the calendar:", f"{c.sal}, festival season aa raha hai:"),
                      bullets([f"For {c.slug}, {beat['month_range']} is the {beat['note']}" if beat else None, own_numbers(c) or None,
                               "Prep: a comeback offer for past customers", f"Timing: have it live before {beat['month_range'].split('-')[0]}" if beat else None]),
                      yes(c.lang, "draft one", "Ek draft kar doon?")])
    return Draft(body, facts=c.base_facts(), offer="festival comeback offer", followup={"type": "package"},
                 rationale="Festival trigger without details: category festival window, no festival named.", template_params=[c.sal])


def h_ipl(c: Ctx) -> Draft:
    p = c.payload
    dig = c.digest(kinds=["seasonal"])
    tm = nice_time(p.get("match_time_iso"))
    weekend = p.get("is_weeknight") is False
    mdate = c.ist_date(p.get("match_time_iso"))
    today = c.now.astimezone(IST).date() if c.now else None
    if today and mdate and mdate < today:
        return Draft("", skip=True, rationale="IPL match date has already passed on the judge's clock — skipped.")
    when_txt = "tonight" if (today is None or mdate == today) else f"on {nice_date(p.get('match_time_iso'), with_weekday=True)}"
    dlv, dine = c.agg.get("delivery_orders_30d"), c.agg.get("dine_in_orders_30d")
    late = c.neg_theme("delivery_late")
    facts = []
    if dig and "12%" in dig.get("summary", ""):
        facts += ["Weekend IPL nights: -12% dine-in covers; weeknights +18%"]
    if dlv and dine:
        facts.append(f"Your orders (30d): {dlv} delivery vs {dine} dine-in")
    combo = c.catalog_offer(kinds=("service_at_price",), keywords=("match",)) or "a drinks-led match combo"
    plan = [("Delivery: match-night post" + (f" — quote realistic times ({late['occurrences_30d']} late-delivery reviews)" if late else "")),
            f"Dine-in: a drinks-led combo (e.g. \"{combo}\"), not food discounts — better margin, lighter kitchen"]
    if not weekend:
        plan = [f"Good night to push {c.offers[0]}" if c.offers else "Good night for a match-night offer"] + plan[1:]
    body = "\n".join([f"{c.sal}, {p.get('match')} {when_txt}, {tm}" + (" — a weekend match:" if weekend else ":"),
                      bullets(facts), L(c.lang, "Plan:", "Plan:"), bullets(plan),
                      yes(c.lang, "post both tonight", "Dono aaj raat post kar doon?")])
    return Draft(body, facts=c.base_facts(), offer="match-night delivery post + dine-in drinks offer", followup={"type": "ipl_posts"},
                 rationale="IPL: data-backed weekend/weeknight insight, delivery warning from reviews, drinks-led dine-in (margin + kitchen load).",
                 template_params=[c.sal, p.get("match", "IPL"), tm])


def _extract_program(text):
    out = {}
    for k, rx in (("weeks", r"(\d+)-week"), ("per_week", r"(\d+)\s*classes?/week"), ("ages", r"age\s*(\d+\s*-\s*\d+)"), ("price", r"(₹\s?[\d,]+)")):
        m = re.search(rx, text or "")
        out[k] = m.group(1).replace(" ", "") if m else None
    return out


def h_planning(c: Ctx) -> Draft:
    p = c.payload
    topic = humanize(p.get("intent_topic", "")) or "the plan"
    pos = c.pos_theme()
    f = c.base_facts()
    t = topic.lower()
    base_title = next((o for o in c.offers if _price(o)), None)
    base = _price(base_title) if base_title else None
    if base:
        f["suggested_price_options"] = [_round5(base * r) for r in (0.93, 0.87, 0.8)]
    if any(k in t for k in ("thali", "corporate", "bulk", "catering")):
        od = re.search(r"(\d+)\s*orders?/day", c.history_text())
        lines = [f"{c.sal}, here's a first cut of the {topic} — edit anything:"]
        if base:
            lines.append(f"• Base: your {base_title}" + (f" (already ~{od.group(1)} orders/day)" if od else ""))
            lines.append(f"• 10+ thalis/day: {inr(f['suggested_price_options'][0])} each · 25+: {inr(f['suggested_price_options'][1])} each (suggested)")
        lines += ["• Pre-order by 11am, delivered by lunch", "• Monthly office pass: 20 working days, billed weekly"]
        if c.agg.get("delivery_share_pct"):
            lines.append(f"You already do {pct(c.agg['delivery_share_pct'])} delivery, so ops are ready" + (f", and {pos['occurrences_30d']} reviews this month praise the {humanize(pos['theme']).replace(' quality', '')}." if pos else "."))
        lines.append(yes(c.lang, f"turn this into a Google post + a 3-line WhatsApp pitch for office admins in {c.locality}",
                         f"Isko Google post + {c.locality} ke office admins ke liye 3-line WhatsApp pitch bana doon?"))
        offer = "corporate package post + office pitch"
    elif any(k in t for k in ("program", "camp", "class", "yoga", "course", "package")):
        prog = _extract_program(c.last_vera_body())
        spec = [x for x in [f"{prog['weeks']} weeks" if prog.get("weeks") else None, f"{prog['per_week']} classes/week" if prog.get("per_week") else None,
                            f"Ages {prog['ages']}" if prog.get("ages") else None, prog.get("price")] if x]
        small = any(t2.get("theme") == "small_classes" for t2 in c.merchant.get("review_themes") or [])
        lines = [f"{c.sal}, here's the {topic} ready to publish — edit anything:",
                 f"*{topic.title()} at {c.mname}, {c.locality}*", bullets(spec + (["Small batches"] if small else []) + ["Book a trial class on WhatsApp"])]
        if pos:
            lines.append(f"Parents will check reviews — {pos['occurrences_30d']} this month already praise your {humanize(pos['theme'])}.")
        lines.append(yes(c.lang, "publish it on your Google profile today and make the Insta carousel", "Aaj Google profile pe publish karke Insta carousel bana doon?"))
        offer = "program post + carousel"
    else:
        lines = [f"{c.sal}, here's a starting outline for {topic}:",
                 bullets(["What's included + who it's for", f"Price anchored on your {base_title}" if base_title else "A clear service+price anchor",
                          "Launch: Google post + WhatsApp to your regulars"]), yes(c.lang, "draft the Google post first", "Pehle Google post draft kar doon?")]
        offer = "launch post"
    body = "\n".join(lines)
    task = AITask("full", "The merchant already said yes — design the plan they asked for (structure, inclusions, timing, pitch) as a ready-to-edit WhatsApp message "
                  "with bullets. Use ONLY facts given; any new price must come from suggested_price_options and be labelled 'suggested'. "
                  "No building/company/place names beyond the merchant's locality. End with ONE YES CTA. Never ask a qualifying question.", priority=3)
    return Draft(body, facts=f, offer=offer, ai=task, followup={"type": "plan_post", "topic": topic},
                 rationale="Planning intent: merchant already committed — deliver the plan (code anchors + AI design), no re-qualifying.",
                 template_params=[c.sal, topic])


HEAT_RULES = {
    "pharmacies": (["Move ORS / electrolytes / sunscreen to the counter", "Check stock of these before the weekend"],
                   "draft a 'heat essentials' WhatsApp for your repeat customers", "Repeat customers ke liye 'heat essentials' WhatsApp draft kar doon?"),
    "restaurants": (["Push cold drinks / shakes / lassi as add-ons", "Highlight delivery today"],
                    "post a 'beat the heat' delivery offer today", "Aaj ke liye 'beat the heat' delivery offer post kar doon?"),
    "gyms": (["Remind members to hydrate", "Nudge early-morning / evening sessions over afternoons"],
             "send your members a short hydration + timing note", "Members ko ek chhota hydration + timing note bhej doon?"),
    "salons": (["Promote summer hair & skin care services"],
               "post a summer hair-care offer", "Summer hair-care offer post kar doon?"),
    "dentists": (["Hot days mean more cold and sugary drinks and treats — especially for children",
                  "A short dental-care reminder builds trust with parents"],
                 "draft a Google post on dental care in hot weather", "Garmi mein dental care pe ek Google post draft kar doon?"),
}


def _event_city(p):
    return (p.get("city") or p.get("location") or p.get("affected_city") or "").strip()


def h_heatwave(c: Ctx) -> Draft:
    p = c.payload
    city = _event_city(p)
    if city and c.city and city.lower() != c.city.lower():
        return Draft("", skip=True, rationale=f"Heatwave in {city}; merchant is in {c.city} — not relevant.")
    temp = p.get("temp_c") or p.get("temperature_c") or p.get("max_temp_c") or p.get("temperature")
    if not temp and not city and not (p.get("alert") or p.get("forecast") or p.get("heat_index")):
        return Draft("", skip=True, rationale="Heatwave trigger without any weather data — skipped (R2: no evidence, restraint).")
    event =(f"it's {temp}°C" if temp else "a heatwave is on") + (f" in {city} today" if city else " today")
    actions, en_cta, hi_cta = HEAT_RULES.get(c.slug, (["Adjust today's plans for the heat"], "draft a heat-day post", "Garmi ke din ka post draft kar doon?"))
    extra = []
    if c.slug == "restaurants" and c.neg_theme("delivery_late"):
        extra.append(f"Quote realistic delivery times — {c.neg_theme('delivery_late')['occurrences_30d']} reviews this month mention late delivery")
    if c.slug == "salons" and c.offers:
        extra.append(f"Your offers: {', '.join(c.offers[:2])}")
    if c.slug == "pharmacies" and "Free Home Delivery" in " ".join(c.offers):
        extra.append("Mention your free home delivery")

    def render(ai):
        line = (ai or {}).get("line", "").strip()
        return "\n".join(x for x in [f"{c.sal}, {event} — quick heat-day plan:", line, bullets(actions + extra), yes(c.lang, en_cta, hi_cta)] if x)
    task = AITask("line", f"Write ONE short sentence linking today's heat to THIS {c.slug.rstrip('s')} using only their real data. No numbers.",
                  no_numbers=True, priority=2)
    return Draft(render(None), facts=c.base_facts(), offer=en_cta, ai=task, render=render, followup={"type": "heat_post"},
                 rationale="Heatwave: exact event + category rule (code), AI adds one merchant-specific line (no numbers).", template_params=[c.sal, event])


def h_local_news(c: Ctx) -> Draft:
    p = c.payload
    city = _event_city(p)
    if city and c.city and city.lower() != c.city.lower():
        return Draft("", skip=True, rationale=f"News for {city}; merchant is in {c.city} — skipped.")
    text = p.get("headline") or p.get("title") or p.get("summary") or p.get("event") or p.get("description")
    if not text:
        return Draft("", skip=True, rationale="Local news trigger without any news text — skipped.")

    def render(ai):
        if not ai or not ai.get("relevant") or not ai.get("line"):
            return ""
        return "\n".join([f"{c.sal}, local update: {text}.", ai["line"].strip(), yes(c.lang, "draft a quick update message for your customers",
                                                                                      "Customers ke liye ek quick update message draft kar doon?")])
    task = AITask("relevance", f"News: '{text}'. Decide if it genuinely matters to THIS {c.slug.rstrip('s')} today. If yes, write 1-2 practical sentences "
                  "using only their real data (no numbers of your own). If unsure, relevant=false.", no_numbers=True, priority=1)
    return Draft("", facts=c.base_facts(), offer="customer update message", ai=task, render=render, skip=True,
                 followup={"type": "news_update"},
                 rationale="Local news: AI judges relevance; skipped if not relevant or AI unavailable (restraint).", template_params=[c.sal])


def h_trend_movement(c: Ctx) -> Draft:
    p = c.payload
    q = p.get("query") or p.get("trend") or p.get("metric_or_topic")
    t = None
    if q:
        t = next((x for x in c.trends() if x.get("query", "").lower() == str(q).lower()), None) or \
            {"query": q, "delta_yoy": p.get("delta_yoy") or p.get("delta_pct"), "segment_age": p.get("segment_age")}
    else:
        t = c.best_trend()
    if not t or t.get("delta_yoy") is None:
        return Draft("", skip=True, rationale="Trend trigger without a trend we can state — skipped.")
    words = [w for w in re.findall(r"[a-z]{4,}", t["query"].lower()) if w not in ("near", "price", "delhi", "mumbai", "bangalore", "chennai", "pune")]
    hay = (" ".join(c.offers) + " " + c.history_text() + " " + " ".join(o.get("title", "") for o in c.category.get("offer_catalog") or [])).lower()
    if not any(w[:6] in hay for w in words):
        return Draft("", skip=True, rationale=f"Trend '{t['query']}' doesn't fit this merchant's offers or history — skipped (relevance filter).")
    related = next((o.get("title") for o in c.category.get("offer_catalog") or [] if any(w[:6] in o.get("title", "").lower() for w in words)), None)
    has_it = any(any(w[:6] in o.lower() for w in words) for o in c.offers)
    facts = [f"'{t['query']}' searches: {pct(t['delta_yoy'], signed=True)} YoY", f"Main age group: {t['segment_age']}" if t.get("segment_age") else None]

    def render(ai):
        line = (ai or {}).get("line", "").strip()
        if not line:
            line = (L(c.lang, "You already offer this — worth featuring it this week.", "Yeh aap already offer karte ho — is hafte feature karna chahiye.") if has_it else
                    L(c.lang, f"There's no offer for it on your profile yet — {noun(c.slug)} often list \"{related}\"." if related else "There's no offer for it on your profile yet.",
                      f"Aapke profile pe iska koi offer nahi hai — {noun(c.slug)} aksar \"{related}\" rakhte hain." if related else "Aapke profile pe iska koi offer nahi hai."))
        return "\n".join([L(c.lang, f"{c.sal}, trend alert:", f"{c.sal}, trend alert:"), bullets(facts), line,
                          yes(c.lang, "add it to your profile with a short post" if not has_it else "feature it in a post this week",
                              "Isse profile pe ek chhote post ke saath add kar doon?" if not has_it else "Is hafte ek post mein feature kar doon?")])
    task = AITask("line", "Write ONE sentence linking this trend to THIS merchant (their offers, past requests, missing offer). No numbers of your own.",
                  no_numbers=True, priority=2)
    return Draft(render(None), facts=c.base_facts(), offer="trend offer + post", ai=task, render=render, followup={"type": "trend_post", "offer": related},
                 rationale="Trend movement: exact trend numbers (code) + relevance filter + AI personal link.", template_params=[c.sal, t["query"]])


def h_generic(c: Ctx) -> Draft:
    head = L(c.lang, f"{c.sal}, a quick check on {c.mname}.", f"{c.sal}, {c.mname} pe ek quick check.")
    return weak_spot_draft(c, head, f"Trigger '{c.kind}' with limited payload — anchored on code-computed weak spots only.")


# =============================================================== customer-facing handlers
def _greet(c: Ctx, name):
    if c.clang == "hindi":
        return f"Namaste {name} ji" if name else "Namaste"
    return f"Hi {name}" if name else "Hi"


# =============================================================== trigger-to-business fit (decision, eval round 1)
# dissociation: 0 = native, 1 = close analogue (map to the nearest intent), 2+ = doesn't fit this business (skip, restraint)
# intent = (what the merchant is told, en / hi) ; cust = the customer-facing opener
NATIVE = {
    "recall_due": (("is due for a routine check-up", "ka routine check-up due hai"), "you're due for your next check-up"),
    "chronic_refill_due": (("is due for a medicine refill", "ka medicine refill due hai"), "your monthly refill may be due"),
    "trial_followup": (("is due a follow-up after their trial", "ko trial ke baad follow-up bhejna chahiye"), "hope you enjoyed your trial with us"),
    "wedding_package_followup": (("has a wedding coming up", "ki shaadi aane wali hai"), "your big day is coming up"),
    "appointment_tomorrow": (("has an appointment tomorrow", "ka kal appointment hai"), "see you tomorrow"),
    "customer_lapsed_soft": (("hasn't visited in a while", "kaafi time se nahi aaye"), "it's been a while!"),
    "customer_lapsed_hard": (("hasn't been back in a long time", "bahut time se wapas nahi aaye"), "it's been a while — we'd love to see you back"),
}
FIT = {  # (kind, category) -> (dissociation, mapped intent or None, mapped customer opener or None)
    ("recall_due", "salons"): (1, ("is due for their regular salon visit", "ka regular salon visit due hai"), "you're due for your regular visit"),
    ("recall_due", "gyms"): (1, ("is due for a fitness check-in", "ka fitness check-in due hai"), "time for a quick fitness check-in"),
    ("recall_due", "pharmacies"): (2, None, None), ("recall_due", "restaurants"): (3, None, None),
    ("chronic_refill_due", "gyms"): (2, None, None),  # was mapped to 'membership top-up': judged forced 3/3 runs -> skip
    ("chronic_refill_due", "salons"): (1, ("is due for their regular service", "ki regular service due hai"), "you're due for your regular service"),
    ("chronic_refill_due", "dentists"): (2, None, None), ("chronic_refill_due", "restaurants"): (3, None, None),
    ("trial_followup", "salons"): (1, ("is due a follow-up after their first visit", "ko pehli visit ke baad follow-up bhejna chahiye"), "hope you loved your first visit"),
    ("trial_followup", "dentists"): (1, ("is due a follow-up after their first consultation", "ko pehli consultation ke baad follow-up bhejna chahiye"), "hope your first consultation went well"),
    ("trial_followup", "restaurants"): (1, ("is due a follow-up after their first visit", "ko pehli visit ke baad follow-up bhejna chahiye"), "hope you enjoyed your first meal with us"),
    ("trial_followup", "pharmacies"): (2, None, None),
    ("wedding_package_followup", "gyms"): (1, ("has a wedding coming up — a wedding-prep plan fits", "ki shaadi aane wali hai — wedding-prep plan sahi rahega"), "your big day is coming up"),
    ("wedding_package_followup", "dentists"): (1, ("has a wedding coming up — a pre-wedding smile check fits", "ki shaadi aane wali hai — pre-wedding smile check sahi rahega"), "your big day is coming up"),
    ("wedding_package_followup", "restaurants"): (2, None, None), ("wedding_package_followup", "pharmacies"): (3, None, None),
    ("appointment_tomorrow", "restaurants"): (1, ("has a table booked for tomorrow", "ka kal table booked hai"), "see you tomorrow"),
    ("appointment_tomorrow", "pharmacies"): (2, None, None),
    ("supply_alert", "dentists"): (2, None, None), ("supply_alert", "salons"): (3, None, None), ("supply_alert", "gyms"): (3, None, None),
    ("supply_alert", "restaurants"): (3, None, None),
    ("cde_opportunity", "salons"): (2, None, None), ("cde_opportunity", "gyms"): (2, None, None),
    ("cde_opportunity", "restaurants"): (3, None, None), ("cde_opportunity", "pharmacies"): (2, None, None),
}


def fit(kind: str, slug: str):
    """-> (dissociation, (intent_en, intent_hi) | None, customer opener | None)"""
    if (kind, slug) in FIT:
        return FIT[(kind, slug)]
    nat = NATIVE.get(kind)
    return (0, nat[0], nat[1]) if nat else (0, None, None)


OPENER_HI = {
    "you're due for your next check-up": "aapka next check-up due hai", "your monthly refill may be due": "aapka monthly refill due ho sakta hai",
    "hope you enjoyed your trial with us": "umeed hai aapko trial accha laga", "your big day is coming up": "aapka bada din aa raha hai",
    "see you tomorrow": "kal milte hain", "it's been a while!": "kaafi time ho gaya!",
    "it's been a while — we'd love to see you back": "kaafi time ho gaya — aapko wapas dekhna accha lagega",
    "you're due for your regular visit": "aapki regular visit due hai", "time for a quick fitness check-in": "ek quick fitness check-in ka time",
    "your membership may be due for a top-up": "aapki membership top-up due ho sakti hai", "you're due for your regular service": "aapki regular service due hai",
    "hope you loved your first visit": "umeed hai pehli visit acchi lagi", "hope your first consultation went well": "umeed hai pehli consultation acchi rahi",
    "hope you enjoyed your first meal with us": "umeed hai pehla meal accha laga",
}


def sender(c: Ctx) -> str:
    """Who signs a customer-facing message: 'Dr. Asha from Asha Dental Care', or just the business name."""
    who = (c.sal or "").strip()
    last = who.split()[-1].lower() if who else ""
    if c.slug == "dentists" and who.startswith("Dr"):
        return f"{who}, {c.mname}" if last in c.mname.lower() else f"{who} from {c.mname}"
    return f"{who} from {c.mname}" if who and last not in c.mname.lower() else c.mname


def brand_line(c: Ctx) -> str:
    """A positive, business-centric line built only from real data (or the merchant's own tagline, if they gave one)."""
    own = (c.merchant.get("_tagline") or "").strip()
    if own:
        return f"✨ {own}"
    pos = c.pos_theme()
    year = (c.ident or {}).get("established_year")
    where = f", {c.locality}" if c.locality else ""
    since = f" — serving {c.locality or c.city} since {year}" if year and (c.locality or c.city) else ""
    if pos and pos.get("common_quote"):
        return f"💬 “{pos['common_quote']}” — {c.mname}{since or where}"
    if since:
        return f"⭐ {c.mname}{since}"
    return f"📍 {c.mname}{where}" if where else ""


def with_brand(c: Ctx, customer_body: str) -> str:
    """Insert the brand line just above the customer draft's final call-to-action line."""
    line = brand_line(c)
    if not line or line in customer_body:
        return customer_body
    lines = customer_body.split("\n")
    return "\n".join(lines[:-1] + [line, lines[-1]]) if len(lines) > 1 else customer_body + "\n" + line


def approval_ask(c: Ctx) -> str:
    return L(c.lang, "Send it? Reply YES — or send your own tagline / changes and I'll use them for your brand.",
             "Bhej doon? Reply YES — ya apni tagline / changes bhejiye, main aapke brand ke liye wahi use karungi.")


def ask_merchant(c: Ctx, what_en: str, what_hi: str) -> Draft:
    """Empty customer trigger: route to the merchant for approval instead of messaging the customer."""
    d = _approval_from_history(c)
    if d:
        return d
    return _plain_ask(c, what_en, what_hi)


def _approval_from_history(c: Ctx):
    """Customer profile is known but the trigger has no details: why-now from the trigger type (mapped to this business),
    the customer's real history with its source, and a ready draft for one-tap approval."""
    cu = c.customer or {}
    rel = cu.get("relationship") or {}
    name = customer_first(cu)
    if not (name and rel.get("last_visit")) or (cu.get("preferences") or {}).get("reminder_opt_in") is False:
        return None
    if c.kind == "trial_followup" and int(rel.get("visits_total") or 0) >= 3:
        return Draft("", skip=True, rationale=f"Trial follow-up, but the customer already has {rel.get('visits_total')} visits — the data contradicts a trial (restraint).")
    _, intent, opener = fit(c.kind, c.slug)
    intent = intent or ("is due a friendly check-in", "ko ek friendly check-in bhejna sahi rahega")
    ago = c.days_since(rel["last_visit"], None)
    ago_txt = f" — {ago} days ago" if ago and ago > 0 else ""
    facts = [f"Last visit: {nice_date(rel['last_visit'])}{ago_txt}",
             (f"{rel['visits_total']} visits" if rel.get("visits_total") else "") +
             (f", {inr(rel['lifetime_value'])} spent with you" if rel.get("lifetime_value") else "")]
    emoji = {"dentists": " 🦷", "gyms": " 💪", "salons": " ✨", "restaurants": " 🍽️", "pharmacies": ""}.get(c.slug, "")
    nxt = {"dentists": "check-up", "gyms": "session", "salons": "appointment", "restaurants": "table"}.get(c.slug, "visit")
    op = opener or "it's been a while!"
    op = OPENER_HI.get(op, op) if c.clang in ("hindi", "hinglish") else op
    here = L(c.clang, f"{sender(c)} here", f"{sender(c)} se")
    cust = "\n".join([f"{_greet(c, name)}, {here}{emoji} — {op}",
                      bullets([f"Last visit: {nice_date(rel['last_visit'])}" + (f" ({ago} days ago)" if ago and ago > 0 else ""),
                               f"Visits with us: {rel['visits_total']}" if rel.get("visits_total") else None]),
                      L(c.clang, f"Want us to hold a slot for your next {nxt}? Reply YES.", f"Aapke next {nxt} ke liye slot rakh dein? Reply YES.")])
    draft = with_brand(c, cust)
    quoted = "\n".join("> " + ln if ln.strip() else ">" for ln in draft.split("\n"))
    body = "\n".join([L(c.lang, f"{c.sal}, {name} {intent[0]} (your customer records):", f"{c.sal}, {name} {intent[1]} (aapke customer records):"),
                      bullets(facts), "", L(c.lang, "Here's what I'd send on your behalf:", "Aapki taraf se main yeh bhejungi:"), quoted, "",
                      approval_ask(c)])
    return Draft(body, "binary_yes_stop", "vera", c.base_facts() | {"customer_draft": draft}, offer=f"send {name} a message",
                 followup={"type": "send_customer_reminder", "who": name, "draft": draft, "brand": brand_line(c)},
                 rationale="Customer trigger without details: why-now from the trigger type (mapped to this business), sourced visit history, ready draft for one-tap approval.",
                 template_params=[c.sal, name])


def _plain_ask(c: Ctx, what_en: str, what_hi: str) -> Draft:
    who = customer_first(c.customer) or name_from_customer_id(c.trigger.get("customer_id")) or \
        str((c.payload or {}).get("customer_name") or "").split(" ")[0].strip() or L(c.lang, "one of your customers", "aapke ek customer")
    rel = (c.customer or {}).get("relationship", {})
    hist = []
    if rel.get("last_visit"):
        hist.append(f"last visit {nice_date(rel['last_visit'], True)}")
    if rel.get("visits_total"):
        hist.append(f"{rel['visits_total']} visits")
    facts = [] if c.placeholder else payload_facts(c)
    fb = ("\n" + bullets(facts) + "\n") if facts else " "
    body = L(c.lang, f"{c.sal}, {who}" + (f" ({', '.join(hist)})" if hist else "") + f" {what_en}." + fb + "Want me to send them a follow-up on your behalf? Reply YES.",
             f"{c.sal}, {who}" + (f" ({', '.join(hist)})" if hist else "") + f" {what_hi}." + fb + "Aapki taraf se follow-up bhej doon? Reply YES.")
    return Draft(body, facts=c.base_facts(), offer=f"send {who} a reminder", followup={"type": "send_customer_reminder", "who": who},
                 rationale="Customer trigger without details: ask the merchant first instead of guessing (respects consent, invents nothing).",
                 template_params=[c.sal, who])


def h_recall_due(c: Ctx) -> Draft:
    p = c.payload
    if c.placeholder or not p.get("available_slots"):
        return ask_merchant(c, "may be due for their next visit", "ka next visit due ho sakta hai")
    name = customer_first(c.customer)
    svc = humanize(p.get("service_due", "")).replace("6 month", "6-month")
    off = next((o for o in c.offers if any(k in o.lower() for k in ("clean", "check", "session"))), None)
    lv = nice_date(p.get("last_service_date")) if p.get("last_service_date") else None
    emoji = {"dentists": " 🦷", "gyms": " 💪", "salons": " ✨"}.get(c.slug, "")
    live = [s for s in p["available_slots"] if s.get("label") and (c.days_until(s.get("iso"), 0) or 0) >= 0]
    if not live:
        return ask_merchant(c, "is due for a visit, but the offered slots have passed", "ka visit due hai, par diye gaye slots nikal gaye")
    slots = [s.get("label") for s in live[:2]]
    head = f"{_greet(c, name)}, {sender(c)} here{emoji} " + L(c.clang, f"Your {svc} is due" + (f" (last: {lv})." if lv else "."),
                                                          f"Aapka {svc} due hai" + (f" (last: {lv})." if lv else "."))
    items = ([f"{off.split('@')[0].strip()}: {inr(_price(off))}"] if off and _price(off) else []) + [f"Slot {i + 1}: {s}" for i, s in enumerate(slots)]
    body = "\n".join([head, bullets(items), L(c.clang, "Reply 1 or 2 — or tell us a time that suits you.", "Reply 1 ya 2 — ya apna time batayein.")])
    return Draft(body, "multi_choice_slot", "merchant_on_behalf", c.base_facts() | {"customer": c.customer}, offer="book the recall slot",
                 followup={"type": "booking", "slots": slots}, rationale="Recall due: real slots + price as bullets in the customer's language; no AI.",
                 template_params=[name, c.mname, svc])


def h_appointment_tomorrow(c: Ctx) -> Draft:
    p = c.payload
    name = customer_first(c.customer)
    when = p.get("appointment_iso") or p.get("slot_iso")
    tm = nice_time(when) if when else ""
    items = [L(c.clang, f"When: tomorrow, {tm}" if tm else "When: tomorrow (at your booked time)", f"Kab: kal, {tm}" if tm else "Kab: kal (aapke booked time pe)"),
             L(c.clang, f"Where: {c.mname}" + (f", {c.locality}" if c.locality else ""), f"Kahan: {c.mname}" + (f", {c.locality}" if c.locality else ""))]
    if p.get("service"):
        items.insert(0, f"Service: {humanize(p['service'])}")
    body = "\n".join([f"{_greet(c, name)}, {sender(c)} " + L(c.clang, "here 👋 Quick reminder:", "se 👋 Ek quick reminder:"), bullets(items),
                      L(c.clang, "Reply YES to confirm, or send a better time and we'll move it.", "Confirm karne ke liye YES reply karein, ya naya time bhejein — hum shift kar denge.")])
    return Draft(body, "binary_yes_stop", "merchant_on_behalf", c.base_facts() | {"customer": c.customer}, offer="confirm appointment",
                 followup={"type": "confirm_appt"}, rationale="Appointment reminder: bullets, never an invented time.", template_params=[name, c.mname])


def h_chronic_refill(c: Ctx) -> Draft:
    p = c.payload
    if c.slug != "pharmacies" or not p.get("molecule_list"):
        return ask_merchant(c, "may be due for a follow-up", "ka follow-up due ho sakta hai")
    name = customer_first(c.customer)
    name_part = f"{name.replace('Mr. ', '').replace('Mrs. ', '')} ji" if re.match(r"(Mr|Mrs|Ms)\.", name or "") else name
    senior = next((o for o in c.offers if "senior" in o.lower()), None) if (c.customer or {}).get("identity", {}).get("senior_citizen") else None
    deliv = next((o for o in c.offers if "deliver" in o.lower()), None)
    runs = nice_date(p.get("stock_runs_out_iso"))
    past = (c.days_until(p.get("stock_runs_out_iso"), 0) or 0) < 0
    extras = [L(c.clang, "Delivery: to your saved address" if p.get("delivery_address_saved") else "Delivery: tell us where",
                "Delivery: saved address pe" if p.get("delivery_address_saved") else "Delivery: address bata dijiye"),
              senior, deliv]
    if c.clang in ("hindi", "hinglish"):
        head = (f"Namaste {name_part}, " if name_part else "Namaste! ") + f"{c.mname} se 🙏\n" + (f"Aapki monthly dawaiyan {runs} ko khatam ho gayi hongi:" if past else f"Aapki monthly dawaiyan {runs} tak khatam ho jayengi:")
        close = "Confirm karne ke liye YES reply karein."
    else:
        head = (f"Hi {name_part}, " if name_part else "Hi! ") + f"{sender(c)} here.\nYour monthly medicines " + (f"were due for a refill on {runs}:" if past else f"run out on {runs}:")
        close = "Reply YES to confirm."
    body = "\n".join([head, bullets(m.capitalize() for m in p["molecule_list"]), "", bullets(extras), close])
    return Draft(body, "binary_yes_stop", "merchant_on_behalf", c.base_facts() | {"customer": c.customer}, offer="deliver refill",
                 followup={"type": "refill"}, rationale="Chronic refill: bullets, real offers; recall handled quietly (only discussed if the customer asks).",
                 template_params=[name, c.mname, runs])


def h_trial_followup(c: Ctx) -> Draft:
    p = c.payload
    opts = [o.get("label") for o in p.get("next_session_options") or []
            if o.get("label") and (not o.get("iso") or (c.days_until(o.get("iso"), 0) or 0) >= 0)]
    if c.placeholder or not opts:
        if p.get("next_session_options"):
            return ask_merchant(c, "did a trial with you, but the next session we had offered has already passed",
                                "ne aapke yahan trial kiya tha, par jo next session offer kiya tha woh nikal gaya")
        return ask_merchant(c, "did a trial with you", "ne aapke yahan trial kiya tha")
    parent, kid = parent_name(c.customer), child_name(c.customer)
    name = parent or customer_first(c.customer)
    thanks = (f"Thanks for bringing {kid} for the trial on {nice_date(p.get('trial_date'))}!" if parent and kid
              else f"Thanks for joining the trial on {nice_date(p.get('trial_date'))}!")
    items = [f"Next session: {opts[0]}"] + ([f"To continue: {c.offers[0]}"] if c.offers else [])
    pos = next((t for t in c.merchant.get("review_themes") or [] if t.get("sentiment") == "pos" and t.get("theme") == "small_classes"), None) or c.pos_theme()
    proof = (f"Parents love our {humanize(pos['theme'])} — {pos['occurrences_30d']} reviews this month mention it." if pos and parent else
             f"{pos['occurrences_30d']} reviews this month mention our {humanize(pos['theme'])}." if pos else "")
    body = "\n".join([f"Hi {name}, {sender(c)} here 🧘 {thanks}" if c.slug == "gyms" else f"Hi {name}, {sender(c)} here. {thanks}", bullets(items), proof,
                      "Reply YES to book, or tell us a better time."])
    return Draft(body, "binary_yes_stop", "merchant_on_behalf", c.base_facts() | {"customer": c.customer}, offer="book next session",
                 followup={"type": "booking", "slots": opts}, rationale="Trial follow-up: bullets + real review proof, addressed to the parent.", template_params=[name, c.mname])


def h_customer_lapsed_soft(c: Ctx) -> Draft:
    name = customer_first(c.customer)
    rel = (c.customer or {}).get("relationship", {})
    ago = c.days_since(rel.get("last_visit"), None) if rel.get("last_visit") else None
    items = [f"Last visit: {nice_date(rel['last_visit'])}" + (f" ({ago} days ago, as per our records)" if ago and ago > 0 else " (as per our records)")
             if rel.get("last_visit") else None,
             f"Visits with us: {rel['visits_total']}" if rel.get("visits_total") else None]
    emoji = {"dentists": " 🦷", "gyms": " 💪", "salons": " ✨"}.get(c.slug, "")
    nxt = {"dentists": "check-up", "gyms": "session", "salons": "appointment"}.get(c.slug, "visit")
    head = f"{_greet(c, name)}, {sender(c)} " + L(c.clang, f"here{emoji} — it's been a while!", f"se{emoji} — kaafi time ho gaya!")
    if c.slug in ("pharmacies", "restaurants"):
        ask = L(c.clang, "Anything you need this week? Reply YES and we'll keep it ready.", "Is hafte kuch chahiye? YES reply karein, hum ready rakhenge.")
    else:
        ask = L(c.clang, f"Want us to hold a slot for your next {nxt}? Reply YES.", f"Aapke next {nxt} ke liye slot rakh dein? Reply YES.")
    body = "\n".join([head, bullets(items), ask])
    return Draft(body, "binary_yes_stop", "merchant_on_behalf", c.base_facts() | {"customer": c.customer}, offer="hold a slot",
                 followup={"type": "confirm_slot"}, rationale="Soft-lapsed customer: real visit history as bullets, no invented offer.", template_params=[name, c.mname])


def h_customer_lapsed_hard(c: Ctx) -> Draft:
    p = c.payload
    name = customer_first(c.customer)
    prefs = (c.customer or {}).get("preferences", {})
    focus = humanize(p.get("previous_focus") or prefs.get("training_focus") or "")
    months = p.get("previous_membership_months")
    lv = ((c.customer or {}).get("relationship") or {}).get("last_visit")
    days = c.days_since(lv, p.get("days_since_last_visit")) if lv else p.get("days_since_last_visit")
    slot = humanize(prefs.get("preferred_slots", ""))
    items = [f"Your {focus} plan" + (f" from your {months} months with us" if months else "") + " is still on file" if focus else None,
             f"{slot.capitalize()} slots are open for you" if slot else None]
    body = "\n".join([f"{_greet(c, name)}, {sender(c)} here 💪 " + (f"It's been {days} days — no pressure, just checking in." if days else "No pressure, just checking in."),
                      bullets(items), "Reply YES and we'll hold a slot for you this week."])
    return Draft(body, "binary_yes_stop", "merchant_on_behalf", c.base_facts() | {"customer": c.customer}, offer="hold a comeback slot",
                 followup={"type": "confirm_slot"}, rationale="Hard-lapsed ex-member: warm, personal, no new-customer offer.", template_params=[name, c.mname])


def h_wedding(c: Ctx) -> Draft:
    p = c.payload
    name = customer_first(c.customer)
    step = humanize(p.get("next_step_window_open", "")).replace("program 30day", "30-day program").replace("skin prep", "skin-prep")
    pref = humanize(((c.customer or {}).get("preferences") or {}).get("preferred_slots", ""))
    items = [f"Wedding: {nice_date(p.get('wedding_date'))}", f"Bridal trial: done ({nice_date(p.get('trial_completed'))}) ✅" if p.get("trial_completed") else None,
             f"Next: {step} — the window is open now" if step else None]
    dtw = c.days_until(p.get("wedding_date"), p.get("days_to_wedding"))
    if dtw is not None and dtw < 0:
        return Draft("", skip=True, rationale="Wedding date has already passed on the judge's clock — skipped.")
    body = "\n".join([f"Hi {name} 💍 {sender(c)} here — " + (f"{dtw} days to your wedding!" if dtw else "your wedding is coming up!"),
                      bullets(items), f"Want us to hold a {pref.title()} slot next week to plan it? Reply YES." if pref else "Want us to hold a slot next week to plan it? Reply YES."])
    return Draft(body, "binary_yes_stop", "merchant_on_behalf", c.base_facts() | {"customer": c.customer}, offer="hold bridal planning slot",
                 followup={"type": "confirm_slot"}, rationale="Bridal follow-up: countdown bullets, preferred day, no invented price.", template_params=[name, c.mname])


ASK_WHAT = {
    "customer_lapsed_hard": ("hasn't visited in a while", "kaafi time se nahi aaye"),
    "customer_lapsed_soft": ("hasn't visited recently", "haal mein nahi aaye"),
    "wedding_package_followup": ("has a wedding coming up", "ki shaadi aane wali hai"),
    "trial_followup": ("did a trial with you", "ne aapke yahan trial kiya tha"),
    "chronic_refill_due": ("may be due for a refill", "ka refill due ho sakta hai"),
    "appointment_tomorrow": ("has an appointment tomorrow", "ka kal appointment hai"),
}
SKIP_KEYS = {"placeholder", "rotation", "customer_id", "merchant_id", "id", "trigger_id"}


def _fmt_val(k, v):
    if isinstance(v, bool):
        return None
    if isinstance(v, str) and re.match(r"\d{4}-\d{2}-\d{2}", v):
        return nice_date(v)
    if isinstance(v, (int, float)):
        return num(v)
    if isinstance(v, str):
        return humanize(v)
    if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
        return ", ".join(humanize(x) for x in v[:4])
    return None


FACT_LABELS = {
    "days_since_last_visit": "Last visit: {} days ago", "previous_focus": "Goal: {}", "previous_membership_months": "Member for {} months",
    "trial_completed": "Bridal trial done: {}", "next_step_window_open": "Next step: {}", "molecule_list": "Medicines: {}",
    "last_refill": "Last refill: {}", "stock_runs_out_iso": "Stock runs out: {}", "service_due": "Due for: {}",
    "last_service_date": "Last visit: {}", "due_date": "Due by: {}", "trial_date": "Trial: {}",
}


def payload_facts(c: Ctx, limit: int = 3) -> list[str]:
    """Readable bullets straight from the trigger payload (no invented values). Day counts follow the judge's clock."""
    p, out = c.payload or {}, []
    if p.get("wedding_date"):
        dtw = c.days_until(p.get("wedding_date"), p.get("days_to_wedding"))
        out.append(f"Wedding: {nice_date(p['wedding_date'])}" + (f" ({dtw} days away)" if dtw and dtw > 0 else ""))
    for k, v in p.items():
        if k in SKIP_KEYS or k.endswith("_id") or k in ("wedding_date", "days_to_wedding", "customer_name"):
            continue
        val = _fmt_val(k, v)
        if not val:
            continue
        val = val.replace("program 30day", "30-day program").replace("skin prep", "skin-prep")
        out.append(FACT_LABELS[k].format(val) if k in FACT_LABELS else f"{humanize(re.sub(r'_iso$', '', k)).capitalize()}: {val}")
    return out[:limit]


RELATION_WORDS = {"grandfather", "grandmother", "father", "mother", "dad", "mom", "parent", "uncle", "aunt", "son", "daughter", "customer"}


def _merchant_approval(category, merchant, trigger, universe, now, c: Ctx):
    """Customer trigger but the customer's profile was never shared with us.
    Decision #20/#22/#23 = ask the merchant first. Instead of a vague ask, show the real trigger facts and the exact
    message Vera would send, so the merchant can approve with one YES."""
    cid = trigger.get("customer_id")
    who = name_from_customer_id(cid)
    if not who or c.placeholder or not payload_facts(c):
        return None
    relation = who.lower() in RELATION_WORDS
    name_for_draft = "" if relation else who
    if relation:
        who = L(c.lang, "one of your regular customers", "aapke ek regular customer")
    lang_pref = {"hinglish": "hi-en", "hindi": "hi"}.get(c.lang, "en")
    pseudo = {"customer_id": cid, "identity": {"name": name_for_draft or "(unknown)", "language_pref": lang_pref}, "relationship": {}, "preferences": {}}
    c2 = Ctx(category, merchant, trigger, pseudo, universe, now)
    handler = HANDLERS.get(c.kind)
    try:
        inner = handler(c2) if handler else None
    except Exception:
        inner = None
    if not inner or inner.skip or not inner.body or inner.send_as != "merchant_on_behalf":
        return None
    facts = payload_facts(c2)
    draft = with_brand(c, inner.body)
    quoted = "\n".join("> " + ln if ln.strip() else ">" for ln in draft.split("\n"))
    _, intent, _o = fit(c.kind, c.slug)
    intent = intent or ("is worth a personal message today", "ko aaj ek personal message bhejna sahi rahega")
    head = L(c.lang, f"{c.sal}, {who[0].upper() + who[1:]} {intent[0]}:", f"{c.sal}, {who} {intent[1]}:")
    mid = L(c.lang, "Here's what I'd send on your behalf:", "Aapki taraf se main yeh bhejungi:")
    body = "\n".join([head, bullets(facts), "", mid, quoted, "", approval_ask(c)])
    return Draft(body, "binary_yes_stop", "vera", c.base_facts() | {"customer_draft": draft},
                 offer=f"send {who} the message", followup={"type": "send_customer_reminder", "who": who, "draft": draft, "brand": brand_line(c)},
                 rationale="Customer trigger without the customer's profile: merchant approves a ready draft built only from trigger facts (consent first, nothing invented).",
                 template_params=[c.sal, who])


# =============================================================== unknown trigger kinds (decision: "read the trigger", eval round 4)
# New kinds can be injected mid-test. 1) nearest known kind, if the payload has fields that handler actually reads;
# 2) otherwise the trigger reader: the trigger's own facts (code) + one AI line + one concrete action; 3) no data -> skip.
ALIASES = [  # (target kind, name pattern) — checked in order
    ("wedding_package_followup", r"bridal|wedding|shaadi"), ("chronic_refill_due", r"refill"),
    ("recall_due", r"recall(?!_batch)|check_?up_due|revisit"), ("customer_lapsed_soft", r"lapsed|inactive_customer|churn"),
    ("appointment_tomorrow", r"appointment"), ("trial_followup", r"trial"), ("competitor_opened", r"competitor|rival"),
    ("festival_upcoming", r"festival|diwali|holi|eid|navratri|durga|onam|pongal|christmas"), ("ipl_match_today", r"ipl|cricket"),
    ("weather_heatwave", r"heat"), ("review_theme_emerged", r"review_theme"), ("perf_spike", r"spike|surge"),
    ("perf_dip", r"perf_(dip|drop)|(views|calls|traffic)_(dip|drop)"), ("milestone_reached", r"milestone"),
    ("renewal_due", r"renew|subscription_expir|plan_expir"), ("research_digest", r"research|digest|study"),
    ("regulation_change", r"regulation|compliance|circular|guideline"), ("supply_alert", r"supply|shortage|recall_batch"),
    ("category_trend_movement", r"trend"), ("local_news_event", r"news"), ("gbp_unverified", r"gbp|unverified"),
]
TEXT_KEYS = ("review_text", "text", "comment", "message", "quote", "feedback", "headline", "summary", "description", "note")
# ---- trigger rating (decision, round 4b): classify every new trigger as good / bad news BEFORE choosing words and tone
# -2 = needs attention today · -1 = heads-up · 0 = update · +1 = good news · +2 = great news (code only: deterministic, explainable)
GOOD_METRICS = r"(?:^|_| )(view|call|order|booking|revenue|sale|footfall|rating|review_count|lead|search|demand|member|signup|enrol|join|subscriber|customer|cover|conversion|ctr|score|visit|enquir|payout|cashback|bonus|crowd)"
BAD_METRICS = r"(?:^|_| )(cost|price|rent|fee|commission|tax|gst|wait|delay|complaint|cancel|refund|return|no_show|churn|rain|temp|aqi|pollution|fuel|shortage|expir|penalty|dispute)"
UP_WORDS = r"(?:^|_)(hike|rise|surge|spike|increase|jump|growth|gain|record|up|higher)(?:_|$)"
DOWN_WORDS = r"(?:^|_)(drop|dip|fall|decline|decrease|loss|slump|down|lower)(?:_|$)"
BAD_EVENTS = (r"(?:^|_)(complaint|negative|fail|delay|late|outage|closure|closed|strike|bandh|flood|storm|cyclone|monsoon|rain|heavy|fog|smog|"
              r"cold_wave|shortage|expir|penalt|suspend|violation|dispute|chargeback|fraud|block|reject|cancel|no_show|overdue|warning|lost)")
GOOD_EVENTS = (r"(?:^|_)(milestone|award|top|best|featured|won|approved|verified|record|positive|bulk_order|new_order|bonus|cashback|credited|"
               r"restock|back_in_stock|anniversary|festival|fair|mela|concert|marathon|carnival)")
BAD_TEXT = r"\b(cold|late|rude|dirty|bad|worst|waited|never|disappointed|overpriced|slow|stale|unhygienic|pain|hurt|wrong|missing|refund|horrible|terrible|poor)\b"
GOOD_TEXT = r"\b(great|amazing|loved|love|best|excellent|friendly|clean|recommend|awesome|fantastic|perfect|wonderful|happy|thank)\b"


def rate_trigger(kind: str, p: dict, quote: str = ""):
    """-> (rating -2..+2, reasons). Signals: event words in the type name, the direction of numbers x whether that metric
    is good or bad when it rises, star ratings, and the tone of any quoted text. No signal = 0 (plain update)."""
    k, sig, why = (kind or "").lower(), 0, []
    big = False
    if re.search(BAD_EVENTS, k):
        sig -= 1; why.append("bad-news event")
    if re.search(GOOD_EVENTS, k):
        sig += 1; why.append("good-news event")
    # direction: from/to pairs, then signed deltas, then the type name
    direction, metric = 0, k
    for key, v in p.items():
        m = re.match(r"(from|old|previous|before)_(\w+)$", key)
        if m and isinstance(v, (int, float)):
            for pre in ("to", "new", "current", "after"):
                v2 = p.get(f"{pre}_{m.group(2)}")
                if isinstance(v2, (int, float)) and v2 != v:
                    direction, metric = (1 if v2 > v else -1), m.group(2) + " " + k
                    move = abs(v2 - v) / max(abs(v), 1e-9)
                    big = big or move >= (0.05 if "rating" in m.group(2) else 0.2)
    if not direction:
        for key, v in p.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and re.search(r"delta|change|pct|percent|growth", key) and v:
                direction, metric = (1 if v > 0 else -1), key + " " + k
                big = abs(v) >= (20 if abs(v) > 1 else 0.2)
                break
    if not direction:
        direction = 1 if re.search(UP_WORDS, k) else -1 if re.search(DOWN_WORDS, k) else 0
    if direction:
        pol = -1 if re.search(BAD_METRICS, metric) else 1 if re.search(GOOD_METRICS, metric) else 0
        if pol:
            sig += direction * pol * (2 if big else 1)
            why.append(f"{'rising' if direction > 0 else 'falling'} {'bad' if pol < 0 else 'good'} metric{' (big move)' if big else ''}")
    stars = next((v for key, v in p.items() if re.search(r"^(rating|stars|star_rating|review_rating)$", key) and isinstance(v, (int, float))), None)
    if stars is not None:
        s = -2 if stars <= 2 else -1 if stars < 4 else 2 if stars >= 4.5 else 1
        sig += s; why.append(f"{stars:g}-star")
    if quote:
        b, g = len(re.findall(BAD_TEXT, quote.lower())), len(re.findall(GOOD_TEXT, quote.lower()))
        if b != g:
            sig += 1 if g > b else -1; why.append("positive text" if g > b else "negative text")
    rating = 0 if sig == 0 else (1 if sig > 0 else -1) * (2 if abs(sig) >= 2 else 1)
    return rating, why


TONE = {  # opener (en, hi), emoji allowed, AI tone instruction
    2: ("great news", "badhiya khabar", "🎉", "celebratory and warm, then point to keeping the momentum"),
    1: ("good news", "achhi khabar", "", "upbeat and encouraging"),
    0: ("quick update", "ek quick update", "", "neutral, brief and useful"),
    -1: ("a heads-up", "ek heads-up", "", "calm and practical, no alarm"),
    -2: ("this needs your attention today", "aaj dhyan dene wali baat", "", "calm, reassuring and action-first: it is fixable; never alarmist, no emoji"),
}
# family: (name pattern, subject en, subject hi, source, {"bad"|"neutral"|"good": (line en, line hi, cta en, cta hi)})
READER = [
    (r"review|rating|feedback|complaint|star", "{m}'s reviews", "{m} ke reviews", "Google reviews", {
        "bad": ("A calm, personal reply today shows every future customer that you care.", "Aaj hi ek shaant, personal reply dikhata hai ki aap care karte ho — baaki customers bhi yeh padhte hain.",
                "draft a short, polite reply you can post", "Main ek short, polite reply draft kar doon?"),
        "bad_noquote": ("Fresh replies and a few new happy-customer reviews are the quickest way to lift it back.", "Naye replies aur kuch khush customers ke reviews se rating sabse jaldi wapas upar aati hai.",
                        "draft a short WhatsApp asking your happy regulars for a review", "Khush regulars se review maangne ka ek short WhatsApp draft kar doon?"),
        "neutral": ("A quick reply shows every future customer that you're listening.", "Ek quick reply dikhata hai ki aap sun rahe ho.",
                    "draft a short reply you can post", "Ek short reply draft kar doon?"),
        "good": ("Happy customers are your best advert — worth thanking them where others can see it.", "Khush customers sabse achha advert hain — sabke saamne unhe thank karna banta hai.",
                 "draft a warm thank-you reply you can post", "Ek warm thank-you reply draft kar doon?")}),
    (r"slot|capacity|cancel|no_show|vacan|table_free|chair", "a slot just opened at {m}", "{m} mein ek slot abhi khali hua", "your bookings", {
        "bad": ("An empty slot is lost income — your regular customers can fill it fastest.", "Khali slot matlab nuksaan — regular customers ise sabse jaldi bhar sakte hain.",
                "draft a WhatsApp to offer it to your regulars", "Regulars ko offer karne ke liye ek WhatsApp draft kar doon?"),
        "neutral": ("Offering it to your regular customers fills it faster than waiting for walk-ins.", "Regular customers ko offer karne se yeh walk-in ke intezaar se jaldi bharta hai.",
                    "draft a WhatsApp to offer it to your regulars", "Regulars ko offer karne ke liye ek WhatsApp draft kar doon?")}),
    (r"weather|rain|monsoon|flood|cold|fog|storm|aqi|pollution|fuel|strike|bandh|traffic|power|outage|holiday|closure|event|election|exam|fair|mela|match|festival|nearby|crowd|concert|marathon|carnival",
     "local update for {loc}", "{loc} ka local update", "local alert", {
        "bad": ("Days like this change footfall — a quick note on timings or delivery keeps customers coming.", "Aise din footfall badalta hai — timings ya delivery pe ek chhota note customers ko jode rakhta hai.",
                "draft a short customer note for today", "Aaj ke liye ek chhota customer note draft kar doon?"),
        "neutral": ("A short, timely note to customers keeps you top of mind on days like this.", "Aise din ek chhota, timely note customers ko aapki yaad dilata hai.",
                    "draft a short customer post for today", "Aaj ke liye ek chhota customer post draft kar doon?"),
        "good": ("More people will be out and about — a timely post puts you in front of them.", "Zyada log bahar honge — ek timely post aapko unke saamne laata hai.",
                 "draft a short post to catch the crowd", "Crowd ke liye ek chhota post draft kar doon?")}),
    (r"search|demand|query|lead|enquir|interest|intent", "customer demand for {m}", "{m} ke liye customer demand", "magicpin data", {
        "bad": ("Demand is softer right now — a small, visible offer keeps you in front of the customers still searching.", "Abhi demand thoda kam hai — ek chhota, dikhne wala offer aapko search karne walon ke saamne rakhta hai.",
                "add a small offer to your profile", "Profile pe ek chhota offer add kar doon?"),
        "neutral": ("Catching demand early turns searches into calls.", "Demand jaldi pakadne se searches calls mein badalti hain.",
                    "add a matching offer to your profile with a short post", "Profile pe ek matching offer + chhota post add kar doon?"),
        "good": ("Catching this demand early turns searches into calls.", "Yeh demand jaldi pakadne se searches calls mein badalti hain.",
                 "add a matching offer to your profile with a short post", "Profile pe ek matching offer + chhota post add kar doon?")}),
    (r"price|cost|rent|gst|tax|fee|commission|payment|payout|settlement|invoice|wallet", "{m}'s account", "{m} ka account", "magicpin account", {
        "bad": ("Worth a one-minute look so it doesn't cost you more than it should.", "Ek minute dekh lijiye taaki zaroorat se zyada kharcha na ho.",
                "send a simple 3-line summary of what changes and what to do", "Kya badla aur kya karna hai — 3-line summary bhej doon?"),
        "neutral": ("Worth a one-minute look so nothing surprises you later.", "Ek minute dekh lijiye taaki baad mein koi surprise na ho.",
                    "send a simple 3-line summary of what changes for you", "Aapke liye kya badla, uska 3-line summary bhej doon?"),
        "good": ("Good to know — a quick look confirms everything matches your records.", "Achha hai — ek nazar se confirm ho jaayega ki sab aapke records se match karta hai.",
                 "send a simple 3-line summary", "Ek simple 3-line summary bhej doon?")}),
    (r"stock|inventory|expir|batch|restock", "{m}'s stock", "{m} ka stock", "stock alert", {
        "bad": ("Sorting it before the weekend avoids turning customers away.", "Weekend se pehle sort karne se customers wapas nahi jaate.",
                "draft a quick reorder list", "Ek quick reorder list draft kar doon?"),
        "neutral": ("Worth a quick look before the weekend rush.", "Weekend rush se pehle ek nazar dekh lijiye.",
                    "draft a quick reorder list", "Ek quick reorder list draft kar doon?"),
        "good": ("Worth telling the customers who were asking for it.", "Jo customers pooch rahe the, unhe bata dena chahiye.",
                 "draft a 'back in stock' WhatsApp for your regulars", "Regulars ke liye 'back in stock' WhatsApp draft kar doon?")}),
]
READER_DEFAULT = ("{m}", "{m}", "magicpin alert", {
    "bad": ("Worth sorting today, before it reaches your customers.", "Aaj hi sort karna sahi rahega, customers tak pahunchne se pehle.",
            "draft the one message I'd send to handle this", "Ise handle karne ke liye ek message draft kar doon?"),
    "neutral": ("", "", "draft the one message I'd send about this", "Is par bhejne layak ek message draft kar doon?"),
    "good": ("A good moment to make the most of it.", "Iska poora fayda uthane ka achha mauka hai.",
             "draft a short post to share the news", "Khabar share karne ke liye ek chhota post draft kar doon?")})
_KEY_CACHE: dict = {}


def _data_keys(p: dict) -> set:
    return {k for k, v in (p or {}).items() if k not in SKIP_KEYS and not k.endswith("_id") and v not in (None, "", [], {}) and v is not False}


def _handler_keys(kind: str) -> set:
    """Payload keys a known handler actually reads (read from its source, so it stays correct as handlers change)."""
    if kind not in _KEY_CACHE:
        import inspect
        try:
            src = inspect.getsource(HANDLERS[kind])
        except (OSError, TypeError, KeyError):
            src = ""
        _KEY_CACHE[kind] = set(re.findall(r"""(?:p|payload)(?:\.get\(|\[)["'](\w+)["']""", src))
    return _KEY_CACHE[kind]


NAME_KEYS = {"customer_name", "patient_name", "member_name", "name"}


def resolve_alias(kind: str, payload: dict):
    """Nearest known kind for an unknown trigger — only if that handler reads ALL of the trigger's data fields
    (otherwise it would drop the new facts, which is exactly what the reader is for)."""
    k = (kind or "").lower()
    keys = _data_keys(payload) - NAME_KEYS
    for target, pat in ALIASES:
        if target in HANDLERS and re.search(pat, k):
            return target if keys and keys <= _handler_keys(target) else None
    return None


UNITS = [(r"_(pct|percent)$", "%"), (r"_mm$", " mm"), (r"_(days|d)$", " days"), (r"_(hours|hrs|h)$", " hours"), (r"_km$", " km"),
         (r"_(c|celsius)$", "°C"), (r"_(inr|rs|rupees)$", "")]
LABEL_FIX = {"delta": "Change", "delta pct": "Change", "change": "Change", "window": "Window", "items": "Items"}


def _reader_facts(p: dict, limit: int = 3) -> list[str]:
    """Readable bullets from an unknown payload: keeps decimals (4.6 stays 4.6), units from key names, from/to pairs as a→b."""
    out, used = [], set()
    for k, v in p.items():
        m = re.match(r"(from|old|previous|before)_(\w+)$", k)
        if m:
            for pre in ("to", "new", "current", "after"):
                k2 = f"{pre}_{m.group(2)}"
                if k2 in p and isinstance(v, (int, float)) and isinstance(p[k2], (int, float)):
                    base, unit = m.group(2), ""
                    for pat, u in UNITS:
                        if re.search(pat, base):
                            base, unit = re.sub(pat, "", base), u
                            break
                    out.append(f"{humanize(base).capitalize()}: {v:g}{unit} → {p[k2]:g}{unit}")
                    used |= {k, k2}
    for k, v in p.items():
        if k in used or k in SKIP_KEYS or k in NAME_KEYS or k.endswith("_id") or k in TEXT_KEYS or isinstance(v, bool):
            continue
        unit, base = "", k
        for pat, u in UNITS:
            if re.search(pat, k):
                unit, base = u, re.sub(pat, "", k)
                break
        if isinstance(v, (int, float)):
            val = (f"{v:+g}%" if unit == "%" and re.search(r"delta|change|hike|drop|rise|up|down", k) else f"{v:g}{unit}")
            if re.search(r"_(inr|rs|rupees)$|price|amount|fee|cost", k):
                val = inr(v)
        elif isinstance(v, str) and re.search(r"\d", v) and not re.match(r"\d{4}-\d{2}-\d{2}", v):
            val = v.strip()   # keep '26-28 April' as written
        else:
            val = _fmt_val(k, v)
        if not val:
            continue
        label = humanize(re.sub(r"_iso$", "", base)).strip()
        label = LABEL_FIX.get(label.lower(), label.capitalize())
        out.append(f"{label}: {val}")
    return out[:limit]


def h_reader(c: Ctx) -> Draft:
    """Unknown trigger kind with real data. Step 1: rate it (good/bad news, how strong). Step 2: the rating picks the
    opener, tone and action template; the family (reviews, slots, ...) picks the subject and source. Facts come from the trigger."""
    p = c.payload
    fam = next((f[1:] for f in READER if re.search(f[0], c.kind.lower())), READER_DEFAULT)
    subj_en, subj_hi, src, tpl = fam
    quote = next((str(p[k]).strip() for k in TEXT_KEYS if isinstance(p.get(k), str) and len(p[k].strip()) >= 12), "")
    facts = _reader_facts(p)
    if not facts and not quote:
        return Draft("", skip=True, rationale=f"New trigger type '{c.kind}' with no readable data — skipped (R2, restraint).")
    rating, why = rate_trigger(c.kind, p, quote)
    mood = "bad" if rating < 0 else "good" if rating > 0 else "neutral"
    if mood == "bad" and not quote and "bad_noquote" in tpl:
        mood = "bad_noquote"
    line_en, line_hi, cta_en, cta_hi = tpl.get(mood) or tpl["neutral"]
    open_en, open_hi, emoji, tone = TONE[rating]
    loc = c.locality or c.city or L(c.lang, "your area", "aapke area")
    fmt = dict(m=c.mname, loc=loc)
    em = f" {emoji}" if emoji else ""
    head = L(c.lang, f"{c.sal}, {open_en}{em} — {subj_en.format(**fmt)} ({src}):", f"{c.sal}, {open_hi}{em} — {subj_hi.format(**fmt)} ({src}):")
    qline = ("> “" + (quote[:157] + "…" if len(quote) > 160 else quote) + "”") if quote else ""
    base_line = L(c.lang, line_en, line_hi)

    def render(ai):
        line = (ai or {}).get("line", "").strip() or base_line
        return "\n".join(x for x in [head, bullets(facts), qline, line, yes(c.lang, cta_en, cta_hi)] if x)
    f = c.base_facts() | {"trigger_facts": facts, "trigger_text": quote, "news_rating": rating}
    task = AITask("line", f"A new kind of update arrived for this {SINGULAR.get(c.slug, 'business')} (trigger_facts / trigger_text). "
                  f"It is rated {rating:+d} on a -2 (needs attention today) to +2 (great news) scale. Tone: {tone}. "
                  "Write ONE short sentence on what it means for THIS merchant and why acting today helps, using only their real data. "
                  "No numbers, no promises, no emoji.", no_numbers=True, priority=2)
    return Draft(render(None), facts=f, offer=cta_en, ai=task, render=render, followup={"type": "reader_action", "action": cta_en},
                 rationale=f"New trigger type '{c.kind}' rated {rating:+d} ({', '.join(why) or 'no good/bad signal'}): "
                           f"tone '{tone.split(',')[0]}', trigger's own facts (code), one AI line, one concrete action.",
                 template_params=[c.sal, c.mname])


# =============================================================== routing
HANDLERS = {
    "research_digest": h_research_digest, "category_research_digest_release": h_research_digest,
    "regulation_change": h_regulation_change, "cde_opportunity": h_cde_opportunity, "supply_alert": h_supply_alert,
    "category_seasonal": h_category_seasonal, "competitor_opened": h_competitor_opened, "review_theme_emerged": h_review_theme,
    "perf_dip": h_perf_dip, "perf_spike": h_perf_spike, "seasonal_perf_dip": h_seasonal_perf_dip, "milestone_reached": h_milestone,
    "renewal_due": h_renewal_due, "winback_eligible": h_winback, "gbp_unverified": h_gbp_unverified, "dormant_with_vera": h_dormant,
    "curious_ask_due": h_curious_ask, "festival_upcoming": h_festival, "ipl_match_today": h_ipl, "active_planning_intent": h_planning,
    "weather_heatwave": h_heatwave, "local_news_event": h_local_news, "category_trend_movement": h_trend_movement,
    # customer-facing
    "recall_due": h_recall_due, "appointment_tomorrow": h_appointment_tomorrow, "chronic_refill_due": h_chronic_refill,
    "trial_followup": h_trial_followup, "customer_lapsed_soft": h_customer_lapsed_soft, "customer_lapsed_hard": h_customer_lapsed_hard,
    "wedding_package_followup": h_wedding,
}
CUSTOMER_KINDS = {"recall_due", "appointment_tomorrow", "wedding_package_followup", "customer_lapsed_hard",
                  "customer_lapsed_soft", "trial_followup", "chronic_refill_due"}
ROTATION = ["curious_ask_due", "renewal_due", "dormant_with_vera", "category_trend_movement"]  # #30: #16 -> #12 case study -> #6 weak spots -> #29 trend


def draft_for(category, merchant, trigger, customer=None, universe=None, rotation_index: int = 0, now=None) -> Draft:
    c = Ctx(category, merchant, trigger, customer, universe, now)
    kind = c.kind
    if kind == "scheduled_recurring":
        # #30: rotate through approved formats; placeholder payload so empty-trigger variants are used
        for step in range(len(ROTATION)):
            k = ROTATION[(rotation_index + step) % len(ROTATION)]
            c2 = Ctx(category, merchant, {**trigger, "kind": k, "payload": {"placeholder": True, "rotation": True}}, customer, universe, now)
            d = HANDLERS[k](c2)
            if not d.skip:
                d.rationale = f"Scheduled check-in, rotation format '{k}': " + d.rationale
                return _clean(d)
        return Draft("", skip=True, rationale="Scheduled check-in: no format has data for this merchant.")
    if kind not in HANDLERS:  # new trigger kind (decision, round 4)
        target = resolve_alias(kind, c.payload)
        if target:
            d = draft_for(category, merchant, {**trigger, "kind": target}, customer, universe, rotation_index, now)
            d.rationale = f"New trigger type '{kind}' read as '{target}': " + d.rationale
            return d
        if not _data_keys(c.payload):
            return Draft("", skip=True, rationale=f"New trigger type '{kind}' with no data — skipped (R2, restraint).")
    dis, _i, _o = fit(kind, c.slug)
    if dis >= 2:
        return Draft("", skip=True, rationale=f"Trigger type doesn't fit a {SINGULAR.get(c.slug, 'this')} business (dissociation {dis}/3) — skipped (restraint).")
    if (trigger.get("scope") == "customer" or kind in CUSTOMER_KINDS) and not customer:
        d = _merchant_approval(category, merchant, trigger, universe, now, c)
        if d:
            return _clean(d)
        what = {"recall_due": ("may be due for their next visit", "ka next visit due ho sakta hai")}.get(kind, ASK_WHAT.get(kind, ("may be worth a personal follow-up", "ko ek personal follow-up bhejna sahi rahega")))
        return _clean(ask_merchant(c, *what))
    handler = HANDLERS.get(kind)
    try:
        d = handler(c) if handler else h_reader(c)
    except Exception as e:  # never crash a send on odd data
        d = h_generic(c)
        d.rationale += f" (fallback after {type(e).__name__}: {e})"
    return _clean(d)


def _clean(d: Draft) -> Draft:
    def tidy(s):
        s = re.sub(r"[ \t]+", " ", s or "")
        s = re.sub(r"\n{3,}", "\n\n", s)
        s = "\n".join(line.rstrip() for line in s.split("\n"))
        return s.replace(" .", ".").replace("..", ".").strip()
    d.body = tidy(d.body)
    if d.render:
        orig = d.render
        d.render = lambda ai: tidy(orig(ai))
    return d
