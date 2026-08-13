# MindForm social pipeline (v1 — X only)

An always-on Python service that turns MindForm AI's research and dev activity
into approved X posts:

1. **Watches** the public GitHub repo `hasan-mavlonov/mindform_v0` and the
   MindForm Zenodo community on a configurable schedule. By default releases
   and Zenodo records auto-draft; commit posts are generated on demand from
   `/menu` (tune with `WATCH_*`). Seen items are tracked in a local SQLite
   file so nothing is processed twice.
2. **Drafts** a post for each new item with Claude, grounded strictly in the
   source material — under 280 characters, or a short numbered thread when the
   content needs the room. Releases, papers, and topic posts are written in
   the founder's voice (hook first, short lines, link at the end); commit
   posts stay plain dev updates.
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
| `ZENODO_COMMUNITY` | — | The slug from your community's URL (`zenodo.org/communities/<slug>`). Defaults to `mindform-ai`. |
| `WATCH_COMMITS` / `WATCH_RELEASES` / `WATCH_ZENODO` | — | Which sources auto-draft. Defaults: commits **off** (generate dev-update posts on demand from `/menu`), releases and Zenodo **on**. |
| `CHECK_INTERVAL_MINUTES` | — | Defaults to 180 (every 3 hours). |
| `ANTHROPIC_MODEL` | — | Defaults to `claude-opus-5`. |
| `DB_PATH` | — | Defaults to `./posting_automation.db`. |
| `PROCESS_BACKLOG_ON_FIRST_RUN` | — | By default the first check per source marks all existing history as *seen* without posting, so you aren't flooded with the backlog. Set `true` to draft the backlog too. |
| `MAX_ITEMS_PER_CYCLE` | — | Defaults to 10 drafts per source per cycle; the overflow is persisted and drafted on later cycles. |

### The interactive menu

Send `/menu` (or `/start`) to the bot to drive it directly instead of waiting
for the watcher:

- **Channel picker** → 𝕏 (X is the only channel in v1).
- **✍️ Generate a post** — on demand, from the latest commit, latest release,
  latest Zenodo record, or a free-typed topic (reply with your text and Claude
  drafts a post grounded in exactly what you wrote). Generated drafts go
  through the same Approve / Edit / Reject review as automatic ones.
- **🕓 Last post** — shows the most recently published draft.
- **📝 Pending drafts** — re-sends up to 5 drafts still awaiting action, with
  their buttons.
- **⏸/▶️ Pause / resume auto-drafting** — turns the automatic source watcher
  off/on (persists across restarts). Paused means fully interactive mode:
  posts happen only when you generate them.
- **🔄 Check sources now** — runs a source check immediately instead of
  waiting for the next scheduled cycle. Handy for testing.

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
resolve it manually without double-posting. If the service dies while a
publish was in flight, the draft is left in a `publishing` state that blocks
re-approval — check Buffer/X to see what actually went out before resolving
it in the database.

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

## Tests

```bash
python -m tests.test_flows
```

Offline tests (no network, no credentials) that exercise the real handlers
against an in-memory database: menu navigation, on-demand generation, the
topic flow, every approve/publish outcome, the whole edit flow, and the
watcher cycle (baseline, cap overflow, restart healing). Run them before
pushing changes.

## Troubleshooting

- **`telegram.error.TimedOut` at startup** — the machine couldn't reach
  `api.telegram.org` (flaky Wi-Fi, VPN, or an ISP that throttles Telegram).
  The service now retries the connection with backoff instead of exiting, so
  it recovers by itself when the network does.
- **`telegram.error.Conflict: terminated by other getUpdates request`** — two
  copies of the service are polling the same bot token. Telegram allows only
  one. Check for a second terminal still running `main.py`, or a deployed
  copy (e.g. the Render worker) running alongside your local one. Stop all
  but one.
- **`Failed to fetch zenodo: ... Connection refused`** (or similar network
  errors for GitHub) — the machine couldn't reach the API at that moment.
  This is non-fatal: the error is logged and the source is retried on the
  next cycle; nothing is lost.

## Notes

- Buffer stopped issuing new developer-app registrations years ago; this uses
  the classic `api.bufferapp.com` API, which keeps working for existing tokens.
- The GitHub poll reads the latest 30 commits and 15 releases per cycle; if
  more than that lands between two checks, the overflow is not picked up. At a
  3-hour interval this is unlikely to matter. (Items that *were* observed but
  deferred by `MAX_ITEMS_PER_CYCLE` are persisted and never lost.)
- Post lengths are validated as plain character counts. X weighs most emoji
  and CJK characters as 2, so a hand-edited draft heavy in emoji can pass the
  280 check here and still be rejected by X downstream. The Claude drafts
  avoid emoji, so this only affects manual edits.
- Explicitly out of scope for v1: Discord/Reddit/Hacker News, a web dashboard,
  and send-time optimization (Buffer handles scheduling).
