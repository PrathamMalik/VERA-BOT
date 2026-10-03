"""Quality floor: the minimum every outgoing message must meet (AI- or rules-written).

Checked by code, so it is cheap and deterministic. The LLM judge measures the finer things
(voice, relevance); this floor only catches the failure types that produced the 12/50 scores.
"""
from __future__ import annotations

import re

from .util import humanize, owner_first, customer_first

JARGON = re.compile(r"\b[a-z]+_[a-z_]+\b|\bplaceholder\b|\bpayload\b|\btrigger\b|\bmetric or topic\b|\bcontext\b", re.I)
CTA = re.compile(r"\breply\b|\bbatayein\b|\bbataiye\b|\bconfirm\b|\?", re.I)


def floor_check(body: str, merchant: dict, trigger: dict, customer: dict | None = None, send_as: str = "vera") -> list[str]:
    """Return the list of floor failures ([] = passes)."""
    fails = []
    text = body or ""
    own = [ln for ln in text.split("\n") if not ln.startswith(">")]  # ignore quoted customer drafts
    own_text = "\n".join(own)
    names = [n for n in (owner_first(merchant), (merchant.get("identity") or {}).get("name"), customer_first(customer)) if n]
    if not any(n.split()[0].lower() in text.lower() for n in names):
        fails.append("no_name")
    # (a "must contain a number" rule was tested in eval round 1 and did NOT predict judge scores -> dropped)
    kind_words = humanize(trigger.get("kind", ""))
    if JARGON.search(text) or (len(kind_words.split()) > 1 and kind_words.lower() in text.lower()):
        fails.append("internal_label")
    if not CTA.search(own[-1] if own else ""):
        fails.append("no_cta_at_end")
    if len(re.findall(r"\breply\s+(yes|no|\d|with|kariye|karein|karo|kar)\b", own_text, re.I)) > 1:
        fails.append("multiple_ctas")
    return fails
