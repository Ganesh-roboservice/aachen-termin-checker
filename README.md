# Aachen Termin Checker

Checks the StädteRegion Aachen Ausländerbehörde booking site every 10 minutes
(06:00–22:00 Berlin time) for **Infostelle → Beratungs- und Antragsservice**
at Aachen Arkaden, and sends you a phone notification when a date earlier than
`BEFORE_DATE` (default 23.12.2026) appears.

It only checks and notifies. You book the slot yourself on
https://termine.staedteregion-aachen.de/auslaenderamt/select2?md=1

## Setup (about 10 minutes)

### 1. Create the repository
1. On GitHub, create a new **public** repository (public repos get unlimited
   free Actions minutes; nothing personal is stored in it).
2. Upload all files from this folder, including the hidden `.github` folder.
   Easiest: `git init`, `git add .`, `git commit -m init`, `git push`.

### 2. Choose how you get notified (one is enough)

**Option A – ntfy (fastest, no account):**
1. Install the **ntfy** app (Android / iOS).
2. Subscribe to a long random topic name (e.g. generate one with
   `openssl rand -hex 12`). Don't reuse an example from anywhere public:
   anyone who knows the topic can read its messages.
3. Add it as a repository secret named `NTFY_TOPIC`.

**Option B – Telegram:**
1. In Telegram, message **@BotFather** → `/newbot` → copy the token.
2. Send any message to your new bot.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `"chat":{"id":...}`.
4. Add secrets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

Secrets: repository → **Settings → Secrets and variables → Actions → New repository secret**.

### 3. Optional: change the cut-off date
Settings → Secrets and variables → Actions → **Variables** tab →
add `BEFORE_DATE` = e.g. `2026-11-30`.

### 4. Test it
**Actions** tab → **Check Aachen Termin** → **Run workflow**:
- tick *Send a test notification* → you should get a message on your phone.
- run again without the tick → the log shows the dates it found.

## Notes
- GitHub's schedule is best-effort: runs can be delayed by several minutes.
  Slots go fast, so open the booking page as soon as the alert arrives.
- The alert's link jumps straight to the location step (click "Aachen Arkaden
  auswählen" to see the dates). The start page link is included as a fallback.
- Each date is alerted once. If it disappears and reappears, you're alerted again.
- If the site changes its layout or the run ends up on an unexpected page, it
  fails with `FLOW ERROR` and GitHub emails you about the failed workflow.
- Scheduled workflows pause after 60 days without repository activity, so the
  workflow pushes an empty "Keepalive" commit when the last commit is 30 days old.
- When you have your appointment, disable the workflow (Actions → ⋯ → Disable).
