# Design decisions (made by me, implemented by Claude)

## Composition strategy per trigger
| # | Trigger | Decision | Why |
|---|---|---|---|
| 1 | research_digest | **Hybrid**: code states the study facts exactly; AI picks the best-fit item for empty triggers and writes the one "why this matters to you" line (fact-checked) | Facts must be exact (no overstating studies), but relevance to the merchant is where AI adds value |
| 2 | regulation_change | **Rules-first**: exact rule + deadline + checklist offer, no AI | Legal/compliance content — zero paraphrase risk beats nicer wording |
| 3 | cde_opportunity | **Rules-first, filler only**: send only when the merchant has no higher-urgency trigger waiting | Urgency 1, nice-to-know; restraint over spam |
| 4 | supply_alert | **Rules-first, action mode**: he already asked for the list, so deliver instead of re-asking. Plus: (a) severity of the recall *reason* shown as 🔴/🟠/🟢 + bold (WhatsApp has no colours); (b) seriousness of the *medicine* judged by AI but from a fixed label set (life-critical / chronic-important / routine); (c) state known exposure facts (distributor replacement → shelf stock recoverable) and offer to calculate, from the merchant's own numbers: units to recall from customers, units on shelf, refunds to reverse, GST impact on reversed sales, other non-refundable losses (e.g. delivery/handling) — never guessing a figure | Safety content stays exact; merchant sees at a glance how urgent it is; no invented numbers |
| 5 | category_seasonal | **AI-first** (fact-checked, bullets): AI writes the whole message from the trend numbers + shop data; template fallback if it fails the check | Low-stakes, high-upside: tailoring the seasonal shift to this shop is where AI adds most value |
| 6 | competitor_opened | **Hybrid, weak-spots first (real + empty triggers)**. Code states competitor facts exactly when present (never names/invents one), finds the merchant's weak spots (call/view drops, negative review themes, below-peer metrics) and computes every stat vs the **average** of similar businesses. AI only picks the 1–3 most relevant weak spots. Single closing offer: show what's working for them (their positives) + a discussion of popular strategies to fix the weak spots. The strategy discussion is AI-written and must contain **no numbers** (enforced by the checker) | A new competitor attacks weak spots first; ending on positives + a conversation (not a sales push for a post) keeps it consultative |
| 7 | review_theme_emerged | **Same pattern as #6**. Real: review facts (count, quote, trend) as bullets from code → one closing offer: positives + common strategies to fix it (AI, no numbers). Empty (no review data): weak spots vs peer average (rule-computed) + same closing offer. Never a vague 'a pattern is showing' claim | Consistent consultative pattern; honest when data is missing |
| 8 | perf_dip | **#6 pattern + one quick fix; skip empty triggers with no real dip**. Bulleted drop + weak spots vs peer avg (code-computed, AI picks top 1–3) + ONE concrete fix from their data (e.g. no active offer) → closing offer: positives + recovery strategies (AI, no numbers). Empty trigger whose 7-day numbers aren't actually down → *(updated)* send an honest status instead: "nothing is dipping" + the real gaps vs peers + one quick fix; send nothing only if there's no gap either | Honest (no fake 'dip'), specific, and gives one immediate lever |
| 9 | perf_spike | **Real spike (≥ +10%): Option 3 — convert the spike** using the merchant's real offers (enquiry-reply draft). **Small spike (< +10%)**: compare the merchant with other same-category merchants in our own data (same city first) to show whether the move is market-wide or specific to him; list the differences between him and them (views, calls, CTR, verified, active offers); offer to cost out a change of strategy. AI only chooses which benchmarks/peers are most relevant; every number is computed by code from the dataset; no names or data invented | A tiny bump isn't worth celebrating — context vs real peers is more useful and fully fact-based |
| 10 | seasonal_perf_dip | **Peer check first, then #6 pattern + one cost-saving tip**. Code checks ≥3 same-category merchants (anonymous): if they're down too → it's seasonal → reassure + weak spots (churn, trial conversion vs avg) + ONE actionable tip that cuts wasted spend (e.g. pause acquisition ads this month, from category data) + positives/strategies offer. If peers are NOT down → it's not seasonal → handle as a real perf_dip (#8) | Proves 'it's the season' with data before reassuring; saves the merchant money |
| 11 | milestone_reached | **Real: milestone + position vs peers** (bullets: you vs similar-business avg, praise count) → offer a review-request message. **Empty:** *(updated)* only a real, calculated milestone — the largest round number actually crossed in the merchant's data (e.g. "787 profile views — past the 750 mark"); skip only if there is none | Specific and honest; no manufactured 'milestones' |
| 12 | renewal_due | **Real: honest, fix first** — admit the weak month in bullets, offer to fix what's in our control (verification, live offer) before asking for renewal; link comes later. **Empty: data-backed case study, regardless of days left** — code compares groups of merchants in our data (e.g. verified vs unverified, with vs without active offers; min 3 per group, anonymous) and keeps only patterns the data actually supports; AI picks the ones that apply to this merchant and turns them into actionable steps. Framed as 'merchants who do X tend to see Y', never as proven cause | Builds trust before asking for money; turns a renewal ping into useful advice |
| 13 | winback_eligible | **Honest, fix first + loss calculator**: bullets of what changed since expiry (calls, customers gone quiet, retention vs avg); offer a free comeback message to the lapsed customers first, renewal later; offer to calculate monthly revenue lost using the merchant's own average bill (rule-based) | Earn the renewal by showing results; money figures only from the merchant's own numbers |
| 14 | gbp_unverified | **Case study + bullet steps**: what being unverified costs (views vs avg), verification steps as bullets. Promise uses magicpin's official **~30%** estimate; our dataset only as 'verified merchants tend to see more' | One cautious official number, no conflicting stats |
| 15 | dormant_with_vera | **Weak spots (#6 pattern)**: where they stand vs similar businesses (code-computed) + positives/strategies offer. Empty trigger (no chat history): same, but no 'it's been X days' claim | Re-open with something useful about *their* business |
| 16 | curious_ask_due | **Ask + give first**: 1–2 bulleted facts as a freebie (relevant trend + review fact, code-picked), then one easy question; a reply earns a ready WhatsApp price-reply | Reciprocity + 'ask the merchant' — the lever real Vera under-uses |
| 17 | festival_upcoming | **Real: timing-aware** — more than 45 days away: short 'save the date' note + seasonal stat + offer an early reminder; 45 days or less: full package pitch built on their real offers. **Empty: category festival window** (e.g. gyms Aug–Oct) with bulleted prep steps, never naming a festival | Restraint when it's too early; no invented festivals |
| 18 | ipl_match_today | **Bullets + delivery warning + dine-in drinks push**: weekend-vs-weeknight data as bullets; keep delivery push (warn about the late-delivery reviews before the rush); for dine-in, promote a drinks / match-night beverage combo instead of food discounts — higher margin, lighter kitchen load. Drinks offer is a suggestion (catalog: 'Match-night Combo @ ₹399 (food + drink)'); never assume alcohol | Uses both channels smartly without overloading the kitchen |
| 19 | active_planning_intent | **Hybrid, action mode**: never re-ask; code supplies fixed facts + price anchors (their real offer price, volumes, reviews, earlier Vera suggestions); AI designs the plan structure + copy; any new price is labelled 'suggested'; checker blocks invented names/places | Delivers the plan the merchant asked for, tailored but grounded |
| 20 | recall_due | **Real: bullets, no AI** — price + slots as bullets, customer's language, 'Reply 1 or 2'. **Empty: ask the merchant** ('X hasn't visited since <date> — want me to send a reminder?') instead of messaging the customer | Customer messages go out in the merchant's name — zero invention; respects consent |
| 21 | appointment_tomorrow | **Bullets, no AI**: when (tomorrow, at the booked time — never an invented time) + where, then confirm/reschedule. Treated as a service message about the customer's own booking | Useful, honest reminder even without details |
| 22 | chronic_refill_due | **Real: bullets, no recall mention** — medicines, run-out date, delivery + senior offer as bullets. The recall is handled quietly by the pharmacy; only if the customer asks about it, reply with reassurance that due process is being followed. **Empty / mismatched (e.g. dentist 'refill'): ask the merchant** first | Don't alarm an elderly customer; transparent only when asked |
| 23 | trial_followup | **Real: bullets + social proof** — next slot + offer as bullets, addressed to the parent when the customer is a child, one line of real review proof. **Empty: ask the merchant** | Gives a real reason to continue; consistent handling of empty customer triggers |
| 24 | customer_lapsed_soft | **Bullets to the customer, no AI**: last visit + visit count from their history, offer to hold a slot; no invented offer (consent covers promotional messages) | Keeps the test customer-facing, uses only real history |
| 25 | customer_lapsed_hard | **Bullets, no trial offer**: warm no-pressure check-in, their goal + membership length + preferred slot as bullets, offer to hold a slot; new-customer offers are not used for ex-members | Personal and respectful; avoids offers the merchant may not intend for them |
| 26 | wedding_package_followup | **Bullets countdown, no AI**: wedding date, days left, trial done, next step (skin-prep window), preferred day; no invented program price | Clear, personal, zero fabrication |
| 27 | weather_heatwave | **Hybrid, with the category rules as guide + fallback**. Code states the event exactly and filters to merchants in the affected city. AI writes 1–2 lines linking the heat to this merchant's real data, following the category rule, with no numbers of its own. Category rules (also the fallback if AI is slow/fails): Pharmacies — ORS/electrolytes/sunscreen to counter + stock check → 'heat essentials' WhatsApp. Restaurants — cold-drink add-ons + delivery push (+ realistic delivery times if late-delivery reviews) → 'beat the heat' offer. Gyms — hydration reminder + early/evening sessions → member note. Salons — summer hair-care services from their offers → post. Dentists — Google post on dental care in hot weather (more cold/sugary drinks and treats, especially for children), no statistics | Natural wording where it helps, predictable category logic underneath |
| 28 | local_news_event | **Hybrid, skip if unsure**: code states the news exactly, same-city merchants only; AI decides whether it matters to this merchant and writes 1–2 practical lines from their real data (no numbers of its own); not relevant, or AI slow/unavailable → send nothing | News is open-ended — restraint beats forced relevance |
| 29 | category_trend_movement | **Hybrid + relevance filter**: code states the trend numbers exactly (query, % change, age group); AI links it to this merchant (their offers, past requests, missing offer); skip if the trend doesn't fit the merchant (not offered, never asked about, not common in their category) | Specific and personal, never spammy |
| 30 | scheduled_recurring | **Rotate approved formats + skip if recently in touch**: each scheduled check-in uses the next format with data for that merchant (curious ask #16 → case study #12 → weak spots #6 → relevant trend #29); skip if the merchant talked to Vera in the last 48h | Variety for 3–5 touches a week without spam |

## Global formatting rule
- Any message that contains **actions to take or several facts** (especially rules, regulations, recalls, checklists, plans) uses **short bullet points** instead of a paragraph — easy to scan on a phone.
- Short conversational messages (curious ask, check-ins) stay as 1–3 plain sentences.

## Global data rules
- **Several triggers for one merchant**: send the higher-urgency one first; the next one goes on a later tick, only after the merchant replies or enough time has passed.
- **Peer comparisons**: anonymous average of **≥ 3** same-category merchants from our own data (same city first, then wider); if fewer than 3, fall back to the category benchmark. Never name another merchant.
- **"What competitors do differently"**: only differences visible in the data (verified status, active offers, performance). Nothing inferred.

## Time rule (30s per call; practice judge 15s)
- At most **one AI call per message** (fact-picking + writing combined).
- Tick AI budget 10s, reply budget 12s, messages composed in parallel.
- Every trigger has a rules-only fallback in its new format; slow AI → fallback ships, never a timeout.
- Peer/case-study statistics pre-computed when contexts arrive.
- *(added)* **Day counts and 'tonight/tomorrow' follow the judge's clock** (the `now` sent with each tick), not the numbers frozen in the data. Events already past (festival, match, CDE, wedding, slots) are skipped or reworded.
- *(added)* **Shorter messages**: the comparison group is named once in the heading instead of in every bullet.

## Conversation behaviour (replies)
| # | Situation | Decision |
|---|---|---|
| 1 | Auto-reply | **Nudge once, end on repeat + flag**: one owner-directed nudge with a one-word CTA; end when it repeats (tracked per merchant, across conversations); flag the merchant so the next proactive message opens by addressing the owner directly |
| 2 | 'Stop' / opt-out | **Confirm once, then never**: one-line confirmation ('you won't get any more messages — reply START anytime'), then permanently block proactive messages to that merchant |
| 3 | Angry, no 'stop' | **Apologise + exit option**: short apology, promise to message only when there's something specific, 'reply STOP anytime'; a second angry message → end + block; an unrelated question after anger is still handled politely |
| 4 | 'Not interested' | **Polite close + pause that topic**: one-line thanks/close, end; never send that trigger type to this merchant again; other topics may still come later |
| 5 | 'Not now' | **Acknowledge + follow their time**: one short line ('Sure, I'll check back tomorrow 👍'), then wait until the time they named ('after 6', 'Monday', 'next week'); defaults 24h for tomorrow/kal/next week, 2h for busy/later |
| 6 | Merchant says yes | **Deliver + approve + next step**: immediately show the actual draft/confirmation, publish only on their OK, and say what comes next; never a qualifying question after a yes |
| 7 | Off-topic (e.g. GST) | **Boundary, plus official facts only when we have them**: if the question matches a source-cited official item in the category data (e.g. GST Council circular), share it as bullets with the source and a 'not tax advice — confirm with your CA' disclaimer; otherwise, or if complex (e.g. 'file my returns'), polite boundary only. No live web lookups, no AI general knowledge presented as official |
| 8 | Real question | **Answer from data; if not in data — honest + what we know + human option**: say plainly what can't be promised, share one relevant data-backed fact (case-study style) and a next step, and offer a call from the magicpin team ('reply CALL'). Never promise results, never invent prices/policies |
| – | Max turns | End after **5 bot messages total**, or earlier after **3 bot messages in a row with no real reply** |
| – | Language | **Stick to the profile language**; switch only if the merchant explicitly asks, or writes in a different language for **3 messages in a row** |

## Voice & language
- **Hinglish** for merchants whose profile includes Hindi, except South Indian merchants (Tamil/Kannada/Malayalam) → English. Customers follow their own language preference.
- **Emojis**: max 1 per message where it fits; none in compliance/recall messages (except the #4 severity dot).
- **Vera's voice**: female first person in Hindi ("kar deti hoon"), matching real Vera.

## Model
- **Gemini (free tier)** via `LLM_PROVIDER=gemini`, model **gemini-3.5-flash-lite** (updated): free tier allows 15 requests/min and 500/day, vs only 5/min and 20/day for plain Flash; our AI jobs are small (pick, one line, label), so Lite is enough. A per-minute limiter spends free calls on AI-first/Hybrid triggers first; any refused/slow call → rules-only fallback.

---
## Eval round 1 → strategy changes (measured, not guessed)
Measured with `scripts/eval_real.py`: 40 triggers in real-test proportions (30 empty placeholders, 10 real-data), customer profiles, April clock, magicpin's own judge prompt on Gemini. Baseline **38.7/50** (real-data 41.9, placeholder 37.6). Weakest dimension: decision quality ("why now") 6.6/10.

Root causes found: (1) message didn't match its trigger (empty triggers fell back to a generic weak-spots template); (2) real facts looked invented because their source was invisible; (3) mixed signals in one message; (4) trigger types that don't fit the business; (5) confrontational tone, language rule.

Decisions (made by me after seeing the data):
- **R1 Why-now first line** for every message, even when the trigger is empty.
- **R2 Evidence must match the trigger** (review trigger → review data, competitor → how you stack up). If the matching evidence doesn't exist → skip. *(Updates #9/T25: an empty dip trigger with no dip in the data is now skipped instead of sending "nothing is dipping".)*
- **R3 Every number shows its source** — "(magicpin dashboard)", "(magicpin data, last 30 days)", "(your customer records)", "Google reviews".
- **R4 One story per message.**
- **Strengths first** (updates #6): where you're already ahead of similar businesses, then 1–2 places to win more.
- **Trigger-to-business fit rating** (dissociation 0–3): 0 native, 1 close analogue → map to the nearest intent (e.g. gym "chronic refill" → membership top-up), 2+ → skip (restraint).
- **Quality floor, enforced**: name present, one CTA at the end, no internal labels; AI text that fails falls back to the rules text; rules text that fails → skip. *(A "must contain a number" rule was tested and did not predict scores — dropped.)*
- **Ready drafts** kept as the backup rung: positive brand line from real data (review quote / "serving X since Y") + the merchant can send their own tagline, which is saved and reused.
- Language rule for Hindi + a southern language: open — to revisit.

## Eval round 2 (held-out check) → round 3
Held-out result (40 unseen triggers): old 38.4 → new 40.3; messages < 30: 3 → 0. Split of the gain: ~+0.8 better messages on the same triggers, ~+1.1 from skipping triggers the old bot handled badly. Coverage 98% → 82%.
- **Rule A kept:** trigger doesn't fit the business (dissociation ≥ 2) → skip.
- **Rule B changed (my call):** dip trigger with nothing down → send, but worded as a *dip check with an all-clear* ("I ran a dip check — good news, nothing is down ✅ (views +8%, magicpin dashboard)") + one forward-looking step, so it never reads as contradicting the trigger.
- **Round 3 — stay on the trigger's topic** (the judge's main remaining complaint): competitor → "what nearby customers comparing the two will see" (reviews, visibility, live offer); renewal → what the plan delivered + at most one step; festival / curious ask → include the merchant's own numbers; dentists' customer messages always carry the doctor's name. The #30 scheduled check-in rotation keeps the case-study format.

## Category analysis → gyms
Pooled all judged messages (5 runs, ~180 scores) by business type. Salons were the weakest with the old bot (36.1 held-out) and gained most from the new strategy (+3.5). **Gyms were flat in every run (~39.2–39.8) — the only category the fixes didn't reach.** Root cause found in magicpin's own data: the gym category's voice is `english_primary_some_hindi` (coach register) while our language rule sent gyms full Hinglish.
- **Language follows the category's own code-mix rule** (gyms → English-primary).
- **Gym voice layer:** one short coach line before the CTA on merchant-facing gym messages (no result promises — taboo).
- **Gym KPIs:** members (active / this year) in the gym's own numbers; churn and trial-to-paid vs benchmark counted as strengths when better.
- **Gym "chronic refill → membership top-up" mapping dropped** (judged forced in 3/3 runs) → skipped.

## Performance triggers (by merchant)
All weak perf messages sat in salons and gyms (28–36); dentists / pharmacies / restaurants steady at 40–43. Common flaw: two stories in one message.
- **Seasonal dip → reassurance only:** "that's the season, not you — similar gyms are down X% too (magicpin data)" + one cost-saving step. No gap list.
- **Big spike (≥10%) → celebrate + protect:** "great week — calls up 15% 🎉 (magicpin dashboard)" + one way to keep the momentum. No "extra calls only matter if they convert".
- One emoji per message: the gym coach line drops its 💪 when the message already has an emoji.

## Round 3 result → round 3.1
Held-out: round 2 39.9 → round 3 41.1 (+1.2; same-trigger paired +1.7, 15 better / 14 same / 4 worse). Gyms 39.3 → 40.3 held-out (42.2 dev) — the gym fix worked. Biggest gains: gym seasonal dip 33→41, gym festival 34→43, dentist appointment 30→42.
- **Dip check reworded again (my call: keep it, keep it positive):** the "nothing is down (+8%)" version scored 26 and 23 — the judge read weekly % it can't see as fabricated and "nothing went down" as contradicting the trigger. New version: "quick health check: your numbers are holding steady ✅ — 2,547 profile views, 22 calls in the last 30 days (magicpin dashboard). A good moment to grow from here:" + one step.
- **Anti-overfitting for this round:** a new sealed FRESH set (seed 4242, 48 cases): 20 dataset triggers never used before, 20 remixed trigger↔merchant pairs, 8 trigger types only named in the brief (heatwave, local news, trend movement, scheduled check-in); each case gets its own clock. Aggregates only.

## Round 3.1 → shipped (+ heatwave skip)
- **Fresh-set check (48 new/remixed cases, seed 4242):** round 3.1 39.5 vs round 2 39.1 (paired +0.33, W/T/L 15/12/12) — within judge noise, never worse; gyms 38.2 → 39.8; held-out 41.5. Decision: **ship round 3.1** (Pratham, OK).
- **Heatwave trigger with no weather data → skip.** Why: rule R2 (evidence must match the trigger). An empty heatwave scored 30–32 because the bot had to invent "a heatwave is on". With a real temperature/city it still sends the heat-day plan.
- `baseline/` updated to this version for future comparisons.

## Round 4: new trigger types (mid-test injection)
- **Problem (measured by probing):** any trigger type without its own handler fell to a generic "quick check" that ignored the trigger's data (a 2★ review → peer stats). The testing brief injects 15 new triggers mid-test and says bots that ignore new context score lower.
- **Decisions (Pratham):**
  1. **Read the trigger.** First the nearest known type, but only if that handler reads *all* of the trigger's data fields; otherwise the *trigger reader*: code turns the trigger's own fields into the why-now line and bullets under one source, the AI adds one no-numbers line on what it means for this merchant, then one concrete action (family table: reviews, slots, local/external, demand, money, stock, default).
  2. **New type with no data → skip** (R2, same as the empty heatwave).
  3. **Newest relevant research, with a floor:** items pushed mid-test are flagged; one wins only if it matches the merchant (≥1 match, and at least half as relevant as the best item). Otherwise the pick stays as before, and the AI may only choose among the new items that pass the floor.
- Existing behaviour unchanged: 0 of 128 dev, held-out and fresh messages differ (rules-only diff).
- **Blind test:** `eval/novel_set.json` = 30 triggers of unseen types, written by a separate helper that never saw the bot code (sealed). `scripts/eval_compare.py` compares round 4 with round 3.1 on it (paired).
- **Round 4b: rate first, then choose the tone (Pratham).** Every new trigger is rated before any wording is picked: **−2** needs attention today · **−1** heads-up · **0** update · **+1** good news · **+2** great news. The rating comes from code signals: good/bad-news words in the type name; the direction of numbers × whether that metric is good or bad when it rises (calls up = good, commission up = bad); a big move (≥20%, or a rating move ≥5%) doubles it; star ratings; and the tone of any quoted text. No signal = 0. The rating picks the opener, the tone given to the AI line (−2 = calm, reassuring, action-first, no emoji; +2 = celebratory, one 🎉) and the line + action template (bad / neutral / good per family, e.g. bad review → polite reply, good review → thank-you reply). The rating and its reasons are written in each message's rationale.
- **Round 4 result (novel set, 30 blind unseen trigger types, real judge):** 38.4 vs round 3.1 28.8 (**+9.6**); paired +10.7, wins/ties/losses 24/1/2; below-30 messages 17 → 2; decision quality 2.7 → 7.0; every category up (pharmacies 22.6 → 39.4). Existing eval sets unchanged (0/128 messages differ). **Shipped (Pratham).**
- Empty new-type triggers: round 3.1's generic check scored 39.3 on 3 cases, but Pratham chose to **keep skipping** (restraint; nothing in the trigger to act on).
