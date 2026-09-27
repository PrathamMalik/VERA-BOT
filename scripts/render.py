"""Print the rules-only version of every test message (or the ones named on the command line)."""
import sys; sys.path.insert(0, 'scripts'); sys.path.insert(0, '.')
from data import load_all, bundle
from vera.templates import draft_for
cats, ms, cus, trs, pairs = load_all()
U = list(ms.values())
only = set(sys.argv[1:])
for p in pairs:
    if only and p['test_id'] not in only:
        continue
    c, m, t, cu = bundle(p, cats, ms, cus, trs)
    d = draft_for(c, m, t, cu, universe=U)
    print(f"{p['test_id']} {t['kind']} | {d.send_as} {d.cta} {'SKIP' if d.skip else len(d.body)} | ai={d.ai.kind if d.ai else '-'}")
    print(d.body if not d.skip else '(skipped) ' + d.rationale)
    print()
