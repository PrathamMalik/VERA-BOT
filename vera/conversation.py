"""Multi-turn handler for /v1/reply — implements "Conversation behaviour" in DECISIONS.md.

Routing order (first match wins):
  ended conversation -> end | START -> unblock | opt-out -> confirm once + block forever | auto-reply -> nudge once, end on repeat + flag
  | CALL -> human handoff | not interested -> polite close + pause that topic | angry -> apologise + exit option (2nd time: end + block)
  | not now -> acknowledge + wait until the time they named | yes -> deliver + approve + next step (never re-qualify)
  | off-topic -> official facts from our data (with disclaimer) or boundary only | question -> answer from data, else honest + data fact + CALL
Also: max 5 bot messages, or end after 3 in a row with no real reply; sticky profile language.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from . import llm
from .peers import case_patterns
from .store import Store
from .templates import Ctx, bullets, _price
from .util import L, detect_msg_lang, merchant_lang, owner_first, salutation, customer_first, humanize, first_sentence, parse_dt, inr, nice_date
from .validator import check

AUTO_PATTERNS = [
    r"thank(s| you)[\w\s,!]{0,25} for (contacting|reaching|your (message|enquiry|inquiry)|messaging|writing)",
    r"(will|shall) (get back|respond|revert|reply|contact you|be in touch)", r"our (team|executive|representative)s? will",
    r"automated (message|reply|response|assistant)", r"\bauto[- ]?reply\b", r"i am an? (automated|virtual) assistant",
    r"(currently|presently) (unavailable|closed|away|out of office)", r"out of (the )?office", r"business hours",
    r"we are (closed|away)", r"(jaankari|jankari) ke liye", r"team tak pahuncha", r"sampark karne ke liye", r"hum jald hi",
    r"aapka sandesh", r"this is an automated",
]
OPT_OUT = [r"\bstop\b", r"unsubscribe", r"don'?t (message|text|contact|msg)", r"do not (message|text|contact)", r"remove me",
           r"\bblock(ing)?\b", r"leave me alone", r"no more (messages|msgs)", r"mat bhejo", r"band karo", r"message mat"]
ABUSE = [r"useless", r"stupid", r"idiot", r"\bfraud", r"\bscam", r"nonsense", r"bakwas", r"rubbish", r"waste of (my )?time",
         r"shut up", r"\bspam", r"pagal", r"bekaar", r"irritat", r"annoying", r"pareshan"]
NOT_INTERESTED = [r"not interested", r"no interest", r"\bnahi chahiye\b", r"no thanks", r"no,? thank", r"don'?t need", r"interest nahi"]
DEFER = [r"\blater\b", r"\bbusy\b", r"baad mein", r"\bcall me later\b", r"tomorrow", r"\bkal\b", r"next week", r"in a meeting", r"not now",
         r"abhi nahi", r"thodi der", r"give me (some )?time", r"let me think", r"soch ?ke", r"think (about|over) it", r"will think",
         r"sochna", r"get back to you", r"after \d", r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", r"shaam", r"evening"]
COMMIT = [r"\b(yes|yeah|yep|yup|haan|han|haa|ji)\b", r"\bsure\b", r"\bok(ay)?\b", r"go ahead", r"let'?s do it", r"\bdo it\b", r"please do",
          r"\bkar do\b", r"\bkardo\b", r"\bkaro\b", r"chalega", r"\bproceed\b", r"\bconfirm", r"send (it|me|the)", r"want to join",
          r"judna hai", r"\bstart\b", r"sounds good", r"\bdone\b", r"\bbook\b", r"\bperfect\b", r"\bagreed?\b"]
STRONG_COMMIT = [r"let'?s (do|start|go)", r"go ahead", r"\bdo it\b", r"please do", r"\bkar do\b", r"\bkardo\b", r"want to join",
                 r"judna hai", r"\bproceed\b", r"\b(yes|haan|han)\b", r"what'?s next", r"whats next", r"next step", r"sign me up", r"chalega"]
OFF_TOPIC = [r"\bgst\b", r"\bitr\b", r"income tax", r"\btax(es)?\b", r"\bloan\b", r"insurance", r"\bvisa\b", r"lawyer", r"legal notice",
             r"electricity bill", r"\bcibil\b", r"passport", r"aadhaar", r"\bpan card\b", r"accounting"]
COMPLEX = [r"\bfile\b", r"\bfiling\b", r"\breturns?\b", r"\bfill\b", r"\bregister\b", r"do (it|this|my)", r"handle my", r"calculate my"]
QUESTION = [r"\?", r"^(what|how|why|when|where|which|who|can|could|is|are|does|will)\b", r"\b(kya|kaise|kitna|kitne|kab|kyun|kaun)\b",
            r"how much", r"\bprice\b", r"\bcost\b", r"\bcharges?\b"]
THANKS_ONLY = re.compile(r"^\s*(ok(ay)?\s*)?(thanks|thank you|thx|ty|dhanyavaad|shukriya)[\s!.🙏]*$", re.I)
ACK_ONLY = re.compile(r"^\s*(hmm+|k|ok|okay|👍|\?|\.+|acha|accha)\s*$", re.I)
LANG_ASK = {"hinglish": [r"hindi (mein|me|please)", r"in hindi", r"hindi mai"], "en": [r"in english", r"english please", r"english mein", r"english me"]}
QUALIFYING = ["would you", "do you", "can you tell", "what if", "how about"]

STRATEGIES = {
    "low_ctr": ["Fresh photos of the place and the team", "A clear first line in your profile saying what you're best at", "Reply to every review — it shows you're active"],
    "low_calls": ["Put one service+price offer live", "Post on Google every week", "Check your phone number and hours are correct"],
    "low_views": ["Post on Google every week", "Add more real photos", "Get the profile verified if it isn't"],
    "low_directions": ["Check your map pin and address", "Add a photo of the entrance or a nearby landmark"],
    "drop_calls": ["Put one service+price offer live", "Post something new this week so the profile looks active"],
    "drop_views": ["Post something new this week", "Add fresh photos"],
    "theme_wait_time": ["Staggered appointment slots at peak hours", "A 'running late' WhatsApp to people who are waiting", "Show your quieter hours on the profile"],
    "theme_saturday_wait": ["Take bookings for Saturday peaks", "Nudge regulars toward weekday slots"],
    "theme_delivery_late": ["Quote realistic delivery times at peak hours", "Keep a prep buffer on busy nights", "Reply to late-delivery reviews with the fix you've made"],
    "theme_weekend_busy": ["Nudge regulars toward weekdays with a weekday-only offer", "Take table bookings for weekend peaks"],
    "theme_morning_crowd": ["Add a class or slot just before the rush", "Show quieter hours on your profile"],
    "low_retention": ["Remind customers when their next visit is due", "A small perk for repeat visits"],
    "high_churn": ["Check in with members who miss a week", "Monthly member challenges to keep people coming"],
    "low_trial_conv": ["Follow up within a day of every trial", "Offer a clear first-month plan right after the trial"],
    "unverified": ["Verify your Google profile (postcard or phone call)"],
    "no_offer": ["Put one service+price offer live"],
}
THEME_DEFAULT = ["Reply publicly to each review with what you've changed", "Fix the root cause at the busiest hours", "Ask happy regulars for a review"]


def _any(pats, text):
    return any(re.search(p, text, re.I) for p in pats)


def _norm(text):
    return re.sub(r"[^a-z0-9ऀ-ॿ ]+", "", (text or "").lower()).strip()


def classify(text: str, repeat_count: int) -> str:
    t = (text or "").strip()
    if not t:
        return "empty"
    if re.fullmatch(r"\s*start\s*", t, re.I):
        return "restart"
    if _any(AUTO_PATTERNS, t) or (repeat_count >= 2 and len(t.split()) >= 4):
        return "auto_reply"
    if _any(OPT_OUT, t):
        return "opt_out"
    if re.fullmatch(r"\s*call( me)?\s*[.!]*\s*", t, re.I):
        return "call"
    if _any(NOT_INTERESTED, t):
        return "not_interested"
    if _any(ABUSE, t):
        return "hostile"
    if _any(OFF_TOPIC, t):
        return "off_topic"
    if THANKS_ONLY.match(t):
        return "thanks"
    if _any(DEFER, t) and not _any([r"\b(yes|haan|sure)\b"], t):
        return "defer"
    if "?" in t and re.search(r"\bbut\b|cost|price|charge|how much|kitna|kitne|paisa|fee|recall|batch|safe", t, re.I):
        return "question"
    if _any(STRONG_COMMIT, t) or (_any(COMMIT, t) and "?" not in t):
        return "commit"
    if _any(QUESTION, t):
        return "question"
    if ACK_ONLY.match(t):
        return "ack"
    return "engaged"


# ------------------------------------------------------------------ time parsing for "not now"
def wait_seconds_for(msg: str, received_at: str | None) -> tuple[int, str]:
    now = parse_dt(received_at) if received_at else None
    if not isinstance(now, datetime):
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    ist = now.astimezone(timezone(timedelta(hours=5, minutes=30)))
    m = re.search(r"(\d+)\s*(hours?|hrs?|ghante|ghanta)", msg, re.I)
    if m:
        return int(m.group(1)) * 3600, f"in {m.group(1)} hours"
    m = re.search(r"\bafter (\d{1,2})\s*(am|pm)?", msg, re.I)
    if m:
        h = int(m.group(1)) % 12 + (12 if (m.group(2) or "pm").lower() == "pm" else 0)
        target = ist.replace(hour=h, minute=0, second=0, microsecond=0)
        if target <= ist:
            target += timedelta(days=1)
        return int((target - ist).total_seconds()), f"after {m.group(1)}{(m.group(2) or 'pm').lower()}"
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    m = re.search(r"\b(" + "|".join(days) + r")\b", msg, re.I)
    if m:
        delta = (days.index(m.group(1).lower()) - ist.weekday()) % 7 or 7
        target = (ist + timedelta(days=delta)).replace(hour=10, minute=0, second=0, microsecond=0)
        return int((target - ist).total_seconds()), f"on {m.group(1).capitalize()}"
    if re.search(r"next week", msg, re.I):
        return 7 * 86400, "next week"
    if re.search(r"evening|shaam", msg, re.I):
        target = ist.replace(hour=18, minute=0, second=0, microsecond=0)
        if target <= ist:
            target += timedelta(days=1)
        return int((target - ist).total_seconds()), "this evening"
    if re.search(r"tomorrow|\bkal\b", msg, re.I):
        return 86400, "tomorrow"
    return 7200, "in a couple of hours"


# ------------------------------------------------------------------ follow-ups after a YES (deliver + approve + next step)
def _strategies(types, c: Ctx, lang: str) -> list[str]:
    """AI-written strategies (no numbers) with a fixed library as fallback."""
    lib = []
    for t in types:
        lib += STRATEGIES.get(t) or (THEME_DEFAULT if t.startswith("theme_") else [])
    lib = list(dict.fromkeys(lib))[:5]
    if llm.enabled() and types:
        try:
            out = llm.parse_json(llm.complete(
                "You write practical small-business advice for Indian merchants. Output only JSON.",
                json.dumps({"TASK": f"Give 3-5 short, common, practical strategies {c.slug} use to fix these weak spots. NO numbers, no promises, no brand names.",
                            "WEAK_SPOTS": types, "CATEGORY": c.slug, "FORMAT": '{"strategies": ["...", "..."]}'}), max_tokens=300, t=6))
            ai = [s for s in out.get("strategies", []) if isinstance(s, str) and not check(s, {}, no_numbers=True, max_len=160, max_emoji=0, action_mode=True)]
            if len(ai) >= 2:
                return ai[:5]
        except Exception:
            pass
    return lib


def build_followup(conv: dict, c: Ctx, lang: str) -> tuple[str, str]:
    f = conv.get("followup") or {}
    t = f.get("type", "")
    who = c.sal
    if t == "positives_strategies":
        pos = f.get("positives") or []
        strat = _strategies(f.get("weak_types") or [], c, lang)
        parts = []
        if pos:
            parts += ["*Working for you*", bullets(pos)]
        parts += [f"*Common ways {f.get('noun', 'businesses')} fix these*", bullets(strat)]
        return "\n".join(parts), L(lang, "Which one should we start with? I'll set it up.", "Kis se shuru karein? Main set up kar deti hoon.")
    if t == "abstract":
        d = c.digest(f.get("item_id"))
        if d:
            return (f"📄 *{d.get('title')}* — {d.get('source')}\n{d.get('summary', '')}\n\nPatient WhatsApp draft:\n\"{first_sentence(d.get('summary', ''))} "
                    f"Ask us at your next visit whether this applies to you.\"",
                    L(lang, "Reply YES to save it as a template. After that I'll draft a Google post on the same topic.",
                      "YES reply karein toh template save kar dungi. Uske baad isi topic pe Google post."))
    if t == "checklist":
        d = c.digest(f.get("item_id"))
        if d:
            return ("*Audit checklist*\n" + bullets([d.get("actionable", "Review the circular"), "Note the equipment/process in use today",
                                                     "Update and sign the SOP", "Re-check before the deadline"]),
                    L(lang, "Reply YES and I'll remind you 2 weeks before the deadline. Next: a short note for your staff.",
                      "YES reply karein, deadline se 2 hafte pehle yaad dila dungi. Uske baad staff ke liye ek chhota note."))
    if t == "registration":
        d = c.digest(f.get("item_id"))
        if d:
            return (bullets([d.get("title"), d.get("source"), nice_date(d.get("date"), with_weekday=True), d.get("actionable")]),
                    L(lang, "Reply YES and I'll send a reminder on the day.", "YES reply karein, us din reminder bhej dungi."))
    if t == "recall_send":
        return (L(lang, f"Done — the recall message is queued for your {f.get('molecule')} customers.", f"Ho gaya — {f.get('molecule')} customers ke liye recall message queue kar diya."),
                L(lang, "Next: share strips sold from these batches, strips on shelf, cost price and GST rate, and I'll calculate your exposure.",
                  "Next: in batches ke bik chuke strips, shelf pe strips, cost price aur GST rate bhejiye — main exposure calculate kar dungi."))
    if t in ("enquiry_reply",):
        offers = ", ".join(c.offers[:2])
        return (f"Enquiry reply:\n\"Thanks for asking about {c.mname}! " + (f"Right now: {offers}. " if offers else "") + "Shall we book you in this week?\"",
                L(lang, "Reply YES and I'll save it as a WhatsApp quick-reply. Next: a Google post to keep the calls coming.",
                  "YES reply karein, WhatsApp quick-reply save kar dungi. Uske baad ek Google post."))
    if t == "offer_live":
        return (f"Here's the offer: \"{f.get('offer')}\" — ready for your Google profile.",
                L(lang, "Reply YES to publish it (Google usually shows changes within 24-48 hours). Next: a post announcing it.",
                  "YES reply karein, publish kar dungi (Google pe 24-48 ghante lagte hain). Uske baad ek announcement post."))
    if t == "peer_breakdown":
        diffs = f.get("differences") or []
        costs = []
        if any("unverified" in d for d in diffs):
            costs.append("Google verification: free (postcard or phone call)")
        costs.append("Anything beyond that (e.g. an offer): share your average bill size and monthly customers and I'll calculate it")
        return ("*What's different about them (from their profiles)*\n" + bullets(diffs) + "\n\n*What switching would cost*\n" + bullets(costs),
                L(lang, "Reply YES and I'll start with the free fix.", "YES reply karein, pehle free wala fix karti hoon."))
    if t == "review_request":
        return (f"Review-request message:\n\"Thanks for choosing {c.mname} 🙏 If you enjoyed your visit, a quick Google review helps us a lot. Thank you!\"",
                L(lang, "Reply YES and I'll set it to go out to your regulars this week. Next: replies to your latest reviews.",
                  "YES reply karein, is hafte regulars ko bhej dungi. Uske baad latest reviews ke replies."))
    if t in ("fix_first", "verification"):
        fixes = f.get("fixes") or (["unverified"] if t == "verification" else [])
        steps = []
        if "unverified" in fixes or "verified" in fixes or t == "verification":
            steps += ["Open your Google Business Profile → \"Get verified\"", "Choose phone call or postcard", "Send me the code when it arrives"]
        if "no_offer" in fixes or "has_offer" in fixes:
            sug = c.catalog_offer()
            steps.append(f"Offer to put live: \"{sug}\"" if sug else "Pick one service+price offer to put live")
        return ("Here's the plan:\n" + bullets(steps or ["I'll review your profile and send the top fix"]),
                L(lang, "Reply YES to go. Once these are done, I'll check back on the renewal.", "YES reply karein. Yeh ho jaye, phir renewal pe baat karenge."))
    if t == "renewal_link":
        return (L(lang, "Sending the renewal link right after this message.", "Renewal link is message ke turant baad bhej rahi hoon."),
                L(lang, "Next: I'll fix the top gap on your profile.", "Uske baad profile ka sabse bada gap fix karungi."))
    if t == "comeback":
        return (f"Comeback message for your {f.get('n') or ''} lapsed customers:\n\"We miss you at {c.mname}! Come by this week — we'd love to see you again.\"".replace("  ", " "),
                L(lang, "Reply YES and I'll send it. Share your average bill size and I'll work out what these customers are worth each month.",
                  "YES reply karein, bhej dungi. Average bill batayein toh in customers ki monthly value bhi calculate kar dungi."))
    if t == "price_reply":
        return ("", "")  # handled by engaged path (their answer) — see respond()
    if t in ("package", "plan_post", "seasonal_post", "heat_post", "trend_post", "ipl_posts", "news_update"):
        return (_post(c, conv, t), L(lang, "Reply YES and I'll publish it, or tell me what to change. After that: a WhatsApp version for your regulars.",
                                      "YES reply karein toh publish kar dungi, ya batayein kya badalna hai. Uske baad regulars ke liye WhatsApp version."))
    if t == "reminder_set":
        return (L(lang, "Done ✅ I'll remind you 6 weeks before.", "Ho gaya ✅ 6 hafte pehle yaad dila dungi."), "")
    if t == "send_customer_reminder":
        return (L(lang, f"Done ✅ Reminder queued for {f.get('who')} from your number.", f"Ho gaya ✅ {f.get('who')} ke liye aapke number se reminder queue kar diya."),
                L(lang, "I'll tell you when they reply.", "Unka reply aate hi bata dungi."))
    return (_post(c, conv, t), L(lang, "Reply YES and I'll publish it, or tell me what to change.", "YES reply karein toh publish kar dungi, ya batayein kya badalna hai."))


def _post(c: Ctx, conv: dict, t: str) -> str:
    pos = c.pos_theme()
    focus = conv.get("focus") or humanize(c.payload.get("intent_topic", ""))
    lines = [f"📍 {c.mname}" + (f", {c.locality}" if c.locality else "")]
    if focus:
        lines.append(f"{focus[0].upper() + focus[1:]} — ask us about it this week.")
    if pos and pos.get("common_quote"):
        lines.append(f"What our customers say: \"{pos['common_quote']}\"")
    if c.offers:
        lines.append(f"This week: {c.offers[0]}.")
    lines.append("Call or WhatsApp us to book.")
    return "Here's the draft:\n" + "\n".join(lines)


# ------------------------------------------------------------------ questions
def _answer(msg: str, c: Ctx, conv: dict, lang: str) -> tuple[str, str]:
    low = msg.lower()
    if re.search(r"renew|renewal|plan|subscription", low) and re.search(r"cost|price|kitna|how much|fee|charge", low) and c.payload.get("renewal_amount"):
        return L(lang, f"Your {c.payload.get('plan', '')} plan renewal is {inr(c.payload['renewal_amount'])}.",
                 f"Aapke {c.payload.get('plan', '')} plan ka renewal {inr(c.payload['renewal_amount'])} hai."), "data"
    if re.search(r"cost|price|kitna|how much|charge|rate", low) and c.offers and not re.search(r"magicpin|vera|plan|you charge", low):
        return L(lang, "Your live offers: " + ", ".join(c.offers) + ".", "Aapke live offers: " + ", ".join(c.offers) + "."), "data"
    if llm.enabled():
        try:
            ctx = {k: v for k, v in c.base_facts().items() if k != "recent_conversation"}
            out = llm.parse_json(llm.complete(
                "You are Vera (magicpin). Answer the merchant's question ONLY from FACTS. If FACTS don't contain the answer, return known=false. Output JSON only.",
                json.dumps({"QUESTION": msg, "FACTS": ctx, "LANGUAGE": lang, "FORMAT": '{"known": true|false, "answer": "<1-2 sentences>"}'}, default=str),
                max_tokens=250, t=6))
            ans = str(out.get("answer", "")).strip()
            if out.get("known") and ans and not check(ans, ctx, "", (c.category.get("voice") or {}).get("vocab_taboo", []), max_len=400, max_emoji=1):
                return ans, "ai"
        except Exception:
            pass
    return "", "unknown"


# ------------------------------------------------------------------ main entry
def _infer_offer(c: Ctx) -> str:
    m = re.search(r"(?:want me to|shall i|should i)\s+([^?]+)\?", c.last_vera_body(), re.I)
    return m.group(1).strip() if m else "draft this week's Google post"


def respond(store: Store, body: dict) -> dict:
    conv_id = body.get("conversation_id") or "conv_unknown"
    msg = str(body.get("message") or "")
    from_role = body.get("from_role", "merchant")
    conv = store.conv(conv_id)
    merchant_id = body.get("merchant_id") or (conv or {}).get("merchant_id")
    merchant = store.get("merchant", merchant_id) or {"merchant_id": merchant_id, "identity": {}}
    category = store.category_for(merchant) or {}
    customer_id = body.get("customer_id") or (conv or {}).get("customer_id")
    customer = store.get("customer", customer_id)
    if conv is None:
        conv = store.new_conv(conv_id, merchant_id=merchant_id, customer_id=customer_id,
                              send_as="merchant_on_behalf" if from_role == "customer" else "vera")
    trigger = store.get("trigger", conv.get("trigger_id")) or {}
    c = Ctx(category, merchant, trigger, customer, store.merchants())
    if not conv.get("offer"):
        conv["offer"] = _infer_offer(c)
        fm = re.search(r"focus on ([^.!?]+)", c.last_merchant_body(), re.I)
        if fm:
            conv["focus"] = fm.group(1).strip()
            conv.setdefault("followup", {"type": "plan_post"})

    with store.lock:
        conv["turns"].append({"from": from_role, "body": msg})
        key = _norm(msg)
        inbound = store.merchant_inbound[f"{merchant_id}:{from_role}"]
        repeat = inbound.count(key) + 1 if key else 0
        inbound.append(key)

    label = classify(msg, repeat)
    flags = store.merchant_flags[merchant_id]

    # ---- sticky language (switch on explicit ask, or 3 messages in a row in another language)
    profile_lang = c.clang if from_role == "customer" else merchant_lang(merchant)
    lang = conv.setdefault("lang", profile_lang)
    ml = "hinglish" if detect_msg_lang(msg) in ("hinglish", "hindi") else "en"
    asked = next((k for k, pats in LANG_ASK.items() if _any(pats, msg)), None)
    if asked:
        lang = conv["lang"] = asked
        conv["lang_streak"] = 0
    elif len(msg.split()) >= 3:
        conv["lang_streak"] = conv.get("lang_streak", 0) + 1 if ml != lang else 0
        if conv["lang_streak"] >= 3:
            lang = conv["lang"] = ml
            conv["lang_streak"] = 0

    first = customer_first(customer) if from_role == "customer" else owner_first(merchant)
    sal = first if from_role == "customer" else salutation(merchant, c.slug)
    bot_turns = sum(1 for t in conv["turns"] if t["from"] == "bot")

    def send(text, rationale, cta="open_ended", end_after=False):
        prev = {t["body"] for t in conv["turns"] if t["from"] == "bot"}
        if text in prev:
            text += L(lang, "\n(Just following up once — ignore if not relevant.)", "\n(Bas ek follow-up — relevant na ho toh ignore karein.)")
        with store.lock:
            conv["turns"].append({"from": "bot", "body": text})
            if end_after:
                conv["ended"], conv["stage"] = True, "closed"
        return {"action": "send", "body": text, "cta": cta, "rationale": f"[{label}] {rationale}"}

    def end(rationale):
        conv["ended"], conv["stage"] = True, "closed"
        return {"action": "end", "rationale": f"[{label}] {rationale}"}

    # ---- restart after opt-out
    if label == "restart":
        flags.pop("opted_out", None)
        conv["ended"] = False
        return send(L(lang, f"Welcome back, {sal}! I'll only message when there's something useful for {c.mname}.",
                      f"Welcome back, {sal}! Sirf tab message karungi jab {c.mname} ke liye kuch kaam ka ho."), "Merchant re-subscribed with START.")
    if conv.get("ended"):
        return {"action": "end", "rationale": "Conversation already closed; not re-engaging."}

    # ---- non-real replies streak (max turns rule)
    real = label not in ("auto_reply", "empty", "ack")
    conv["nonreal_streak"] = 0 if real else conv.get("nonreal_streak", 0) + 1

    if label == "empty":
        return {"action": "wait", "wait_seconds": 1800, "rationale": "Empty message; waiting."}

    if label == "opt_out":
        flags["opted_out"] = True
        return send(L(lang, f"Done — you won't get any more messages from me, {sal}. If you ever want help again, just reply START.",
                      f"Theek hai {sal} — you won't get any more messages from me. Kabhi help chahiye ho toh START reply karein."),
                    "Explicit opt-out: confirm once, block all future proactive messages.", "none", end_after=True)

    if label == "auto_reply":
        conv["auto_reply_count"] = conv.get("auto_reply_count", 0) + 1
        seen = max(conv["auto_reply_count"], repeat)
        flags["auto_reply"] = True
        if seen >= 2:
            return end(f"Auto-reply detected {seen}x — exiting; merchant flagged so the next proactive message addresses the owner directly.")
        return send(L(lang, f"Looks like an auto-reply 🙂 {sal}, whenever you see this yourself — just reply YES and I'll {conv['offer']}. No rush.",
                      f"Lagta hai yeh auto-reply hai 🙂 {sal}, jab aap khud dekhein — bas YES reply kar dijiye, main '{conv['offer']}' kar dungi."),
                    "Auto-reply detected once: one owner-directed nudge, end if it repeats.", "binary_yes_stop")
    flags.pop("auto_reply", None)  # a real human replied
    store.mark_reply(merchant_id)

    if from_role == "customer":
        return _customer_flow(label, msg, c, conv, lang, first, send, end)

    if label == "call":
        return send(L(lang, f"Done — I've asked the magicpin team to call you, {sal}. They'll reach you on this number.",
                      f"Ho gaya — magicpin team ko bol diya hai, woh aapko isi number pe call karenge."), "Merchant asked for a human — handoff.",
                    "none", end_after=True)

    if label == "not_interested":
        if trigger.get("kind"):
            flags.setdefault("paused_kinds", set()).add(trigger["kind"])
        return send(L(lang, f"No problem, {sal} — thanks for letting me know. I'll only reach out if something important comes up for {c.mname}.",
                      f"Koi baat nahi {sal} — batane ke liye shukriya. Sirf tab message karungi jab {c.mname} ke liye kuch zaroori ho."),
                    "Declined: polite close, conversation ended, this topic paused for the merchant.", "none", end_after=True)

    if bot_turns >= 5:
        return end("Turn budget reached (5 bot messages).")
    if conv["nonreal_streak"] >= 3:
        return end("3 bot messages in a row without a real reply — closing.")

    if label == "hostile":
        conv["hostile"] = conv.get("hostile", 0) + 1
        if conv["hostile"] >= 2:
            flags["opted_out"] = True
            return end("Second angry message — ending and blocking further messages.")
        return send(L(lang, f"Sorry, {sal} — fair point, I don't want to waste your time. I'll only message when there's something specific to {c.mname}. Reply STOP anytime and I won't message again.",
                      f"Sorry {sal} — aap sahi ho, aapka time waste nahi karna. Sirf tab message karungi jab {c.mname} ke liye kuch specific ho. STOP reply karein — then I won't message again."),
                    "Angry but no opt-out: apologise, lower frequency, clear exit.")

    if label == "defer":
        secs, when = wait_seconds_for(msg, body.get("received_at"))
        conv["wait_until"] = secs
        ack = L(lang, f"Sure, {sal} — I'll check back {when} 👍", f"Zaroor {sal} — {when} phir baat karte hain 👍")
        conv["turns"].append({"from": "bot", "body": ack})
        return {"action": "wait", "wait_seconds": secs, "body": ack,
                "rationale": f"[defer] Acknowledged and backing off until {when} ({secs // 3600}h)."}

    if label == "thanks":
        if conv.get("stage") == "action":
            return end("Merchant acknowledged the delivered work — closing the loop.")
        return {"action": "wait", "wait_seconds": 86400, "rationale": "[thanks] Polite acknowledgement, no commitment — waiting."}

    if label == "off_topic":
        low = msg.lower()
        items = [d for d in c.category.get("digest") or [] if d.get("kind") == "compliance"
                 and any(re.search(p, (d.get("title", "") + d.get("summary", "") + d.get("source", "")).lower()) and re.search(p, low) for p in OFF_TOPIC)]
        topic = "GST filing" if re.search(r"gst", low) else ("tax filing" if re.search(r"tax|itr", low) else "that")
        boundary = L(lang, f"{topic[0].upper() + topic[1:]} is outside what I can help with — your CA is the right person for that. I'm here whenever you need help with your Google profile or offers.",
                     f"{topic[0].upper() + topic[1:]} mein main help nahi kar paungi — iske liye aapke CA sahi rahenge. Google profile ya offers mein kabhi bhi help chahiye ho, main hoon.")
        if items and not _any(COMPLEX, low):
            d = items[0]
            from .templates import clean_dates
            facts = [clean_dates(x.rstrip(".")) for x in re.split(r"(?<=[.])\s+", d.get("summary", "")) if x.strip()][:3]
            text = "\n".join([L(lang, f"{topic[0].upper() + topic[1:]} itself is best done with your CA, but here's one official update relevant to you:",
                                f"{topic[0].upper() + topic[1:]} ke liye CA best hain, par yeh ek official update aapke kaam ka hai:"),
                              bullets([clean_dates(d.get("title"))] + facts + [f"Source: {d.get('source')}"]),
                              "(For information only — not tax advice. Please confirm with your CA.)"])
            return send(text, "Off-topic: shared an official, source-cited item from our data with a disclaimer.")
        return send(boundary, "Off-topic / complex request: polite boundary only.")

    if label == "question":
        ans, how = _answer(msg, c, conv, lang)
        if ans:
            return send(ans + "\n" + L(lang, f"Want me to go ahead and {conv['offer']}? Reply YES.", f"'{conv['offer']}' kar doon? Reply YES."),
                        f"Question answered from {how}.")
        pats = [p for p in case_patterns(store.merchants()) if p["applies"](merchant)]
        known = (L(lang, f"What I can show you: {pats[0]['text'][0].lower() + pats[0]['text'][1:]} — and {pats[0]['gap_text']}.",
                   f"Jo main dikha sakti hoon: {pats[0]['text']} — aur {pats[0]['gap_text']}.") if pats else "")
        text = "\n".join(x for x in [
            L(lang, f"Honest answer, {sal}: I don't have that in my data, and I won't guess or promise results.",
              f"Seedhi baat {sal}: yeh mere data mein nahi hai, aur main guess ya promise nahi karungi."),
            known,
            L(lang, "If you'd rather talk to a person, reply CALL and someone from the magicpin team will ring you.",
              "Kisi insaan se baat karni ho toh CALL reply karein — magicpin team aapko call karegi.")] if x)
        return send(text, "Question not answerable from data: honest, one data-backed fact, human option.")

    if label == "ack":
        return send(L(lang, f"No rush, {sal} — whenever you're ready, just reply YES and I'll {conv['offer']}.",
                      f"Koi jaldi nahi {sal} — jab ready hon, bas YES reply kariye, main '{conv['offer']}' kar dungi."),
                    "Non-committal reply ('hmm'/'ok?') — one gentle nudge, no pressure.", "binary_yes_stop")

    # commit / engaged -> ACTION MODE: deliver + approve + next step (never re-qualify)
    if label == "engaged":
        conv["focus"] = msg[:120]
        if (conv.get("followup") or {}).get("type") == "price_reply":
            svc = msg.strip().strip(".!")
            off = next((o for o in c.offers if svc.lower()[:5] in o.lower()), None)
            text = (f"Here's your ready price-reply for {svc}:\n\"Thanks for asking! " + (f"{off}. " if off else f"{svc[0].upper() + svc[1:]} is available — ask us for today's price. ")
                    + f"Book on WhatsApp or call {c.mname}.\"")
            conv["stage"] = "action"
            return send(text + "\n" + L(lang, "Reply YES and I'll save it as a quick-reply. Next: a Google post on it.", "YES reply karein, quick-reply save kar dungi. Uske baad ek Google post."),
                        "Merchant answered the curious-ask — delivered the promised price-reply.")
    conv["stage"] = "action"
    art, nxt = build_followup(conv, c, lang)
    if not art:
        art, nxt = _post(c, conv, "post"), L(lang, "Reply YES and I'll publish it, or tell me what to change.", "YES reply karein toh publish kar dungi.")
    if not art.lower().startswith(("done", "here", "ho gaya", "sending", "*", "📄", "enquiry", "review", "comeback", "steps")):
        art = L(lang, "Done — here it is:\n", "Ho gaya — yeh raha:\n") + art
    text = f"{art}\n\n{nxt}".strip()
    return send(text, "Commitment detected — delivered the work now, approval + next step (no re-qualifying).")


def _customer_flow(label, msg, c: Ctx, conv, lang, first, send, end):
    if label in ("opt_out", "not_interested", "hostile"):
        return send(L(lang, f"Understood, {first} — we won't message you again. Take care!", f"Theek hai {first} — ab message nahi karenge. Dhyan rakhiye!"),
                    "Customer opted out / declined — stop messaging on the merchant's behalf.", "none", end_after=True)
    if label == "defer":
        secs, when = wait_seconds_for(msg, None)
        return {"action": "wait", "wait_seconds": secs, "body": L(lang, f"No problem, {first} — we'll check back {when}.", f"Koi baat nahi {first} — {when} phir yaad dila denge."),
                "rationale": "Customer asked for time."}
    if re.search(r"recall|batch|safe|kharab|problem", msg, re.I) and c.slug == "pharmacies":
        return send(L(lang, f"Thank you for asking, {first}. Yes — for that recall we're following the due process: every strip we send is checked against the affected batch numbers before it goes out.",
                      f"Poochne ke liye shukriya {first} ji. Haan — us recall ke liye poora due process follow ho raha hai: har strip bhejne se pehle affected batch numbers se check hoti hai."),
                    "Customer asked about the recall — reassurance that due process is followed (as decided).", "none")
    slots = (conv.get("followup") or {}).get("slots") or c.payload.get("available_slots") or c.payload.get("next_session_options") or []
    slots = [s.get("label") if isinstance(s, dict) else s for s in slots]
    pick = re.search(r"\b([12])\b", msg)
    if pick and slots and int(pick.group(1)) <= len(slots):
        s = slots[int(pick.group(1)) - 1]
        conv["stage"] = "action"
        return send(L(lang, f"Booked ✅ {s} at {c.mname}. We'll send a reminder the day before — reply here if anything changes.",
                      f"Book ho gaya ✅ {s}, {c.mname}. Ek din pehle reminder bhejenge — kuch badle toh yahin reply karein."), "Customer picked a slot — booked.", "none")
    if label in ("commit", "engaged", "ack"):
        conv["stage"] = "action"
        s = slots[0] if slots else None
        ftype = (conv.get("followup") or {}).get("type")
        if ftype == "refill":
            txt = L(lang, f"Done ✅ Your medicines will be delivered to your saved address. Thank you, {first}!", f"Ho gaya ✅ Dawaiyan aapke saved address pe pahuncha denge. Shukriya {first} ji!")
        elif s:
            txt = L(lang, f"Done ✅ {s} is yours at {c.mname}. We'll remind you the day before.", f"Ho gaya ✅ {s} aapke liye book hai. Ek din pehle yaad dila denge.")
        else:
            txt = L(lang, f"Done ✅ Thanks {first}! The {c.mname} team will confirm the exact time with you shortly.",
                    f"Ho gaya ✅ Shukriya {first}! {c.mname} team jaldi exact time confirm karegi.")
        return send(txt, "Customer said yes — confirmed; exact scheduling left to the merchant (no invented times).", "none")
    if label == "thanks":
        return end("Customer acknowledged — nothing more to say.")
    return send(L(lang, f"Thanks {first}! I've passed this to the {c.mname} team — they'll reply here shortly.",
                  f"Shukriya {first}! Yeh {c.mname} team ko bhej diya hai — woh jaldi yahin reply karenge."),
                "Customer question outside the booking flow — handed to the merchant rather than guessing.", "none")
