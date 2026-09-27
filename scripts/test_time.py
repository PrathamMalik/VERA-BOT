"""Day counts follow the judge's clock; past events are skipped or reworded."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from data import load_all, bundle
from vera.templates import draft_for
cats, ms, cus, trs, pairs = load_all(); U = list(ms.values()); P = {p["test_id"]: p for p in pairs}
F = []
def d(tid, now):
    c, m, t, cu = bundle(P[tid], cats, ms, cus, trs); return draft_for(c, m, t, cu, universe=U, now=now)
def ok(c, l):
    print(("PASS " if c else "FAIL ") + l); F.append(l) if not c else None
ok("188 din" in d("T18", "2026-04-26T10:00:00Z").body, "festival: 188 days on 26 Apr")
ok("30 din" in d("T18", "2026-10-01T10:00:00Z").body, "festival: 30 days on 1 Oct (package pitch)")
ok(d("T18", "2026-11-05T10:00:00Z").skip, "festival: skipped after the date")
ok("tonight" in d("T21", "2026-04-26T08:00:00Z").body, "IPL: 'tonight' on match day")
ok("on Sun 26 Apr" in d("T21", "2026-04-24T08:00:00Z").body, "IPL: date, not 'tonight', two days before")
ok(d("T21", "2026-04-28T08:00:00Z").skip, "IPL: skipped after the match")
ok("93 days" in d("T13", "2026-06-01T10:00:00Z").body, "lapsed customer: days counted from last visit")
ok("Slot" not in d("T28", "2026-11-10T10:00:00Z").body, "recall: past slots never offered")
print("TIME OK" if not F else f"{len(F)} FAILED"); sys.exit(1 if F else 0)
