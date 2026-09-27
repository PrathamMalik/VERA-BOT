# How my bot works — interview guide

## The one-line pitch
> "Code decides what's true, the AI only helps where it's safe, and a checker makes sure nothing made-up ever reaches a merchant. And when there's nothing honest to say, the bot stays quiet."

## The core idea: I matched AI freedom to risk
I didn't hand everything to an AI. For every trigger type I asked one question: **"If this message is wrong, how much damage does it do?"**

| Risk | Examples | My choice |
|---|---|---|
| High: legal, safety, or sent in the merchant's name to customers | Regulation change, drug recall, customer reminders, refills | **Rules only.** Exact facts, no AI wording. |
| Medium: advice about the merchant's own business | Competitor opened, performance dip, renewal | **Hybrid.** Code computes every number; AI only picks what's most relevant or writes one line. |
| Low: trends and ideas | Seasonal demand, planning a new package | **AI-first,** but still fact-checked. |

## Five decisions I'm proudest of (use these in the interview)
1. **Weak spots, not flattery.** When a competitor opens nearby, the bot shows the merchant where they're *most exposed* (e.g. "CTR 2.1% vs 3.1% avg for similar clinics"). It then offers what's working in their favour, plus common strategies to fix the gaps. It's consultative, not a sales push.
2. **Real peers, honestly.** Comparisons use anonymous averages of at least 3 similar merchants in our data, same city first. It never names a competitor, which protects other merchants' privacy.
3. **Case studies only when the data agrees.** Patterns like "verified profiles average 60% more views" are calculated from the data and kept *only if true*. For example, the data did **not** support "expired plans lose traffic", so the bot never says it.
4. **Drug recall done like a pharmacist would:**
   - 🟠 severity tag, with exact batch numbers
   - action steps as bullets
   - an offer to calculate exposure (refunds, GST, losses) from the merchant's *own* numbers
   - the elderly customer's refill reminder *doesn't* mention the recall unless he asks, and then he's reassured that due process is being followed
5. **Restraint.** No message when there's nothing honest to say: a "dip" when the numbers aren't down, a "milestone" with no milestone, news that doesn't matter to this merchant. The brief says restraint is rewarded.

## How a message is built
1. **Code** reads the 4 contexts: category, merchant, trigger, customer.
2. **Code** writes a complete, safe version in the final format (bullets for facts and actions).
3. **The AI (free Gemini)** gets one small, specific job, if the trigger allows it.
4. **The checker** blocks:
   - any number not in the data
   - taboo words ("guaranteed", "cures")
   - more than one ask or more than one emoji
   - missing bullets
   - a qualifying question after the merchant has said yes
5. If the AI fails, is slow or is rate-limited, **the safe version goes out**. The bot can never time out.

## How replies are handled
| Merchant says… | Bot does… |
|---|---|
| Canned auto-reply | One nudge to the owner; ends if it repeats; next time it addresses the owner directly |
| "Stop" | Confirms once ("you won't get any more messages, reply START anytime"), then never messages again |
| Angry | Apologises, offers a clean exit; a second angry message ends it |
| "Not interested" | Polite one-line close; never sends that topic again |
| "Busy, message me after 6" | "Sure, I'll check back after 6pm 👍", then waits exactly that long |
| "Yes / let's do it" | Delivers the actual draft, publishes on approval, says what's next; never asks another qualifying question |
| "Help me file my GST" | Honest boundary. If our data holds an official GST item relevant to them, shares it with its source and a "not tax advice" disclaimer |
| A question the data can't answer | "I won't guess or promise results", plus one real data point, plus "reply CALL to talk to a person" |

A conversation also ends after 5 bot messages, or after 3 in a row with no real reply. Language stays on the merchant's profile language unless they ask to switch or write 3 messages in a row in another language.

## Why Gemini (free) works with this design
Every message already has a correct rules-only version. So the free tier's rate limit only affects *polish*, never correctness or speed. A limiter spends the free AI calls on the triggers where AI adds the most value.

## If they ask "what would you improve?"
1. Real appointment slots, prices and stock data, so fewer triggers have to be skipped or routed to the merchant.
2. Learning from outcomes: which formats get replies, per merchant.
3. A/B testing the hybrid lines against the rules-only versions, to *prove* the AI's value.
4. Persistent storage (e.g. Redis) so a restart doesn't lose conversation state.

## Files
- `DECISIONS.md`: every decision, with the why
- `vera/templates.py`: one handler per trigger type
- `vera/peers.py`: peer averages, weak spots, case studies
- `vera/composer.py`: the one-AI-call pipeline
- `vera/validator.py`: the checker
- `vera/conversation.py`: the reply router
- `vera/engine.py`: tick rules
- `vera/llm.py`: the Gemini client and rate limiter
- `scripts/`: the tests and the submission builder
