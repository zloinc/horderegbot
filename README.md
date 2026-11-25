# Horde watch (WoW Forever, EU PvP)

Checks public sources every 5 minutes and sends a Telegram message when:

- 🟢 a player on the Blizzard EU forum reports that Horde character creation is open
- 🔵 Blizzard staff post about the faction lock (EU or US forum)
- 📰 Wowhead publishes news about the lock being lifted

It only reads public web pages. It never logs in to the game or Battle.net.

## Setup (about 10 minutes)

### 1. Create the Telegram bot
1. In Telegram, open **@BotFather**, send `/newbot`, pick a name. Copy the **token** it gives you.
2. Open your new bot and press **Start** (send it any message).
3. In a browser, open `https://api.telegram.org/bot<TOKEN>/getUpdates` with your token in place of `<TOKEN>`.
   Find `"chat":{"id":123456789` — that number is your **chat id**.

To alert your friends too: each friend presses Start on the bot, you reload the
page above to see their ids, and you list all ids separated by commas
(`111,222,333`). Or add the bot to a group chat and use the group's id (a negative number).

### 2. Create the GitHub repository
1. On github.com, create a new **public** repository (for example `horde-watch`).
   Public matters: GitHub Actions is free without limit on public repositories.
   A private one would use up the free 2,000 minutes in about a week.
   Your token stays hidden either way.
2. Add these files, keeping the paths exactly:
   - `watcher.py`
   - `.github/workflows/watch.yml` (use **Add file → Create new file** and type the
     full path as the file name; GitHub creates the folders)

### 3. Add the secrets
Repository → **Settings → Secrets and variables → Actions → New repository secret**:

- `TELEGRAM_BOT_TOKEN` = the token from BotFather
- `TELEGRAM_CHAT_ID` = your chat id (or several, comma-separated)

### 4. Test it
Repository → **Actions → Horde watch → Run workflow**, tick **test**, run.
Within a minute you get a Telegram message listing each source with ✅ or ❌.
At least one "EU forum" line must be ✅. After that the schedule runs by itself;
the first scheduled run sends "Horde watcher is live".

## Good to know

- **Timing.** GitHub's schedule is "about every 5 minutes"; at busy times runs
  can start 10–20 minutes late or be skipped.
- **Daily ping.** Once a day (after 08:00 UTC) you get a silent "still watching"
  message, so silence means "nothing found", not "broken". Set
  `HEARTBEAT_HOUR_UTC = None` in `watcher.py` to turn it off.
- **If the forum is unreachable** three runs in a row, you get one warning.
- **False alarms and misses.** Alerts come from what players write, so some will
  be wrong and a short opening that nobody posts about will be missed.
- **60-day rule.** GitHub pauses schedules in repositories with no activity for
  60 days and emails you a link to re-enable. The daily state update normally
  counts as activity.
- **Stop it.** Actions → Horde watch → "…" → Disable workflow.
