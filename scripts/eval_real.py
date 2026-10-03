"""Score the bot under REAL-test conditions with magicpin's own judge rubric.

Why: the practice judge only uses 25 easy triggers, no customer profiles and today's date.
The real test uses mostly empty (placeholder) triggers, customer profiles and a simulated April clock.
This script composes messages for a fixed, stratified sample of 40 triggers (eval/eval_set.json)
and scores each one with the judge prompt from judge_simulator.py.

Run (one line, from anywhere):
  JUDGE_API_KEY="your-key" python3 path/to/scripts/eval_real.py

Writes eval_results.md in the project folder; send that file back for analysis.
"""
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
CODE = os.environ.get("VERA_CODE")            # optional: score another version of the bot (e.g. baseline/)
if CODE:
    sys.path.insert(0, str((ROOT / CODE).resolve()))
SET = os.environ.get("EVAL_SET", "eval_set")  # eval_set = dev set (used to design fixes) | holdout_set = sealed
TAG = os.environ.get("EVAL_TAG", "")
SEALED = SET in ("holdout_set", "fresh_set", "novel_set")                 # held-out: aggregate scores only, no message texts (so we can't tune to it)

KEY = os.environ.get("JUDGE_API_KEY", "").strip()
if not KEY:
    sys.exit("Set JUDGE_API_KEY first, e.g.  JUDGE_API_KEY=\"your-key\" python3 scripts/eval_real.py")
MOCK = KEY == "mock"  # offline self-test: fixed judge scores, bot in rules-only mode
os.environ.setdefault("LLM_PROVIDER", "gemini")
os.environ.setdefault("LLM_API_KEY", "" if MOCK else KEY)                       # the bot's own AI calls
os.environ.setdefault("LLM_MODEL", "gemini-3.5-flash-lite")
os.environ.setdefault("LLM_RPM", "12")

import judge_simulator as js                                    # magicpin's rubric + prompt, unchanged
from vera.composer import compose
from vera.quality import floor_check

JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "gemini-3.1-flash-lite")
class _Mock(js.LLMProvider):
    def name(self): return "mock"
    def complete(self, prompt, system=None):
        return json.dumps({k: 7 for k in ["specificity", "category_fit", "merchant_fit", "decision_quality", "engagement_compulsion"]} | {"hint": "mock"})
judge = js.LLMScorer(_Mock() if MOCK else js.GeminiProvider(KEY, JUDGE_MODEL), None)

data = json.load(open(ROOT / "eval" / f"{SET}.json"))
NOW = data["now"]
cats, ms, cus = data["categories"], data["merchants"], data["customers"]
universe = list(ms.values())

rows = []
t0 = time.time()
print(f"Scoring {len(data['triggers'])} triggers | bot model {os.environ['LLM_MODEL']} | judge {JUDGE_MODEL} | clock {NOW}\n")
for i, t in enumerate(data["triggers"], 1):
    m = ms[t["merchant_id"]]
    c = cats[m["category_slug"]]
    cu = cus.get(t.get("customer_id")) if t.get("customer_id") else None
    kind_group = "placeholder" if t["payload"].get("placeholder") else "real-data"
    try:
        msg = compose(c, m, t, cu, use_llm=True, universe=universe, now=t.get("_now") or NOW)
    except Exception as e:
        msg = {"skip": True, "body": "", "rationale": f"CRASH {type(e).__name__}: {e}"}
    if msg.get("skip") or not msg.get("body"):
        rows.append({"id": t["id"], "kind": t["kind"], "cat": m["category_slug"], "src": t.get("_src", ""), "scope": t.get("scope"), "group": kind_group, "skipped": True, "why": msg.get("rationale", "")})
        print(f"[{i:2}/{len(data['triggers'])}] SKIP  {t['id']}")
        continue
    shape = "approval draft" if "> " in msg["body"] else ("customer-facing" if msg.get("send_as") == "merchant_on_behalf" else "merchant-facing")
    fails = floor_check(msg["body"], m, t, cu, msg.get("send_as"))
    s = judge.score({"body": msg["body"], "cta": msg.get("cta"), "send_as": msg.get("send_as")}, c, m, t, cu)
    if "scoring failed" in (s.hint or "") and not MOCK:   # judge error (rate limit / bad JSON): wait, retry once
        time.sleep(30)
        s = judge.score({"body": msg["body"], "cta": msg.get("cta"), "send_as": msg.get("send_as")}, c, m, t, cu)
    if "scoring failed" in (s.hint or ""):
        rows.append({"id": t["id"], "kind": t["kind"], "group": kind_group, "skipped": True, "judge_error": True, "why": "judge failed twice — excluded"})
        print(f"[{i:2}/{len(data['triggers'])}] JUDGE ERROR (excluded)  {t['id']}")
        continue
    rows.append({"id": t["id"], "kind": t["kind"], "cat": m["category_slug"], "src": t.get("_src", ""), "scope": t.get("scope"), "group": kind_group, "shape": shape, "skipped": False,
                 "source": msg.get("_source"), "floor": fails, "total": s.total,
                 "dims": {"specificity": s.specificity, "category_fit": s.category_fit, "merchant_fit": s.merchant_fit,
                          "decision_quality": s.decision_quality, "engagement": s.engagement_compulsion},
                 "reasons": {"specificity": s.specificity_reason, "category_fit": s.category_fit_reason, "merchant_fit": s.merchant_fit_reason,
                             "decision_quality": s.decision_quality_reason, "engagement": s.engagement_reason},
                 "hint": s.hint, "body": msg["body"]})
    print(f"[{i:2}/{len(data['triggers'])}] {s.total:2}/50  {'floor OK ' if not fails else 'floor FAIL'}  {t['id']}")
    time.sleep(0 if MOCK else 4.2)  # stay under the free judge limit (15/min)

scored = [r for r in rows if not r["skipped"]]
def avg(rs):
    return round(sum(r["total"] for r in rs) / len(rs), 1) if rs else None

groups = defaultdict(list)
for r in scored:
    groups[f"trigger: {r['group']}"].append(r)
    groups[f"shape: {r['shape']}"].append(r)
    groups["floor: pass" if not r["floor"] else "floor: FAIL"].append(r)
    groups[f"source: {r['source']}"].append(r)
    groups[f"category: {r['cat']}"].append(r)

out = [f"# Eval results — {TAG or SET} ({'SEALED: aggregates only' if SEALED else 'dev set'})\n",
       f"- Clock: {NOW} | bot model: {os.environ['LLM_MODEL']} | judge: {JUDGE_MODEL} | took {int(time.time() - t0)}s",
       f"- Triggers: {len(rows)} | sent: {len(scored)} | skipped: {len(rows) - len(scored)} | coverage: {round(100 * len(scored) / max(1, len(rows)))}%",
       f"- **Average: {avg(scored)}/50**\n", "## By group\n", "| Group | Messages | Avg /50 |", "|---|---|---|"]
for k in sorted(groups):
    out.append(f"| {k} | {len(groups[k])} | {avg(groups[k])} |")
dim_avg = {d: round(sum(r['dims'][d] for r in scored) / len(scored), 1) for d in scored[0]["dims"]} if scored else {}
out += ["\n## Average per dimension\n", "| Dimension | Avg /10 |", "|---|---|"] + [f"| {d} | {v} |" for d, v in dim_avg.items()]
if SEALED:
    out += ["\n_Held-out set: message texts and per-trigger scores are deliberately not shown._"]
out += [] if SEALED else ["\n## All messages (lowest first)\n", "| Score | Trigger | Group | Shape | Floor |", "|---|---|---|---|---|"]
for r in ([] if SEALED else sorted(scored, key=lambda r: r["total"])):
    out.append(f"| {r['total']} | {r['id']} | {r['group']} | {r['shape']} | {', '.join(r['floor']) or 'pass'} |")
skipped = [r for r in rows if r["skipped"]]
if skipped and not SEALED:
    out += ["\n## Skipped\n"] + [f"- {r['id']}: {r['why'][:160]}" for r in skipped]
out += [] if SEALED else ["\n## Lowest 10 in detail\n"]
for r in ([] if SEALED else sorted(scored, key=lambda r: r["total"])[:10]):
    out += [f"### {r['total']}/50 — {r['id']}", "```", r["body"], "```",
            "Scores: " + ", ".join(f"{d} {v}" for d, v in r["dims"].items())]
    out += [f"- {d}: {why}" for d, why in r["reasons"].items() if why]
    out += [f"- hint: {r['hint']}", f"- floor: {', '.join(r['floor']) or 'pass'}\n"]

name = f"eval_results{'_' + TAG if TAG else ''}"
path = ROOT / f"{name}.md"
path.write_text("\n".join(out), encoding="utf-8")
if SEALED:
    rows = [{k: v for k, v in r.items() if k in ("cat", "src", "scope", "group", "shape", "skipped", "judge_error", "total", "dims", "floor", "source")} for r in rows]
json.dump({"tag": TAG or SET, "set": SET, "rows": rows}, open(ROOT / f"{name}.json", "w"), ensure_ascii=False, indent=1)
print(f"\nAverage {avg(scored)}/50 over {len(scored)} messages ({len(rows) - len(scored)} skipped)")
for k in sorted(groups):
    print(f"  {k:28} {avg(groups[k])}  (n={len(groups[k])})")
print(f"\nSaved: {path}\nSend that file (eval_results.md) back in the chat.")
