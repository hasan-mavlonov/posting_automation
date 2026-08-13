# MindForm social pipeline (v1 — X only)

An always-on Python service that turns MindForm AI's research and dev activity
into approved X posts:

1. **Watches** the public GitHub repo `hasan-mavlonov/mindform_v0` (new commits
   and releases) and the Zenodo community *MindForm AI Research* (new records)
   on a configurable schedule. Seen items are tracked in a local SQLite file so
   nothing is processed twice.
2. **Drafts** a post for each new item with Claude, grounded strictly in the
   commit message / release notes / record description — under 280 characters,
   or a short numbered thread when the content needs the room.
3. **Sends** each draft to you on Telegram with three buttons: **Approve**,
   **Edit**, **Reject**. Edit asks you to reply with the corrected text, which
   becomes the new draft and is re-sent with the same buttons.
4. **Publishes** approved drafts to your connected X profile through the
   Buffer API.

Everything runs in a single long-lived process (`main.py`): the Telegram bot's
update loop and the source watcher share one asyncio event loop.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill it in
python main.py
```

The service reads configuration from environment variables (a local `.env`
file is loaded automatically). It **fails loudly at startup** if any required
credential is missing — it never silently no-ops.

### Filling in `.env`

| Variable | Required | How to get it |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | ✅ | Create a key at platform.claude.com. |
| `TELEGRAM_BOT_TOKEN` | ✅ | Create a bot with [@BotFather](https://t.me/BotFather). |
| `TELEGRAM_CHAT_ID` | ✅ | Start a chat with your bot, run the service once with a placeholder value — or easier: message the bot `/id` once it's running, it replies with the chat id. Alternatively use [@userinfobot](https://t.me/userinfobot). |
| `BUFFER_ACCESS_TOKEN` | ✅ | Buffer account → developer apps → access token. |
| `BUFFER_PROFILE_ID` | ✅ | `curl "https://api.bufferapp.com/1/profiles.json?access_token=..."` and copy the `id` of your X profile. |
| `GITHUB_TOKEN` | — | Optional; raises the GitHub API rate limit. |
| `GITHUB_REPO` | — | Defaults to `hasan-mavlonov/mindform_v0`. |
| `ZENODO_COMMUNITY` | — | Defaults to `mindform-ai-research`. Use the identifier from your community's Zenodo URL. |
| `CHECK_INTERVAL_MINUTES` | — | Defaults to 180 (every 3 hours). |
| `ANTHROPIC_MODEL` | — | Defaults to `claude-opus-5`. |
| `DB_PATH` | — | Defaults to `./posting_automation.db`. |
| `PROCESS_BACKLOG_ON_FIRST_RUN` | — | By default the first check per source marks all existing history as *seen* without posting, so you aren't flooded with the backlog. Set `true` to draft the backlog too. |
| `MAX_ITEMS_PER_CYCLE` | — | Defaults to 10 drafts per source per cycle; the rest carry over. |

### The review flow in Telegram

- **✅ Approve** — publishes immediately via Buffer. Thread parts are posted as
  sequential separate posts (the classic Buffer API has no native thread
  support).
- **✏️ Edit** — the bot asks you to reply with the corrected text. Separate
  thread posts with a line containing only `---`. The edited draft is re-sent
  with the same three buttons.
- **🚫 Reject** — marks the draft dismissed and moves on.

If publishing fails before anything went out, the draft stays actionable and
you can hit Approve again. If it fails midway through a thread, the draft is
marked `failed` and the message tells you how many parts went out, so you can
resolve it manually without double-posting.

## Deploying on Render

Create a **Background Worker** (not a Web Service — the process serves no HTTP)
pointing at this repo, with build command `pip install -r requirements.txt` and
start command `python main.py`. Add the environment variables from the table
above in the Render dashboard. One caveat: the SQLite state file lives on the
service's disk, and Render's default disk is ephemeral — attach a small
persistent disk and point `DB_PATH` at it (e.g. `/var/data/posting_automation.db`),
otherwise a redeploy forgets what was already posted (the first-run baseline
prevents re-posting old history, but items that arrived while the service was
down would be baselined too). Logs go to stdout and show every check, draft,
and publish, so Render's log view tells you exactly what the worker is doing.

## Notes

- Buffer stopped issuing new developer-app registrations years ago; this uses
  the classic `api.bufferapp.com` API, which keeps working for existing tokens.
- The GitHub poll reads the latest 30 commits and 15 releases per cycle; if
  more than that lands between two checks, the overflow is not picked up. At a
  3-hour interval this is unlikely to matter.
- Explicitly out of scope for v1: Discord/Reddit/Hacker News, a web dashboard,
  and send-time optimization (Buffer handles scheduling).
