"""Round 4 check: NEW trigger types. Scores the new bot and the previous version (baseline/) on the NOVEL set —
30 triggers with trigger types neither version was built for, written blind by a separate helper that never saw
the bot's code. Sealed: aggregates only. Writes COMPARE.md.

Run:  JUDGE_API_KEY="your-key" python3 path/to/scripts/eval_compare.py      (~20-25 min)
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if not os.environ.get("JUDGE_API_KEY"):
    sys.exit('Set JUDGE_API_KEY first:  JUDGE_API_KEY="your-key" python3 scripts/eval_compare.py')
RUNS = [("new_novel", "novel_set", None), ("old_novel", "novel_set", "baseline")]
LABEL = {"new_novel": "NEW (round 4, trigger reader)", "old_novel": "SHIPPED (round 3.1)"}
for tag, st, code in RUNS:
    print(f"\n==================== {tag} ====================")
    env = dict(os.environ, EVAL_SET=st, EVAL_TAG=tag, **({"VERA_CODE": code} if code else {}))
    env.pop("VERA_CODE", None) if not code else None
    subprocess.run([sys.executable, str(ROOT / "scripts" / "eval_real.py")], env=env, check=False)

def load(tag):
    p = ROOT / f"eval_results_{tag}.json"
    return json.load(open(p))["rows"] if p.exists() else []

def stats(rows):
    sc = [r for r in rows if not r["skipped"]]
    real = [r for r in rows if not r.get("judge_error")]
    if not sc:
        return None
    a = lambda rs: round(sum(r["total"] for r in rs) / len(rs), 1) if rs else "-"
    dims = {d: round(sum(r["dims"][d] for r in sc) / len(sc), 1) for d in sc[0]["dims"]}
    return {"avg": a(sc), "n": len(sc), "total": len(rows), "coverage": f"{round(100 * len(sc) / max(1, len(real)))}%", "judge_errors": len(rows) - len(real),
            "placeholder": a([r for r in sc if r["group"] == "placeholder"]), "real": a([r for r in sc if r["group"] == "real-data"]),
            "below30": sum(1 for r in sc if r["total"] < 30), **dims,
            **{f"cat:{c}": a([r for r in sc if r.get("cat") == c]) for c in ("dentists", "salons", "restaurants", "gyms", "pharmacies")},
            **{f"scope:{c}": a([r for r in sc if r.get("scope") == c]) for c in ("merchant", "customer")}}

S = {tag: stats(load(tag)) for tag, _, _ in RUNS}
cols = ["avg", "scope:merchant", "scope:customer", "cat:dentists", "cat:salons", "cat:restaurants", "cat:gyms", "cat:pharmacies", "placeholder", "real", "decision_quality", "specificity", "category_fit", "merchant_fit", "engagement", "below30", "coverage", "judge_errors"]
out = ["# New-trigger check (round 4 vs round 3.1)\n", "NOVEL = 30 blind triggers of types the bot was never built for (26 merchant, 4 customer, 3 empty). References — fresh set: round 3.1 39.5 | held-out 41.5\n",
       "| Metric | " + " | ".join(LABEL[t] for t, _, _ in RUNS) + " |", "|---|---|---|"]
for c in cols:
    out.append(f"| {c} | " + " | ".join(str((S[t] or {}).get(c, "-")) for t, _, _ in RUNS) + " |")
nh, oh = S.get("new_novel"), S.get("old_novel")
if nh and oh:
    d = round(nh["avg"] - oh["avg"], 1)
    out.append(f"\n**NOVEL-set change, round 4 vs round 3.1: {d:+} points** (judge noise is about ±0.4).")
    a_rows, b_rows = load("new_novel"), load("old_novel")
    pairs = [(x["total"], y["total"]) for x, y in zip(a_rows, b_rows) if not x["skipped"] and not y["skipped"]]
    if pairs:
        diff = [x - y for x, y in pairs]
        out.append(f"- Paired (both sent, same trigger): {len(pairs)} cases, mean {sum(diff) / len(diff):+.2f}, "
                   f"wins/ties/losses {sum(v > 0 for v in diff)}/{sum(v == 0 for v in diff)}/{sum(v < 0 for v in diff)}")
    only_old = [y["total"] for x, y in zip(a_rows, b_rows) if x["skipped"] and not x.get("judge_error") and not y["skipped"]]
    if only_old:
        out.append(f"- Skipped by round 4 but sent by round 3.1: {len(only_old)} cases, round 3.1 scored them {round(sum(only_old) / len(only_old), 1)} on average")
dev_md = ROOT / "eval_results_new_dev.md"  # (not run in this round)
if dev_md.exists():
    out += ["\n---\n", dev_md.read_text(encoding="utf-8")]
(ROOT / "COMPARE.md").write_text("\n".join(out), encoding="utf-8")
print("\n" + "\n".join(out[:16]))
print(f"\nSaved: {ROOT / 'COMPARE.md'}  <- send this file back in the chat")
