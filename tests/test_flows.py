"""Offline tests for the bot's real handler and watcher flows.

No network, no Telegram: handlers only touch attributes on the update/context
objects, so lightweight fakes stand in for PTB types, external calls
(publish_posts, source fetchers, the drafter) are stubbed, and the SQLite
layer runs in memory.

Run directly (python -m tests.test_flows) or via pytest.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config
from db import Database
from publisher import PublishError
from sources import SourceItem
import telegram_bot as tb
import watcher


# --- fakes ----------------------------------------------------------------


class FakeSentMessage:
    def __init__(self, message_id: int):
        self.message_id = message_id
        self.edits: list[str] = []

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)


class FakeBot:
    def __init__(self):
        self.sent: list[dict] = []
        self._next_id = 100

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self._next_id += 1
        self.sent.append(
            {"chat_id": chat_id, "text": text, "reply_markup": reply_markup,
             "message_id": self._next_id}
        )
        return FakeSentMessage(self._next_id)


class FakeQuery:
    def __init__(self, data: str):
        self.data = data
        self.answered = False
        self.edits: list[dict] = []

    async def answer(self):
        self.answered = True

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.edits.append({"text": text, "reply_markup": reply_markup})


class FakeChat:
    def __init__(self, chat_id: int):
        self.id = chat_id


class FakeIncomingMessage:
    """A text message from the operator (for on_text)."""

    def __init__(self, text: str, bot: FakeBot, reply_to_id: int | None = None):
        self.text = text
        self._bot = bot
        self.reply_to_message = FakeSentMessage(reply_to_id) if reply_to_id else None
        self.replies: list[dict] = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self._bot._next_id += 1
        self.replies.append({"text": text, "message_id": self._bot._next_id})
        return FakeSentMessage(self._bot._next_id)


class FakeUpdate:
    def __init__(self, chat_id: int, query: FakeQuery | None = None,
                 message: FakeIncomingMessage | None = None):
        self.callback_query = query
        self.effective_chat = FakeChat(chat_id)
        self.effective_message = message


class FakeContext:
    def __init__(self, bot: FakeBot, bot_data: dict):
        self.bot = bot
        self.bot_data = bot_data


class StubDrafter:
    def __init__(self, posts=None):
        self.posts = posts or ["stub post"]
        self.drafted: list[SourceItem] = []

    async def draft_posts(self, item: SourceItem) -> list[str]:
        self.drafted.append(item)
        return list(self.posts)


CHAT_ID = 42


def make_env(**drafter_kwargs):
    config = Config(
        anthropic_api_key="k", telegram_bot_token="t", telegram_chat_id=CHAT_ID,
        buffer_access_token="b", buffer_profile_id="p", github_token="",
        github_repo="owner/repo", zenodo_community="community",
        check_interval_minutes=1, anthropic_model="claude-opus-5",
        db_path=":memory:", process_backlog_on_first_run=False,
        max_items_per_cycle=2,
    )
    db = Database(":memory:")
    bot = FakeBot()
    bot_data = {
        "config": config, "db": db, "drafter": StubDrafter(**drafter_kwargs),
        "wake_event": asyncio.Event(),
    }
    return config, db, bot, FakeContext(bot, bot_data)


def item(source="github_commit", eid="sha1", title="Fix parser", url="http://u/1"):
    return SourceItem(source=source, external_id=eid, title=title, body="body text", url=url)


async def press(context, data: str) -> FakeQuery:
    query = FakeQuery(data)
    update = FakeUpdate(CHAT_ID, query=query)
    if data.split(":")[0] in ("menu", "xpanel", "gen"):
        await tb.on_menu_callback(update, context)
    else:
        await tb.on_callback(update, context)
    assert query.answered
    return query


# --- tests ----------------------------------------------------------------


def test_menu_navigation():
    async def run():
        _, _, _, context = make_env()
        q = await press(context, "menu:main")
        assert "channel" in q.edits[-1]["text"].lower()
        q = await press(context, "menu:x")
        assert "Auto-drafting" in q.edits[-1]["text"]
        q = await press(context, "menu:gen")
        assert "Generate" in q.edits[-1]["text"]

    asyncio.run(run())


def test_auto_toggle_and_check_now():
    async def run():
        _, db, bot, context = make_env()
        assert db.get_setting("auto_drafting", "on") == "on"
        await press(context, "xpanel:auto")
        assert db.get_setting("auto_drafting", "on") == "off"
        await press(context, "xpanel:auto")
        assert db.get_setting("auto_drafting", "on") == "on"

        await press(context, "xpanel:check")
        assert context.bot_data["force_cycle"] is True
        assert context.bot_data["wake_event"].is_set()
        assert any("Checking" in m["text"] for m in bot.sent)

    asyncio.run(run())


def test_generate_from_latest_commit():
    async def run():
        _, db, bot, context = make_env()
        latest = item(eid="newsha", title="Newest commit")
        original = tb.FETCHER_BY_SOURCE["github_commit"]
        tb.FETCHER_BY_SOURCE["github_commit"] = lambda config: [item(eid="old"), latest]
        try:
            q = await press(context, "gen:github_commit")
        finally:
            tb.FETCHER_BY_SOURCE["github_commit"] = original

        drafted = context.bot_data["drafter"].drafted
        assert [i.external_id for i in drafted] == ["newsha"]  # newest, not oldest
        draft = db.get_draft(1)
        assert draft["status"] == "pending"
        assert draft["external_id"].startswith("newsha@")  # no UNIQUE collision
        assert not db.is_seen("github_commit", "newsha")  # watcher dedup untouched
        assert any("Draft #1" in m["text"] for m in bot.sent)  # review message sent
        assert "✅" in q.edits[-1]["text"]

    asyncio.run(run())


def test_topic_flow():
    async def run():
        _, db, bot, context = make_env()
        await press(context, "gen:topic")
        prompt_id = bot.sent[-1]["message_id"]
        assert db.pop_input_prompt(CHAT_ID, prompt_id) == "topic"
        db.add_input_prompt(CHAT_ID, prompt_id, "topic")  # restore for the reply

        message = FakeIncomingMessage("we hit 1k stars", bot, reply_to_id=prompt_id)
        await tb.on_text(FakeUpdate(CHAT_ID, message=message), context)

        draft = db.get_draft(1)
        assert draft is not None and draft["source"] == "topic" and draft["item_url"] == ""
        rendered = tb.format_draft(draft, json.loads(draft["posts_json"]))
        assert "http" not in rendered
        assert context.bot_data["drafter"].drafted[0].body == "we hit 1k stars"

    asyncio.run(run())


def test_approve_paths():
    async def run():
        _, db, _, context = make_env()

        # success
        d1 = db.create_draft("github_commit", "s1", "T", "http://u", ["p1", "p2"])
        published = []
        tb_publish = tb.publish_posts
        tb.publish_posts = lambda config, posts: published.append(posts) or ["id1", "id2"]
        try:
            await press(context, f"approve:{d1}")
        finally:
            tb.publish_posts = tb_publish
        assert published == [["p1", "p2"]]  # both parts, in order
        assert db.get_draft(d1)["status"] == "published"

        # clean failure before anything went out -> back to pending, retryable
        d2 = db.create_draft("github_commit", "s2", "T", "http://u", ["p"])
        def fail_clean(config, posts):
            raise PublishError("boom", published_count=0)
        tb.publish_posts = fail_clean
        try:
            q = await press(context, f"approve:{d2}")
        finally:
            tb.publish_posts = tb_publish
        assert db.get_draft(d2)["status"] == "pending"
        assert q.edits[-1]["reply_markup"] is not None  # buttons re-offered

        # partial thread failure -> failed, no buttons
        d3 = db.create_draft("github_commit", "s3", "T", "http://u", ["a", "b"])
        def fail_partial(config, posts):
            raise PublishError("mid-thread", published_count=1)
        tb.publish_posts = fail_partial
        try:
            q = await press(context, f"approve:{d3}")
        finally:
            tb.publish_posts = tb_publish
        assert db.get_draft(d3)["status"] == "failed"
        assert q.edits[-1]["reply_markup"] is None

        # terminal guard: approving again does nothing
        q = await press(context, f"approve:{d1}")
        assert "Already handled" in q.edits[-1]["text"]

        # stuck-publishing guard
        d4 = db.create_draft("github_commit", "s4", "T", "http://u", ["p"])
        db.set_draft_status(d4, "publishing")
        q = await press(context, f"approve:{d4}")
        assert "in flight" in q.edits[-1]["text"]
        assert db.get_draft(d4)["status"] == "publishing"

    asyncio.run(run())


def test_edit_flow():
    async def run():
        _, db, bot, context = make_env()
        d1 = db.create_draft("github_commit", "s1", "T", "http://u", ["original"])

        await press(context, f"edit:{d1}")
        assert db.get_draft(d1)["status"] == "awaiting_edit"
        prompt_id = bot.sent[-1]["message_id"]

        # stray text (not a reply) must never become content
        stray = FakeIncomingMessage("note to self", bot)
        await tb.on_text(FakeUpdate(CHAT_ID, message=stray), context)
        assert json.loads(db.get_draft(d1)["posts_json"]) == ["original"]
        assert "Reply directly" in stray.replies[-1]["text"]

        # separator-only reply: draft unchanged, prompt re-armed
        empty = FakeIncomingMessage("---", bot, reply_to_id=prompt_id)
        await tb.on_text(FakeUpdate(CHAT_ID, message=empty), context)
        assert json.loads(db.get_draft(d1)["posts_json"]) == ["original"]
        prompt_id = empty.replies[-1]["message_id"]  # re-registered prompt

        # too many parts
        many = FakeIncomingMessage("\n---\n".join(f"p{i}" for i in range(6)), bot,
                                   reply_to_id=prompt_id)
        await tb.on_text(FakeUpdate(CHAT_ID, message=many), context)
        assert "limit is a thread" in many.replies[-1]["text"]
        prompt_id = many.replies[-1]["message_id"]

        # too long
        long_msg = FakeIncomingMessage("x" * 300, bot, reply_to_id=prompt_id)
        await tb.on_text(FakeUpdate(CHAT_ID, message=long_msg), context)
        assert "Too long" in long_msg.replies[-1]["text"]
        prompt_id = long_msg.replies[-1]["message_id"]

        # valid edit with leading/trailing separators
        good = FakeIncomingMessage("---\n1/ first\n---\n2/ second\n---", bot,
                                   reply_to_id=prompt_id)
        await tb.on_text(FakeUpdate(CHAT_ID, message=good), context)
        draft = db.get_draft(d1)
        assert draft["status"] == "pending"
        assert json.loads(draft["posts_json"]) == ["1/ first", "2/ second"]
        assert any(f"Draft #{d1}" in m["text"] for m in bot.sent)  # re-sent for review

    asyncio.run(run())


def test_watcher_cycle():
    async def run():
        from types import SimpleNamespace

        _, db, bot, context = make_env()
        app = SimpleNamespace(bot_data=context.bot_data, bot=bot)

        commits = [item(eid=f"sha{i}", title=f"c{i}", url=f"http://u/{i}") for i in range(5)]
        original = watcher.SOURCE_FETCHERS
        watcher.SOURCE_FETCHERS = [("github_commit", lambda config: list(commits))]
        try:
            # cycle 1: first run baselines everything, drafts nothing
            await watcher.run_cycle(app)
            assert context.bot_data["drafter"].drafted == []
            assert db.is_seen("github_commit", "sha0")
            assert db.seen_count("github_commit") == 6  # 5 items + sentinel

            # cycle 2: three new items, cap is 2 -> 2 drafted, 1 queued
            commits.extend(item(eid=f"new{i}", title=f"n{i}") for i in range(3))
            await watcher.run_cycle(app)
            drafted = [i.external_id for i in context.bot_data["drafter"].drafted]
            assert drafted == ["new0", "new1"]  # oldest first
            assert [r["external_id"] for r in db.pending_items("github_commit")] == ["new2"]
            assert db.is_seen("github_commit", "new0") and not db.is_seen("github_commit", "new2")

            # cycle 3: deferred item drains even if it left the fetch window
            del commits[:]
            commits.extend(item(eid=f"sha{i}") for i in range(5))
            await watcher.run_cycle(app)
            assert [i.external_id for i in context.bot_data["drafter"].drafted][-1] == "new2"
            assert db.pending_items("github_commit") == []
            assert db.is_seen("github_commit", "new2")

            # healing: draft exists but item never marked seen (crash window)
            db.create_draft("github_commit", "crashed", "T", "http://u", ["p"])
            commits.append(item(eid="crashed"))
            before = len(context.bot_data["drafter"].drafted)
            await watcher.run_cycle(app)
            assert len(context.bot_data["drafter"].drafted) == before  # not re-drafted
            assert db.is_seen("github_commit", "crashed")
        finally:
            watcher.SOURCE_FETCHERS = original

    asyncio.run(run())


ALL_TESTS = [
    test_menu_navigation,
    test_auto_toggle_and_check_now,
    test_generate_from_latest_commit,
    test_topic_flow,
    test_approve_paths,
    test_edit_flow,
    test_watcher_cycle,
]

if __name__ == "__main__":
    for test in ALL_TESTS:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(ALL_TESTS)} test(s) passed")
