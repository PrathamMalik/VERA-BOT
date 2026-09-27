"""Run the official judge_simulator.py flows with a mock LLM (checks protocol compatibility, not quality)."""
import json, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import judge_simulator as js

class Mock(js.LLMProvider):
    def name(self): return "mock"
    def complete(self, prompt, system=None):
        return json.dumps({k: 7 for k in ["specificity", "category_fit", "merchant_fit", "decision_quality", "engagement_compulsion"]} |
                          {"specificity_reason": "mock", "category_fit_reason": "mock", "merchant_fit_reason": "mock",
                           "decision_quality_reason": "mock", "engagement_reason": "mock", "hint": "mock"})

js.DATASET_DIR = js.Path(os.path.join(os.path.dirname(__file__), "..", "dataset"))
runner_cls = [v for v in vars(js).values() if isinstance(v, type) and hasattr(v, "_auto_reply")][0]
r = runner_cls(Mock())
ok = r.run(sys.argv[1] if len(sys.argv) > 1 else "all")
print("RESULT:", ok)
