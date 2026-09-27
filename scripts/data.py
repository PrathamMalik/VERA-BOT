"""Load the expanded dataset (run dataset/generate_dataset.py --out dataset/expanded first)."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXP = ROOT / "dataset" / "expanded"


def load_dir(name):
    out = {}
    for f in sorted((EXP / name).glob("*.json")):
        d = json.loads(f.read_text())
        key = d.get("slug") or d.get("merchant_id") if name in ("categories", "merchants") else d.get("customer_id") or d.get("id")
        if name == "categories":
            key = d["slug"]
        elif name == "merchants":
            key = d["merchant_id"]
        elif name == "customers":
            key = d["customer_id"]
        else:
            key = d["id"]
        out[key] = d
    return out


def load_all():
    return (load_dir("categories"), load_dir("merchants"), load_dir("customers"), load_dir("triggers"),
            json.loads((EXP / "test_pairs.json").read_text())["pairs"])


def bundle(pair, cats, merchants, customers, triggers):
    t = triggers[pair["trigger_id"]]
    m = merchants[pair["merchant_id"]]
    c = cats[m["category_slug"]]
    cu = customers.get(pair.get("customer_id")) if pair.get("customer_id") else None
    return c, m, t, cu
