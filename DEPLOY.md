# Deploying the bot (about 15 minutes, no coding)

The judge needs a public web address for your bot. The simplest route is **GitHub + Render**, both free.

## 1. Put the code on GitHub
1. Create an account at github.com.
2. Click **New repository**, name it `vera-bot`, set it to **Private**, and click **Create**.
3. On the empty repo page, click **uploading an existing file**.
4. Drag in everything from this folder (including the `vera/` and `dataset/` folders) and click **Commit changes**.

## 2. Run it on Render
1. Create an account at render.com and sign in with GitHub.
2. Click **New → Web Service** and pick your `vera-bot` repo.
3. Fill in the settings:
   - Runtime: **Python 3**
   - Build command: `echo ok`
   - Start command: `python bot.py`
   - Instance type: **Free**
4. Under **Environment variables**, add:
   - `LLM_PROVIDER` = `gemini`
   - `LLM_API_KEY` = your free Gemini key (see "Getting the free Gemini key" below). Leave it out to run in rules-only mode.
   - `LLM_MODEL` = `gemini-3.5-flash-lite` (free tier: 15 requests/min, 500/day)
   - `LLM_RPM` = `12` (stays under the 15/min limit)
   - `TEAM_NAME`, `TEAM_MEMBERS`, `CONTACT_EMAIL` = your details
5. Click **Create Web Service**. After a minute or two you'll get a URL like `https://vera-bot-xxxx.onrender.com`.

## 3. Check it works
Open `https://<your-url>/v1/healthz` in a browser. You should see `{"status": "ok", ...}`.
Then submit **that base URL** (without `/v1/...`) on the challenge portal.

## Getting the free Gemini key (no card)
1. Go to **aistudio.google.com** and sign in.
2. **API Keys → Create API key**, choose **Default Gemini Project**. Copy it (new keys start with `AQ.`; older ones with `AIza` — both work).
3. Open **Rate Limit**: the page must say **Free tier** with no "API access is restricted" banner. If you see that banner, don't add a card; use a different Google account you already own, or Groq (free, `LLM_PROVIDER=groq`).
4. Paste the key into Render as `LLM_API_KEY`. Never put it in the code or on GitHub.

## Important: free servers fall asleep
Free Render services typically go to sleep after roughly 15 minutes idle, and take 30–60 seconds to wake up. **Open the healthz link 5 minutes before the test starts.** After that the judge's regular checks (every 60 seconds) keep it awake.
If you want zero risk, Render's cheapest paid tier or Railway doesn't sleep.

## Testing locally (optional)
```bash
python bot.py                      # terminal 1
python scripts/harness.py          # terminal 2: full self-test, should print ALL CHECKS PASSED
```
**magicpin's own practice judge, with real Gemini scores** (run in Terminal from the vera-bot folder):
```bash
export JUDGE_API_KEY="<your key>"          # typed in Terminal only; never saved in a file
export BOT_URL="https://<your-render-url>"
python3 judge_simulator.py
```
- The judge uses `gemini-3.1-flash-lite` and the bot uses `gemini-3.5-flash-lite`. Google counts limits per model, so they don't eat each other's quota.
- It prints a score out of 50 per message with the judge's reasons. If you hit a limit, wait a minute and run again.
