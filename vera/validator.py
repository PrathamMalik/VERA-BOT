"""Post-generation checks. The single most important job: block anything the contexts can't back up."""
from __future__ import annotations

import json
import re

GENERIC_TABOO = ["guaranteed", "guarantee", "100% safe", "miracle", "best in city", "i hope you are doing well",
                 "i hope you're doing well", "i am reaching out", "i'm reaching out", "proves", "cures"]
QUALIFYING = ["would you", "do you", "can you tell", "what if", "how about"]
NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿⬀-⯿]")


def _norm_num(tok: str) -> str:
    t = tok.replace(",", "")
    if re.match(r"^\d+\.\d+$", t):
        t = t.rstrip("0").rstrip(".")
    return t


def allowed_numbers(facts, *texts) -> set[str]:
    """Every number that appears in the facts / reference texts, plus % renderings of fractions."""
    blob = json.dumps(facts, ensure_ascii=False, default=str) + " " + " ".join(t or "" for t in texts)
    out = set()
    for tok in NUM_RE.findall(blob):
        n = _norm_num(tok)
        out.add(n)
        try:
            v = float(n)
            if 0 < abs(v) < 5 and "." in n:
                for d in (0, 1):
                    out.add(_norm_num(f"{abs(v) * 100:.{d}f}"))
            if v == int(v):
                out.add(str(int(v)))
        except ValueError:
            pass
    out.update(str(i) for i in range(0, 11))  # small numbers: Vera's own proposals ("2-min", "Reply 1 or 2")
    return out


def check(body: str, facts: dict, reference: str = "", taboos=None, must_mention=None, max_len: int = 1200,
          no_numbers: bool = False, max_emoji: int = 1, need_bullets: int = 0, action_mode: bool = False) -> list[str]:
    """Return a list of problems (empty list = pass)."""
    problems = []
    if not body or not body.strip():
        return ["empty"]
    if len(body) > max_len:
        problems.append(f"too long ({len(body)} > {max_len})")
    if no_numbers and NUM_RE.search(body):
        problems.append("contains numbers (not allowed in this part)")
    elif not no_numbers:
        bad = sorted({_norm_num(t) for t in NUM_RE.findall(body)} - allowed_numbers(facts, reference))
        if bad:
            problems.append(f"numbers not in context: {', '.join(bad[:6])}")
    low = body.lower()
    for t in (taboos or []) + GENERIC_TABOO:
        t0 = re.sub(r"\(.*?\)", "", str(t)).strip().lower()
        if t0 and re.search(r"\b" + re.escape(t0) + r"\b", low):
            problems.append(f"taboo: '{t0}'")
    if "http" in low and "http" not in (json.dumps(facts, default=str) + (reference or "")).lower():
        problems.append("invented URL")
    if low.count("reply yes") > 1:
        problems.append("multiple CTAs")
    if len(EMOJI_RE.findall(body)) > max_emoji:
        problems.append("too many emojis")
    if need_bullets and body.count("•") < need_bullets:
        problems.append("missing bullet points")
    if action_mode and any(q in low for q in QUALIFYING):
        problems.append("qualifying question after a yes")
    if must_mention and not any(m and m.lower() in low for m in must_mention):
        problems.append(f"doesn't address {must_mention[0]}")
    return problems
