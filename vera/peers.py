"""Peer comparisons, weak spots / positives, and data-backed case-study patterns.

Rules (from DECISIONS.md):
  * Peer averages are anonymous averages of >= 3 same-category merchants from our own data —
    same city first, then all cities — else the category's published benchmark. Never a name.
  * Case-study patterns compare groups of merchants (min 3 per group) and are kept ONLY if the data
    actually supports them. Framed as "merchants who do X tend to see Y", never as proven cause.
  * Every number here is computed by code. The AI may only choose among these facts.
"""
from __future__ import annotations

from statistics import mean

from .util import num, pct, humanize, L

MIN_GROUP = 3
NOUN = {"dentists": "clinics", "salons": "salons", "restaurants": "restaurants", "gyms": "gyms", "pharmacies": "pharmacies"}
METRICS = [("views", "Views", "avg_views_30d"), ("calls", "Calls", "avg_calls_30d"),
           ("directions", "Direction requests", "avg_directions_30d"), ("ctr", "CTR", "avg_ctr")]
RATE_KEYS = [("retention_6mo_pct", "6-month retention"), ("retention_3mo_pct", "3-month retention"),
             ("retention_30d_pct", "30-day retention")]


def noun(slug):
    return NOUN.get(slug, "businesses")


def _fmt(metric, v):
    return pct(v) if metric == "ctr" else num(v)


class PeerGroup:
    """Anonymous comparison set for one merchant."""

    def __init__(self, merchant: dict, category: dict, universe: list | None):
        self.m = merchant
        self.cat = category or {}
        self.slug = merchant.get("category_slug") or self.cat.get("slug", "")
        self.bench = self.cat.get("peer_stats") or {}
        city = merchant.get("identity", {}).get("city")
        others = [x for x in (universe or []) if x.get("category_slug") == self.slug
                  and x.get("merchant_id") != merchant.get("merchant_id") and x.get("performance")]
        same_city = [x for x in others if x.get("identity", {}).get("city") == city]
        if len(same_city) >= MIN_GROUP:
            self.members, self.label = same_city, f"similar {noun(self.slug)} in {city}"
        elif len(others) >= MIN_GROUP:
            self.members, self.label = others, f"similar {noun(self.slug)} on magicpin"
        else:
            self.members, self.label = [], f"similar {noun(self.slug)}"
        self.source = "peers" if self.members else "benchmark"

    def avg(self, metric):
        """30-day average for views/calls/directions/ctr."""
        if self.members:
            vals = [x["performance"].get(metric) for x in self.members if x["performance"].get(metric) is not None]
            if len(vals) >= MIN_GROUP:
                v = mean(vals)
                return round(v, 3) if metric == "ctr" else round(v)
        key = dict((m, k) for m, _, k in METRICS).get(metric)
        return self.bench.get(key)

    def avg_delta(self, metric):
        """Average 7-day change of the group (None if < 3 data points)."""
        vals = [x["performance"].get("delta_7d", {}).get(f"{metric}_pct") for x in self.members]
        vals = [v for v in vals if v is not None]
        return round(mean(vals), 3) if len(vals) >= MIN_GROUP else None

    def share(self, fn):
        """Share of the group satisfying fn (None if no group)."""
        if len(self.members) < MIN_GROUP:
            return None
        return sum(1 for x in self.members if fn(x)) / len(self.members)


def _gap(mine, peer):
    try:
        return (mine - peer) / peer if peer else None
    except TypeError:
        return None


def weak_spots(m: dict, pg: PeerGroup, lang: str = "en") -> list[dict]:
    """Every real weakness we can prove from the data, most severe first."""
    out = []
    perf = m.get("performance") or {}
    for metric, label, _ in METRICS:
        mine, peer = perf.get(metric), pg.avg(metric)
        g = _gap(mine, peer) if mine is not None and peer else None
        if g is not None and g <= -0.10:
            out.append({"id": f"below_{metric}", "type": f"low_{metric}", "sev": min(abs(g), 1.0),
                        "text": f"{label}: {_fmt(metric, mine)} vs {_fmt(metric, peer)} ({pct(g, signed=True)})"})
    for k, v in (perf.get("delta_7d") or {}).items():
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        metric = k.replace("_pct", "")
        if v <= -0.10 and metric in ("calls", "views"):
            out.append({"id": f"drop_{metric}", "type": f"drop_{metric}", "sev": min(abs(v), 1.0) * 0.9,
                        "text": L(lang, f"{metric.capitalize()} down {pct(v)} this week", f"Is hafte {metric} {pct(v)} neeche")})
    for t in m.get("review_themes") or []:
        if t.get("sentiment") == "neg" and t.get("occurrences_30d"):
            q = f" (\"{t['common_quote']}\")" if t.get("common_quote") else ""
            out.append({"id": f"theme_{t['theme']}", "type": f"theme_{t['theme']}", "sev": 0.35 + 0.05 * int(t["occurrences_30d"]),
                        "text": f"{humanize(t['theme']).capitalize()}: {t['occurrences_30d']} Google reviews this month{q}"})
    agg = m.get("customer_aggregate") or {}
    for key, label in RATE_KEYS:
        if agg.get(key) is not None and pg.bench.get(key) is not None and agg[key] < pg.bench[key] - 0.05:
            out.append({"id": f"low_{key}", "type": "low_retention", "sev": pg.bench[key] - agg[key] + 0.2,
                        "text": f"{label}: {pct(agg[key])} vs {pct(pg.bench[key])}"})
    if agg.get("monthly_churn_pct") is not None and pg.bench.get("monthly_churn_pct") is not None \
            and agg["monthly_churn_pct"] > pg.bench["monthly_churn_pct"]:
        out.append({"id": "high_churn", "type": "high_churn", "sev": 0.4,
                    "text": f"Monthly churn: {pct(agg['monthly_churn_pct'])} vs {pct(pg.bench['monthly_churn_pct'])}"})
    if agg.get("trial_to_paid_pct") is not None and pg.bench.get("trial_to_paid_pct") is not None \
            and agg["trial_to_paid_pct"] < pg.bench["trial_to_paid_pct"]:
        out.append({"id": "low_trial_conv", "type": "low_trial_conv", "sev": 0.35,
                    "text": f"Trial-to-paid: {pct(agg['trial_to_paid_pct'])} vs {pct(pg.bench['trial_to_paid_pct'])}"})
    if m.get("identity", {}).get("verified") is False:
        out.append({"id": "unverified", "type": "unverified", "sev": 0.45, "text": "Google profile not verified"})
    if not [o for o in m.get("offers") or [] if o.get("status") == "active"]:
        out.append({"id": "no_offer", "type": "no_offer", "sev": 0.3, "text": "No live offer on your Google profile"})
    out.sort(key=lambda x: -x["sev"])
    return out


def positives(m: dict, pg: PeerGroup) -> list[dict]:
    out = []
    perf = m.get("performance") or {}
    for metric, label, _ in METRICS:
        mine, peer = perf.get(metric), pg.avg(metric)
        g = _gap(mine, peer) if mine is not None and peer else None
        if g is not None and g >= 0.10:
            out.append({"id": f"above_{metric}", "sev": min(g, 3.0),
                        "text": f"{label}: {_fmt(metric, mine)} vs {_fmt(metric, peer)} ({pct(g, signed=True)})"})
    for t in m.get("review_themes") or []:
        if t.get("sentiment") == "pos" and t.get("occurrences_30d"):
            q = f" (\"{t['common_quote']}\")" if t.get("common_quote") else ""
            out.append({"id": f"theme_{t['theme']}", "sev": 0.5 + 0.05 * int(t["occurrences_30d"]),
                        "text": f"{t['occurrences_30d']} Google reviews this month praise your {humanize(t['theme'])}{q}"})
    for k, v in (perf.get("delta_7d") or {}).items():
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v >= 0.10 and k.replace("_pct", "") in ("calls", "views"):
            out.append({"id": f"rise_{k}", "sev": v, "text": f"{k.replace('_pct', '').capitalize()} up {pct(v)} this week"})
    agg = m.get("customer_aggregate") or {}
    for key, label in RATE_KEYS:
        if agg.get(key) is not None and pg.bench.get(key) is not None and agg[key] > pg.bench[key] + 0.05:
            out.append({"id": f"high_{key}", "sev": 0.4, "text": f"{label}: {pct(agg[key])} vs {pct(pg.bench[key])}"})
    if agg.get("monthly_churn_pct") is not None and pg.bench.get("monthly_churn_pct") is not None \
            and agg["monthly_churn_pct"] < pg.bench["monthly_churn_pct"]:
        out.append({"id": "low_churn", "sev": 0.45, "text": f"Monthly churn: {pct(agg['monthly_churn_pct'])} vs {pct(pg.bench['monthly_churn_pct'])} benchmark"})
    if agg.get("trial_to_paid_pct") is not None and pg.bench.get("trial_to_paid_pct") is not None \
            and agg["trial_to_paid_pct"] > pg.bench["trial_to_paid_pct"]:
        out.append({"id": "high_trial_conv", "sev": 0.45, "text": f"Trial-to-paid: {pct(agg['trial_to_paid_pct'])} vs {pct(pg.bench['trial_to_paid_pct'])} benchmark"})
    if m.get("identity", {}).get("verified"):
        out.append({"id": "verified", "sev": 0.05, "text": "Verified Google profile"})
    out.sort(key=lambda x: -x["sev"])
    return out


# ------------------------------------------------------------------ case-study patterns (data-backed only)
def _has_offer(x):
    return any(o.get("status") == "active" for o in x.get("offers") or [])


PATTERNS = [
    # id, group test, applies-to-merchant test, wording
    ("verified", lambda x: bool(x.get("identity", {}).get("verified")), "Verified profiles", "unverified ones",
     "yours is still unverified", lambda m: m.get("identity", {}).get("verified") is False),
    ("has_offer", _has_offer, "Merchants with an active offer", "those without",
     "you have none live", lambda m: not _has_offer(m)),
]


def case_patterns(universe: list | None) -> list[dict]:
    """Patterns the data supports across ALL merchants (min 3 per group). Empty list if unsupported."""
    out = []
    ms = [x for x in (universe or []) if x.get("performance")]
    for pid, test, yes_lbl, no_lbl, gap_txt, applies in PATTERNS:
        a = [x for x in ms if test(x)]
        b = [x for x in ms if not test(x)]
        if len(a) < MIN_GROUP or len(b) < MIN_GROUP:
            continue
        parts = []
        for metric in ("views", "calls"):
            va, vb = mean(x["performance"][metric] for x in a), mean(x["performance"][metric] for x in b)
            if va > vb * 1.05:
                parts.append((metric, round(va), round(vb), (va - vb) / vb))
        if not parts:
            continue  # the data does not support this pattern -> never claim it
        text = f"{yes_lbl} average " + " and ".join(f"{num(va)} {mt}" for mt, va, vb, g in parts) + \
               f" vs " + " and ".join(f"{num(vb)}" for mt, va, vb, g in parts) + f" for {no_lbl} (" + \
               ", ".join(f"{pct(g, signed=True)} {mt}" for mt, va, vb, g in parts) + ")"
        out.append({"id": pid, "text": text, "gap_text": gap_txt, "applies": applies, "n": (len(a), len(b))})
    return out
