"""Write submission.jsonl — one line per canonical test pair. Deliberate skips (restraint) are written with an empty body + reason."""
import json, os, sys
ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, ROOT)
from data import load_all, bundle
from bot import compose

cats, ms, cus, trs, pairs = load_all()
U = list(ms.values())
out = os.path.join(ROOT, "submission.jsonl")
skips = 0
with open(out, "w", encoding="utf-8") as f:
    for p in pairs:
        c, m, t, cu = bundle(p, cats, ms, cus, trs)
        r = compose(c, m, t, cu, universe=U)
        import re as _re
        rationale = _re.sub(r"\s*\[(rules-only|ai|label)[^\]]*\]$", "", r["rationale"])
        row = {"test_id": p["test_id"], "body": r["body"], "cta": r["cta"], "send_as": r["send_as"],
               "suppression_key": r["suppression_key"], "rationale": rationale}
        if r.get("skip"):
            skips += 1
            row.update(body="", cta="none", send_as="none", action="skip",
                       rationale="Deliberate restraint — no message sent. " + rationale)
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
print(f"wrote {len(pairs)} lines ({skips} deliberate skips) -> {os.path.abspath(out)}")
