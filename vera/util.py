"""Small formatting + language helpers shared by the composer and conversation handler."""
from __future__ import annotations

import re
from datetime import datetime, date

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ---------------------------------------------------------------- numbers
def inr(value) -> str:
    """4999 -> '₹4,999' (Indian digit grouping)."""
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return f"₹{value}"
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        s = head + "," + tail
    return ("-" if n < 0 else "") + "₹" + s


def num(value) -> str:
    """2410 -> '2,410'."""
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return str(value)
    return f"{n:,}"


def pct(fraction, signed: bool = False, digits: int | None = None) -> str:
    """0.021 -> '2.1%', -0.5 -> '50%' (or '-50%' when signed)."""
    try:
        v = float(fraction) * 100
    except (TypeError, ValueError):
        return str(fraction)
    if digits is None:
        digits = 0 if abs(v) >= 10 or float(v).is_integer() else 1
    out = f"{abs(v):.{digits}f}".rstrip("0").rstrip(".") if digits else f"{abs(v):.0f}"
    if signed:
        return ("+" if v >= 0 else "-") + out + "%"
    return out + "%"


# ---------------------------------------------------------------- dates
def parse_dt(s):
    if not s:
        return None
    if isinstance(s, (datetime, date)):
        return s
    try:
        s2 = str(s).replace("Z", "+00:00")
        return datetime.fromisoformat(s2)
    except ValueError:
        try:
            return datetime.strptime(str(s)[:10], "%Y-%m-%d")
        except ValueError:
            return None


def nice_date(s, with_year: bool = False, with_weekday: bool = False) -> str:
    d = parse_dt(s)
    if not d:
        return str(s)
    out = f"{d.day} {MONTHS[d.month - 1]}"
    if with_year:
        out += f" {d.year}"
    if with_weekday:
        out = f"{WEEKDAYS[d.weekday()]} {out}"
    return out


def nice_time(s) -> str:
    d = parse_dt(s)
    if not isinstance(d, datetime):
        return ""
    h, m = d.hour, d.minute
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d}{suffix}" if m else f"{h12}{suffix}"


def days_between(a, b) -> int | None:
    da, db = parse_dt(a), parse_dt(b)
    if not da or not db:
        return None
    da = da.date() if isinstance(da, datetime) else da
    db = db.date() if isinstance(db, datetime) else db
    return (db - da).days


def humanize(slug: str) -> str:
    """'corporate_bulk_thali_package' -> 'corporate bulk thali package'."""
    return str(slug or "").replace("_", " ").replace("-", " ").strip()


# ---------------------------------------------------------------- language
HINDI_BELT_BLOCKERS = {"ta", "kn", "ml"}  # south-Indian merchants: English reads more natural than Hinglish


def merchant_lang(merchant: dict, category: dict | None = None) -> str:
    """Return 'hinglish' or 'en' for merchant-facing copy.
    The category's own voice rule wins: code_mix 'english_primary…' (gyms) -> English."""
    if str(((category or {}).get("voice") or {}).get("code_mix", "")).startswith("english_primary"):
        return "en"
    langs = [str(l).lower() for l in (merchant.get("identity", {}).get("languages") or [])]
    if "hi" in langs and not (HINDI_BELT_BLOCKERS & set(langs)):
        return "hinglish"
    return "en"


def customer_lang(customer: dict | None, merchant: dict) -> str:
    if not customer:
        return merchant_lang(merchant)
    pref = str(customer.get("identity", {}).get("language_pref") or "").lower()
    if pref in ("hi", "hindi"):
        return "hindi"
    if "hi" in pref.split("-") or pref.startswith("hi"):
        return "hinglish"
    return "en"


DEVANAGARI = re.compile(r"[ऀ-ॿ]")
ROMAN_HINDI_WORDS = {
    "hai", "hain", "nahi", "nahin", "kya", "karo", "kar", "karna", "mujhe", "aap", "aapka", "aapki", "haan",
    "ji", "bhai", "theek", "thik", "chahiye", "abhi", "baad", "mein", "hoga", "kaise", "kitna", "kyun",
    "matlab", "accha", "acha", "chalega", "batao", "bhejo", "judna", "karwana", "samajh", "zaroor", "dijiye",
    "shukriya", "dhanyavaad", "bilkul", "wala", "wali", "raha", "rahi", "sakte", "hum", "tum",
}


def detect_msg_lang(text: str) -> str:
    """Rough per-turn language detection: 'hindi' | 'hinglish' | 'en'."""
    if not text:
        return "en"
    if DEVANAGARI.search(text):
        return "hindi"
    words = re.findall(r"[a-zA-Z]+", text.lower())
    if not words:
        return "en"
    hits = sum(1 for w in words if w in ROMAN_HINDI_WORDS)
    if hits >= 2 or (hits >= 1 and len(words) <= 4):
        return "hinglish"
    return "en"


def L(lang: str, en: str, hi: str | None = None) -> str:
    """Pick the English or Hinglish variant of a sentence."""
    if lang in ("hinglish", "hindi") and hi:
        return hi
    return en


# ---------------------------------------------------------------- names
def owner_first(merchant: dict) -> str:
    ident = merchant.get("identity", {})
    first = ident.get("owner_first_name")
    if first:
        return re.sub(r"^Dr\.?\s*", "", str(first)).strip()
    name = ident.get("name", "")
    m = re.match(r"Dr\.?\s+(\w+)", name)
    return m.group(1) if m else ""


def salutation(merchant: dict, category_slug: str) -> str:
    first = owner_first(merchant)
    if category_slug == "dentists":
        return f"Dr. {first}" if first else "Doctor"
    return first or merchant.get("identity", {}).get("name", "there")


def customer_first(customer: dict | None) -> str:
    if not customer:
        return ""
    name = str(customer.get("identity", {}).get("name") or "")
    if not name or name.startswith("("):
        return ""
    return name


def parent_name(customer: dict | None) -> str | None:
    """'Karthik (parent: Sumitra)' -> 'Sumitra'."""
    if not customer:
        return None
    m = re.search(r"parent:\s*([^)]+)\)", str(customer.get("identity", {}).get("name") or ""))
    return m.group(1).strip() if m else None


def child_name(customer: dict | None) -> str:
    name = str((customer or {}).get("identity", {}).get("name") or "")
    return name.split("(")[0].strip()


def name_from_customer_id(cid: str | None) -> str:
    """'c_001_priya_for_m001' -> 'Priya'. Used only when the customer context was never pushed."""
    if not cid:
        return ""
    m = re.match(r"c_\d+_([a-z]+)", cid)
    return m.group(1).capitalize() if m else ""


ABBREV = r"(?<!\bDr\.)(?<!\bMr\.)(?<!\bMrs\.)(?<!\bMs\.)(?<!\bvs\.)(?<!\bSt\.)(?<!\be\.g\.)(?<!\bi\.e\.)(?<!\b[A-Z]\.)"


def split_sentences(text: str) -> list[str]:
    """Split on sentence ends, but not after Dr./Mr./vs./initials like 'R.'."""
    text = str(text or "").strip()
    parts = re.split(ABBREV + r"(?<=[.!?])\s+", text)
    return [x.strip() for x in parts if x.strip()]


def first_sentence(text: str) -> str:
    parts = split_sentences(text)
    return parts[0] if parts else ""


def active_offers(merchant: dict) -> list[str]:
    return [o.get("title") for o in merchant.get("offers") or [] if o.get("status") == "active" and o.get("title")]


def signal_value(merchant: dict, prefix: str):
    """signals like 'stale_posts:22d' -> '22d' for prefix 'stale_posts'."""
    for s in merchant.get("signals") or []:
        s = str(s)
        if s == prefix:
            return True
        if s.startswith(prefix + ":"):
            return s.split(":", 1)[1]
    return None
