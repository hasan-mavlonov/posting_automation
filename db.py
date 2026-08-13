"""SQLite state: seen source items, drafts and their review status.

A single connection guarded by a lock — handlers run on the event loop and a
few calls come from worker threads, so cross-thread access must be safe.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_items (
    source      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    first_seen  TEXT NOT NULL,
    PRIMARY KEY (source, external_id)
);

CREATE TABLE IF NOT EXISTS drafts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source              TEXT NOT NULL,
    external_id         TEXT NOT NULL,
    item_title          TEXT NOT NULL,
    item_url            TEXT NOT NULL,
    posts_json          TEXT NOT NULL,
    status              TEXT NOT NULL,
    telegram_message_id INTEGER,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (source, external_id)
);

CREATE TABLE IF NOT EXISTS edit_prompts (
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    draft_id   INTEGER NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);

CREATE TABLE IF NOT EXISTS pending_items (
    source      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    url         TEXT NOT NULL,
    queued_at   TEXT NOT NULL,
    PRIMARY KEY (source, external_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS input_prompts (
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
"""

# Draft status lifecycle:
#   pending        -> waiting for Approve / Edit / Reject in Telegram
#   awaiting_edit  -> Edit pressed, waiting for replacement text
#   publishing     -> Approve pressed, Buffer call in flight; a draft stuck
#                     here means the process died mid-publish - verify on
#                     Buffer/X and resolve in the DB by hand
#   published      -> sent to Buffer
#   rejected       -> dismissed by the reviewer
#   failed         -> publishing partially failed; needs manual attention


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # --- seen items -------------------------------------------------------

    def is_seen(self, source: str, external_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM seen_items WHERE source = ? AND external_id = ?",
                (source, external_id),
            ).fetchone()
        return row is not None

    def seen_count(self, source: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM seen_items WHERE source = ?", (source,)
            ).fetchone()
        return int(row["n"])

    def mark_seen(self, source: str, external_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO seen_items (source, external_id, first_seen) VALUES (?, ?, ?)",
                (source, external_id, _now()),
            )
            self._conn.commit()

    # --- drafts -----------------------------------------------------------

    def create_draft(
        self, source: str, external_id: str, item_title: str, item_url: str, posts: list[str]
    ) -> int:
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO drafts (source, external_id, item_title, item_url, posts_json,"
                " status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                (source, external_id, item_title, item_url, json.dumps(posts), now, now),
            )
            self._conn.commit()
        return int(cur.lastrowid)

    def get_draft(self, draft_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM drafts WHERE id = ?", (draft_id,)
            ).fetchone()

    def find_draft(self, source: str, external_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM drafts WHERE source = ? AND external_id = ?"
                " ORDER BY id DESC LIMIT 1",
                (source, external_id),
            ).fetchone()

    def set_draft_status(self, draft_id: int, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE drafts SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now(), draft_id),
            )
            self._conn.commit()

    def update_draft_posts(self, draft_id: int, posts: list[str], status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE drafts SET posts_json = ?, status = ?, updated_at = ? WHERE id = ?",
                (json.dumps(posts), status, _now(), draft_id),
            )
            self._conn.commit()

    def set_draft_message(self, draft_id: int, message_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE drafts SET telegram_message_id = ?, updated_at = ? WHERE id = ?",
                (message_id, _now(), draft_id),
            )
            self._conn.commit()

    def drafts_awaiting_edit(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM drafts WHERE status = 'awaiting_edit' ORDER BY id"
            ).fetchall()

    def drafts_by_status(self, statuses: tuple[str, ...], limit: int) -> list[sqlite3.Row]:
        placeholders = ", ".join("?" for _ in statuses)
        with self._lock:
            return self._conn.execute(
                f"SELECT * FROM drafts WHERE status IN ({placeholders})"
                " ORDER BY id DESC LIMIT ?",
                (*statuses, limit),
            ).fetchall()

    def last_published_draft(self) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM drafts WHERE status = 'published'"
                " ORDER BY updated_at DESC, id DESC LIMIT 1"
            ).fetchone()

    # --- settings ---------------------------------------------------------

    def get_setting(self, key: str, default: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row is not None else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value)
            )
            self._conn.commit()

    # --- input prompts (force-reply questions that expect free text) ------

    def add_input_prompt(self, chat_id: int, message_id: int, kind: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO input_prompts (chat_id, message_id, kind)"
                " VALUES (?, ?, ?)",
                (chat_id, message_id, kind),
            )
            self._conn.commit()

    def pop_input_prompt(self, chat_id: int, message_id: int) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT kind FROM input_prompts WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "DELETE FROM input_prompts WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            )
            self._conn.commit()
        return str(row["kind"])

    # --- pending items (per-cycle cap overflow) ---------------------------

    def queue_pending_item(
        self, source: str, external_id: str, title: str, body: str, url: str
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO pending_items"
                " (source, external_id, title, body, url, queued_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (source, external_id, title, body, url, _now()),
            )
            self._conn.commit()

    def pending_items(self, source: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM pending_items WHERE source = ? ORDER BY rowid", (source,)
            ).fetchall()

    def remove_pending_item(self, source: str, external_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM pending_items WHERE source = ? AND external_id = ?",
                (source, external_id),
            )
            self._conn.commit()

    # --- edit prompts -----------------------------------------------------

    def add_edit_prompt(self, chat_id: int, message_id: int, draft_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO edit_prompts (chat_id, message_id, draft_id) VALUES (?, ?, ?)",
                (chat_id, message_id, draft_id),
            )
            self._conn.commit()

    def pop_edit_prompt(self, chat_id: int, message_id: int) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT draft_id FROM edit_prompts WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "DELETE FROM edit_prompts WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            )
            self._conn.commit()
        return int(row["draft_id"])

    def close(self) -> None:
        with self._lock:
            self._conn.close()
