# Vera bot — magicpin AI Challenge submission

**Approach.** For each of the 30 trigger types (the 26 in the dataset plus 4 the brief names), I decided whether the message should be written by **rules**, by **rules plus AI (hybrid)**, or by **AI first**. The rule for deciding was simple: the more a mistake would cost (compliance, drug recalls, messages sent in the merchant's name to customers), the less freedom the AI gets.

The full reasoning is in `DECISIONS.md`. In practice it works like this:

- **Code owns every fact and number.** This covers the merchant's own data and the peer averages. Peer averages are anonymous averages of at least 3 merchants in the same category, same city first; below that, the published category benchmark is used. It also covers the data-backed case studies. A pattern such as "verified profiles average +60% views" is kept only if the data supports it, and it is always framed as a tendency, never as a cause.
- **At most one AI call per message, and it gets a narrow job.** Depending on the trigger, the AI:
  - picks the 1–3 most relevant weak spots from a list that code has already computed;
  - writes one "why this matters to you" line;
  - rates how serious a recalled medicine is, choosing from fixed labels;
  - judges whether a piece of local news actually matters to this merchant;
  - or writes the whole message, for low-risk triggers like seasonal trends.
- **A checker stands between the AI and the merchant.** It rejects any AI output that:
  - uses a number not found in the data;
  - uses a taboo or over-promising word;
  - contains more than one call to action or more than one emoji;
  - is missing its bullet points;
  - asks a qualifying question after the merchant has already said yes.

  A rejected, slow or rate-limited AI call never blocks a message: the rules-only version, which is already in the final format, is sent instead.
- **Restraint is deliberate.** The bot sends nothing when:
  - an "empty" trigger has nothing honest to say (a "milestone" with no milestone, a "dip" when the numbers aren't down);
  - a news item doesn't matter to this merchant;
  - it's a CDE event while a more important message is waiting;
  - it's a scheduled check-in for a merchant who talked to Vera in the last 48 hours.
- **Conversation router.**
  - Opt-out: confirm once, then never message again.
  - Auto-reply: nudge once, end if it repeats, and address the owner directly next time.
  - Angry: apologise and offer an exit.
  - Not interested: polite close, and pause that topic.
  - "Not now": acknowledge, then wait until the time they named.
  - "Yes": deliver the draft, publish on approval, and say the next step. Never ask another qualifying question.
  - Off-topic (e.g. GST): share only official, source-cited items from the data, with a disclaimer, or else a polite boundary.
  - A question the data can't answer: an honest answer plus the option to speak to a human (CALL).
  - A conversation ends after 5 bot messages, or after 3 in a row with no real reply.
  - Language stays on the merchant's profile language unless they ask to switch or write 3 messages in a row in another language.

**Tradeoffs.**
- Reliability comes before flair. Every message has a rules-only version, so the free Gemini tier's rate limits cost wording quality, not correctness or speed.
- Several empty triggers are routed to the merchant for approval rather than sent straight to the customer, which respects consent. The cost is that a few customer-facing tests become merchant-facing.
- Two test pairs (T23, T25) are intentionally skipped, because the data gives nothing honest to say.

**What additional context would help most.**
- Real appointment slots and service prices per merchant.
- Stock and cost data for recall-exposure maths.
- Outcome data (which messages got replies), to learn which levers work.
- Competitor details for the empty `competitor_opened` triggers.
- An official source feed for regulatory and GST questions.

**Run it.** `python bot.py`. It uses only the Python standard library. Settings (environment variables):
- `LLM_PROVIDER=gemini` and `LLM_API_KEY=<free key>`; without a key the bot runs in rules-only mode.
- `TEAM_NAME`, `TEAM_MEMBERS`, `CONTACT_EMAIL`

Tests:
- `python scripts/harness.py`: end-to-end test
- `python scripts/test_tick_rules.py`: tick rules
- `python scripts/test_llm_path.py`: AI-path checks
- `python scripts/make_submission.py`: builds `submission.jsonl`
