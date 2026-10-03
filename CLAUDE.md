# CLAUDE.md — Vera bot (magicpin AI Challenge)

## What this is
Pratham's submission for the magicpin Tech/Product AI Analyst challenge: **Vera**, a WhatsApp assistant for Indian local merchants (dentists, salons, restaurants, gyms, pharmacies), served over HTTP and scored by an LLM judge on 5 dimensions (specificity, category fit, merchant fit, decision quality, engagement; 0–10 each).
- Live: https://vera-bot-sj6m.onrender.com (Render free tier, auto-deploys from GitHub `PrathamMalik/VERA-BOT`, branch `main`)
- Challenge brief and testing brief: `docs/`
- Every behaviour decision and its reason: **`DECISIONS.md`** (read it before changing behaviour)
- Plain-English explanation for interviews: `HOW_IT_WORKS.md`

## Working rules (important)
- **Pratham makes the product decisions.** He is a business/mechanical-engineering person, not a CS major. For any change to *what the bot says or does*, show 1–2 concrete example messages (before/after), then ask; don't decide silently. Pure engineering fixes (bugs, tests, tooling) are fine to just do.
- Explain steps plainly and one at a time; give exact commands to copy.
- **Never** put API keys in code, files, commits or chat. Keys live only in env vars (`LLM_API_KEY` on Render; `JUDGE_API_KEY` in the local shell).
- **Held-out eval set (`eval/holdout_set.json`) is sealed:** use it only for aggregate scores. Never print or read its message texts, and never tune on it.
- Log every accepted behaviour change in `DECISIONS.md` (short: what + why + evidence).

## Architecture (stdlib-only Python 3.11, no pip deps)
- `bot.py`: HTTP server: `/v1/healthz`, `/v1/metadata`, `/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/teardown`
- `vera/templates.py`: one handler per trigger kind → rules-only `Draft`; unknown kinds → `resolve_alias` / `h_reader`; fit/dissociation table (`FIT`), approval drafts, brand line, strengths-first pattern
- `vera/composer.py`: at most ONE AI call per message → validator → quality floor → gym voice layer
- `vera/quality.py`: quality floor (name, one CTA at end, no internal labels); fail → rules text → else skip
- `vera/peers.py`: anonymous peer averages (≥3 merchants, same city first) and case-study patterns
- `vera/conversation.py`: reply router (auto-reply, opt-out, hostile, defer, yes→action, own-tagline)
- `vera/engine.py`: tick selection rules; `vera/llm.py`: Gemini client + rate limiter; `vera/util.py`: language, formatting

## Core strategy (from measured eval rounds, see DECISIONS.md)
Code owns every fact; the AI only picks/phrases. Each message: why-now first line (R1), evidence that matches the trigger (R2), every number with its source (R3), one story (R4), strengths first, one CTA, ≤1 emoji. Restraint: skip when there's nothing honest to say or the trigger doesn't fit the business (dissociation ≥ 2). Language follows the category's `voice.code_mix` (gyms = English-primary).

## Commands
```bash
python3 bot.py                                  # run locally on :8080 (PORT env to change)
bash scripts/restart.sh                         # restart local server (pid-file based)
python3 scripts/harness.py                      # end-to-end self-test (needs server running) → "ALL CHECKS PASSED"
python3 scripts/test_llm_path.py                # AI-path checks with a fake model
python3 scripts/test_tick_rules.py              # tick selection rules
python3 scripts/test_time.py                    # judge-clock / date logic
python3 scripts/sim_mock.py                     # magicpin judge_simulator flows with a mock LLM
python3 scripts/make_submission.py              # regenerate submission.jsonl
JUDGE_API_KEY=mock python3 scripts/eval_real.py # offline eval smoke test (no API calls)
python3 scripts/eval_compare.py                 # REAL eval: new vs baseline/ on dev + held-out (needs JUDGE_API_KEY; ~20-25 min)
```
- Eval uses Gemini free tier: bot model `gemini-3.5-flash-lite` (15 RPM, 500/day), judge `gemini-3.1-flash-lite` (separate quota). Judge noise ≈ ±0.4 points.
- `baseline/vera/` = the previous version, used by `eval_compare.py` for the held-out comparison. Update it to the current `vera/` after a version is accepted.
- macOS gotcha: if Python raises CERTIFICATE_VERIFY_FAILED, run `"/Applications/Python 3.12/Install Certificates.command"`.

## Deploy
Commit + push to `main` → Render redeploys (~3 min). Check `/v1/healthz` and `/v1/metadata` (model should read `gemini:gemini-3.5-flash-lite`). Render env vars: `LLM_PROVIDER=gemini`, `LLM_API_KEY`, `LLM_MODEL=gemini-3.5-flash-lite`, `LLM_RPM=12`, `PYTHON_VERSION=3.11.9`, `TEAM_NAME`, `TEAM_MEMBERS`, `CONTACT_EMAIL` (college email = registration email).

## Status / next steps
- Eval history (avg /50): round 1 dev 38.7 / held-out 38.4 → round 2 dev 40.6 / held-out 40.3 (below-30 messages 3→0; coverage 98%→82%).
- Round 3 → dev 41.1 / held-out 41.1; round 3.1 → held-out 41.5, fresh set 39.5 (vs round 2 39.1). Round 3.1 + heatwave-skip shipped.
- `eval/fresh_set.json` (48, sealed) = the overfitting check; use it for every future round.
- Round 4 (unknown trigger types → rate good/bad −2..+2 → tone template + trigger reader; newest-research floor): novel set 38.4 vs 28.8 (+9.6, paired 24/1/2). **Shipped.** `eval/novel_set.json` (30, sealed, written blind) = the new-trigger test.
- Open question: language for merchants listing Hindi + a southern language (judge docked English once).
- Before submitting: set up a free keep-awake ping (cron-job.org → `/v1/healthz` every 10 min); submit the base URL on the magicpin form.
