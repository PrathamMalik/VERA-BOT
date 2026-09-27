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
        self.lang = merchant_lang(self.merchant)
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


# =============================================================== the #6 "weak spots" pattern (shared by #6 #7 #8 #10 #15)
def weak_spot_draft(c: Ctx, head: str, rationale: str, extra_after: str = "", exclude_types=(), min_spots=0) -> Draft:
    ws = [w for w in weak_spots(c.merchant, c.pg, c.lang) if w["type"] not in exclude_types]
    pos = positives(c.merchant, c.pg)
    cands = [{"id": w["id"], "text": w["text"]} for w in ws[:8]]
    follow = {"type": "positives_strategies", "weak_types": [], "weak_texts": [], "positives": [p["text"] for p in pos[:3]],
              "noun": noun(c.slug)}

    def render(ai):
        ids = [w["id"] for w in ws]
        picks = [i for i in (ai or {}).get("picks", []) if i in ids][:3] if ai else []
        if not picks:
            picks = ids[:3] if len(ids) >= 3 and ws[2]["sev"] >= 0.3 else ids[:2]
        chosen = [w for w in ws if w["id"] in picks]
        follow["weak_types"] = [w["type"] for w in chosen]
        follow["weak_texts"] = [w["text"] for w in chosen]
        parts = [head]
        if chosen:
            parts.append(L(c.lang, f"Your weakest spots (you vs avg for {c.pg.label}):", f"Aapke weak spots (aap vs {c.pg.label} ka avg):"))
            parts.append(bullets(w["text"] for w in chosen))
        else:
            parts.append(L(c.lang, f"You're ahead of {c.pg.label} on every number we track.",
                           f"Aap har tracked number pe {c.pg.label} se aage ho."))
        if extra_after:
            parts.append(extra_after)
        if chosen:
            parts.append(yes(c.lang, "show what's working for you + the common fixes",
                             "Kya kaam kar raha hai + common fixes dikha doon?"))
        else:
            parts.append(yes(c.lang, "show how to keep that lead", "Yeh lead kaise bani rahe, dikha doon?"))
        return "\n".join(p for p in parts if p)

    f = c.base_facts()
    f["weak_spot_candidates"] = [w["text"] for w in ws[:8]]
    f["peer_label"] = c.pg.label
    task = AITask("picks_line", "Pick the 1-3 weak spots that matter most for this merchant right now, given the trigger. Return picks only; line may be empty.",
                  candidates=cands, max_picks=3, priority=2) if len(cands) > 2 else None
    d = Draft(render(None), facts=f, offer="positives + strategies", rationale=rationale, ai=task, render=render, followup=follow,
              template_params=[c.sal, c.mname])
    return d


# =============================================================== merchant-facing handlers
def h_research_digest(c: Ctx) -> Draft:
    items = [d for d in c.category.get("digest") or [] if d.get("kind") in ("research", "trend", "tech")]
    item = c.digest(c.payload.get("top_item_id"))
    empty = item is None
    if empty:
        words = (" ".join(c.offers) + " " + c.history_text() + " " + " ".join(t.get("theme", "") for t in c.merchant.get("review_themes") or [])).lower()
        scored = sorted(items, key=lambda d: -sum(1 for w in re.findall(r"[a-z]{5,}", (d.get("title", "") + d.get("summary", "")).lower()) if w in words))
        item = scored[0] if scored else None
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
            it = by_id[ai["picks"][0]]
        return build(it, (ai or {}).get("line", "").strip())

    f = c.base_facts(); f["digest_items"] = items if empty else [item]
    task = AITask("picks_line" if empty else "line",
                  ("Pick the ONE digest item most relevant to this merchant, and write one sentence on why it matters to THEM "
                   if empty else "Write one sentence on why this item matters to THIS merchant ")
                  + "(their patients/customers, offers, past requests). Never overstate the study (no 'proves', 'cures', 'guaranteed').",
                  candidates=[{"id": d["id"], "text": d["title"]} for d in items] if empty else [], max_picks=1, priority=2)
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
        head = f"{c.sal}, heads-up:\n" + bullets(facts) + "\n" + \
            L(c.lang, "New places win customers where you're weakest.", "Naye places wahan jeetate hain jahan aap weak ho.")
    else:
        head = L(c.lang, f"{c.sal}, a new {c.slug.rstrip('s')} has opened near {c.locality}. New places win customers where you're weakest.",
                 f"{c.sal}, {c.locality} ke paas naya {c.slug.rstrip('s')} khula hai. Naye places wahan jeetate hain jahan aap weak ho.")
    d = weak_spot_draft(c, head, "Competitor opened: exact competitor facts (never invented) + code-computed weak spots vs peers; closing offer = positives + strategies.")
    d.facts["competitor"] = p
    return d


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
    head = L(c.lang, f"{c.sal}, a quick look at {c.mname}.", f"{c.sal}, {c.mname} pe ek quick look.")
    return weak_spot_draft(c, head, "Review-theme trigger without review data: no vague claims — weak spots vs peers instead.")


def h_perf_dip(c: Ctx) -> Draft:
    p = c.payload
    if not c.placeholder and p.get("metric"):
        metric, d, base = p["metric"], float(p.get("delta_pct", 0)), p.get("vs_baseline")
    else:
        mv = _moves(c.delta, -1)
        if not mv:
            # honest status instead of a fake 'dip': nothing is down, but show the real gap vs peers (if any)
            ups = ", ".join(f"{k} {pct(v, signed=True)}" for k, v in _moves(c.delta, +1)[:2])
            head = L(c.lang, f"{c.sal}, quick look at your numbers: {ups + ' this week — ' if ups else ''}nothing is dipping right now.",
                     f"{c.sal}, aapke numbers pe ek nazar: {ups + ' is hafte — ' if ups else ''}abhi kuch gir nahi raha.")
            if not weak_spots(c.merchant, c.pg, c.lang):
                return Draft("", skip=True, rationale="Empty perf_dip trigger, numbers not down and no gap vs peers — nothing honest to add.")
            fix = ""
            if not c.offers and c.catalog_offer():
                fix = L(c.lang, f"Quickest fix: no active offer on Google — e.g. \"{c.catalog_offer()}\".",
                        f"Sabse jaldi fix: Google pe koi active offer nahi — jaise \"{c.catalog_offer()}\".")
            return weak_spot_draft(c, head, "Dip trigger but the data shows no dip: honest status + the real gaps vs peers + one quick fix.",
                                   extra_after=fix, exclude_types=("no_offer",) if fix else ())
        (metric, d), base = mv[0], None
    head = L(c.lang, f"{c.sal}, your {_metric_label(metric)} dropped {pct(d)} this week" + (f" (usually ~{base})" if base else "") + ".",
             f"{c.sal}, is hafte aapke {_metric_label(metric)} {pct(d)} gire" + (f" (usually ~{base})" if base else "") + ".")
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
        lead = L(c.lang, f"{c.sal}, {_metric_label(metric)} up {pct(d)} this week" + (f" (usually ~{base})" if base else "") +
                 (f", likely from your {humanize(driver)}" if driver else "") + ". Extra calls only matter if they convert:",
                 f"{c.sal}, is hafte {_metric_label(metric)} {pct(d)} upar" + (f" (usually ~{base})" if base else "") +
                 (f", shayad aapke {humanize(driver)} ki wajah se" if driver else "") + ". Extra calls tabhi kaam ke hain jab convert hon:")
        if c.offers:
            tips = ["Mention your offers when people call: " + ", ".join(f"\"{o}\"" for o in c.offers[:2]),
                    "Reply to every new enquiry the same day"]
            cta = yes(c.lang, "draft a 2-line reply you can send to every new enquiry", "Har nayi enquiry ke liye 2-line reply draft kar doon?")
            follow = {"type": "enquiry_reply"}
        else:
            sug = c.catalog_offer(kinds=("free_service", "service_at_price"))
            tips = [f"You have no active offer to mention — {noun(c.slug)} often use one like \"{sug}\""]
            cta = yes(c.lang, "put it live on your profile", "Ise profile pe live kar doon?")
            follow = {"type": "offer_live", "offer": sug}
        body = "\n".join([lead, bullets(tips), cta])
        return Draft(body, facts=f, offer="enquiry reply draft", followup=follow,
                     rationale="Real spike (≥10%): convert the extra enquiries using the merchant's real offers.", template_params=[c.sal, pct(d)])
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
        lead = L(c.lang, f"{c.sal}, {_metric_label(metric)} up {pct(d)} this week — " + (f"same as {pg.label} ({pct(mkt, signed=True)}), so it's the market, not just you." if abs(d - mkt) < 0.05 else f"vs {pct(mkt, signed=True)} for {pg.label}."),
                 f"{c.sal}, is hafte {_metric_label(metric)} {pct(d)} upar — " + (f"{pg.label} jaisa hi ({pct(mkt, signed=True)}), yani market ka move hai." if abs(d - mkt) < 0.05 else f"{pg.label} ka {pct(mkt, signed=True)}."))
    else:
        lead = L(c.lang, f"{c.sal}, {_metric_label(metric)} up {pct(d)} this week — a small move.", f"{c.sal}, is hafte {_metric_label(metric)} {pct(d)} upar — chhota move.")

    def render(ai):
        ids = [x["id"] for x in cands]
        picks = [i for i in (ai or {}).get("picks", []) if i in ids][:3] or ids[:3]
        chosen = [x for x in cands if x["id"] in picks]
        parts = [lead]
        if chosen:
            parts += [L(c.lang, "Where you differ (you vs their avg):", "Aap kahan alag ho (aap vs unka avg):"), bullets(x["text"] for x in chosen)]
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
    peer_line = (f" — {c.pg.label} are down {pct(mkt)} too" if mkt is not None else "")
    head = L(c.lang, f"{c.sal}, {_metric_label(metric)} down {pct(d)} this week — that's the season, not you{peer_line}."
             + (f" ({beat['month_range']}: {beat['note']}.)" if beat else ""),
             f"{c.sal}, is hafte {_metric_label(metric)} {pct(d)} neeche — yeh season hai, aap nahi{peer_line}."
             + (f" ({beat['month_range']}: {beat['note']}.)" if beat else ""))
    tip = (L(c.lang, f"Money-saving tip: {dig['actionable'].rstrip('.')}.", f"Paisa bachane ka tip: {dig['actionable'].rstrip('.')}.")
           if dig and dig.get("actionable") else "")
    return weak_spot_draft(c, head, "Seasonal dip confirmed by peers: reassure + weak spots + one cost-saving tip.",
                           extra_after=tip, exclude_types=(f"drop_{metric}", "drop_calls", "drop_views"))


def h_milestone(c: Ctx) -> Draft:
    p = c.payload
    if c.placeholder or not p.get("metric"):
        # only a REAL, calculated milestone: the largest round number actually crossed in the merchant's data
        nice = [100, 250, 500, 750, 1000, 1500, 2000, 2500, 5000, 10000]
        found = None
        for val, label in ((c.agg.get("total_unique_ytd"), "customers this year"), (c.perf.get("views"), "profile views in 30 days")):
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
        body = "\n".join([L(c.lang, f"{c.sal}, a milestone worth marking:", f"{c.sal}, ek milestone jo celebrate karna chahiye:"), bullets(facts),
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
    # empty -> data-backed case study, regardless of days left
    pats = case_patterns(c.universe)
    apply = [x for x in pats if x["applies"](c.merchant)]
    if not pats:
        return h_generic(c)
    use = apply or pats[:1]
    steps = []
    for x in apply:
        steps.append({"verified": "Verify your Google profile (postcard or phone call)",
                      "has_offer": f"Put one service+price offer live, e.g. \"{c.catalog_offer() or 'a service @ price'}\""}[x["id"]])
    body = "\n".join([L(c.lang, f"{c.sal}, a quick case study from magicpin merchants:", f"{c.sal}, magicpin merchants se ek quick case study:"),
                      bullets(x["text"] + (f" — {x['gap_text']}" if x in apply else "") for x in use),
                      (L(c.lang, "Steps:", "Steps:") + "\n" + bullets(steps)) if steps else L(c.lang, "You're already set up this way — keep it that way.", "Aap pehle se aise set ho — bas yeh bana rahe."),
                      yes(c.lang, "start with the first step", "Pehla step shuru kar doon?") if steps else yes(c.lang, "share one more idea for this month", "Is mahine ke liye ek aur idea bhej doon?")])
    return Draft(body, facts=c.base_facts(), offer="case-study steps", followup={"type": "fix_first", "fixes": [x["id"] for x in apply]},
                 rationale="Renewal trigger without details: data-backed case study (patterns across ≥3 merchants per group, framed as tendencies).",
                 template_params=[c.sal])


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
            if days and not c.placeholder else L(c.lang, f"{c.sal}, a quick check on {c.mname}.", f"{c.sal}, {c.mname} pe ek quick check."))
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
                      bullets([f"For {c.slug}, {beat['month_range']} is the {beat['note']}" if beat else None,
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
    event = (f"it's {temp}°C" if temp else "a heatwave is on") + (f" in {city} today" if city else " today")
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


def ask_merchant(c: Ctx, what_en: str, what_hi: str) -> Draft:
    """Empty customer trigger: route to the merchant for approval instead of messaging the customer."""
    who = customer_first(c.customer) or name_from_customer_id(c.trigger.get("customer_id")) or L(c.lang, "one of your customers", "aapke ek customer")
    rel = (c.customer or {}).get("relationship", {})
    hist = []
    if rel.get("last_visit"):
        hist.append(f"last visit {nice_date(rel['last_visit'], True)}")
    if rel.get("visits_total"):
        hist.append(f"{rel['visits_total']} visits")
    body = L(c.lang, f"{c.sal}, {who}" + (f" ({', '.join(hist)})" if hist else "") + f" {what_en}. Want me to send them a reminder on your behalf? Reply YES.",
             f"{c.sal}, {who}" + (f" ({', '.join(hist)})" if hist else "") + f" {what_hi}. Aapki taraf se reminder bhej doon? Reply YES.")
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
    head = f"{_greet(c, name)}, {c.mname} here{emoji} " + L(c.clang, f"Your {svc} is due" + (f" (last: {lv})." if lv else "."),
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
    body = "\n".join([f"{_greet(c, name)}, {c.mname} " + L(c.clang, "here 👋 Quick reminder:", "se 👋 Ek quick reminder:"), bullets(items),
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
        head = f"Namaste {name_part}, {c.mname} se 🙏\n" + (f"Aapki monthly dawaiyan {runs} ko khatam ho gayi hongi:" if past else f"Aapki monthly dawaiyan {runs} tak khatam ho jayengi:")
        close = "Confirm karne ke liye YES reply karein."
    else:
        head = f"Hi {name_part}, {c.mname} here.\nYour monthly medicines " + (f"were due for a refill on {runs}:" if past else f"run out on {runs}:")
        close = "Reply YES to confirm."
    body = "\n".join([head, bullets(m.capitalize() for m in p["molecule_list"]), "", bullets(extras), close])
    return Draft(body, "binary_yes_stop", "merchant_on_behalf", c.base_facts() | {"customer": c.customer}, offer="deliver refill",
                 followup={"type": "refill"}, rationale="Chronic refill: bullets, real offers; recall handled quietly (only discussed if the customer asks).",
                 template_params=[name, c.mname, runs])


def h_trial_followup(c: Ctx) -> Draft:
    p = c.payload
    opts = [o.get("label") for o in p.get("next_session_options") or [] if o.get("label")]
    if c.placeholder or not opts:
        return ask_merchant(c, "did a trial recently", "ne haal hi mein trial kiya")
    parent, kid = parent_name(c.customer), child_name(c.customer)
    name = parent or customer_first(c.customer)
    thanks = (f"Thanks for bringing {kid} for the trial on {nice_date(p.get('trial_date'))}!" if parent and kid
              else f"Thanks for joining the trial on {nice_date(p.get('trial_date'))}!")
    items = [f"Next session: {opts[0]}"] + ([f"To continue: {c.offers[0]}"] if c.offers else [])
    pos = next((t for t in c.merchant.get("review_themes") or [] if t.get("sentiment") == "pos" and t.get("theme") == "small_classes"), None) or c.pos_theme()
    proof = (f"Parents love our {humanize(pos['theme'])} — {pos['occurrences_30d']} reviews this month mention it." if pos and parent else
             f"{pos['occurrences_30d']} reviews this month mention our {humanize(pos['theme'])}." if pos else "")
    body = "\n".join([f"Hi {name}, {c.mname} here 🧘 {thanks}" if c.slug == "gyms" else f"Hi {name}, {c.mname} here. {thanks}", bullets(items), proof,
                      "Reply YES to book, or tell us a better time."])
    return Draft(body, "binary_yes_stop", "merchant_on_behalf", c.base_facts() | {"customer": c.customer}, offer="book next session",
                 followup={"type": "booking", "slots": opts}, rationale="Trial follow-up: bullets + real review proof, addressed to the parent.", template_params=[name, c.mname])


def h_customer_lapsed_soft(c: Ctx) -> Draft:
    name = customer_first(c.customer)
    rel = (c.customer or {}).get("relationship", {})
    items = [f"Last visit: {nice_date(rel['last_visit'])}" if rel.get("last_visit") else None,
             f"Visits with us: {rel['visits_total']}" if rel.get("visits_total") else None]
    emoji = {"dentists": " 🦷", "gyms": " 💪", "salons": " ✨"}.get(c.slug, "")
    nxt = {"dentists": "check-up", "gyms": "session", "salons": "appointment"}.get(c.slug, "visit")
    head = f"{_greet(c, name)}, {c.mname} " + L(c.clang, f"here{emoji} — it's been a while!", f"se{emoji} — kaafi time ho gaya!")
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
    days = c.days_since(lv, p.get("days_since_last_visit")) if lv else (p.get("days_since_last_visit") if c.now is None else None)
    slot = humanize(prefs.get("preferred_slots", ""))
    items = [f"Your {focus} plan" + (f" from your {months} months with us" if months else "") + " is still on file" if focus else None,
             f"{slot.capitalize()} slots are open for you" if slot else None]
    body = "\n".join([f"{_greet(c, name)}, {c.mname} here 💪 " + (f"It's been {days} days — no pressure, just checking in." if days else "No pressure, just checking in."),
                      bullets(items), "Reply YES and we'll hold one this week."])
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
    body = "\n".join([f"Hi {name} 💍 {c.mname} here — " + (f"{dtw} days to your wedding!" if dtw else "your wedding is coming up!"),
                      bullets(items), f"Want us to hold a {pref.title()} slot next week to plan it? Reply YES." if pref else "Want us to hold a slot next week to plan it? Reply YES."])
    return Draft(body, "binary_yes_stop", "merchant_on_behalf", c.base_facts() | {"customer": c.customer}, offer="hold bridal planning slot",
                 followup={"type": "confirm_slot"}, rationale="Bridal follow-up: countdown bullets, preferred day, no invented price.", template_params=[name, c.mname])


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
            c2 = Ctx(category, merchant, {**trigger, "kind": k, "payload": {"placeholder": True}}, customer, universe, now)
            d = HANDLERS[k](c2)
            if not d.skip:
                d.rationale = f"Scheduled check-in, rotation format '{k}': " + d.rationale
                return _clean(d)
        return Draft("", skip=True, rationale="Scheduled check-in: no format has data for this merchant.")
    if (trigger.get("scope") == "customer" or kind in CUSTOMER_KINDS) and not customer:
        what = {"recall_due": ("may be due for their next visit", "ka next visit due ho sakta hai")}.get(kind, (f"has a '{humanize(kind)}' update", f"ka '{humanize(kind)}' update hai"))
        return _clean(ask_merchant(c, *what))
    handler = HANDLERS.get(kind)
    try:
        d = handler(c) if handler else h_local_news(c) if any(k in c.payload for k in ("headline", "title", "summary", "event")) else h_generic(c)
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
