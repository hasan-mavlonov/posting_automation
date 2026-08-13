"""Telegram review flow: draft messages with Approve / Edit / Reject buttons.

Approve publishes through Buffer, Edit collects replacement text via a
force-reply prompt, Reject dismisses the draft.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from telegram import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from drafter import MAX_THREAD_POSTS, POST_LIMIT
from publisher import PublishError, publish_posts
from sources import SOURCE_LABELS

log = logging.getLogger(__name__)

# Lines containing only dashes separate thread parts in edited text
# (anchored per line, so a separator at the start or end also counts).
EDIT_SEPARATOR = re.compile(r"^\s*-{3,}\s*$", re.MULTILINE)


def draft_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve:{draft_id}"),
                InlineKeyboardButton("✏️ Edit", callback_data=f"edit:{draft_id}"),
                InlineKeyboardButton("🚫 Reject", callback_data=f"reject:{draft_id}"),
            ]
        ]
    )


def format_draft(draft, posts: list[str]) -> str:
    label = SOURCE_LABELS.get(draft["source"], draft["source"])
    lines = [
        f"📝 Draft #{draft['id']} · {label}",
        draft["item_title"],
        draft["item_url"],
        "",
    ]
    if len(posts) == 1:
        lines.append(posts[0])
        lines.append(f"\n({len(posts[0])} chars)")
    else:
        for i, post in enumerate(posts, start=1):
            lines.append(f"— post {i}/{len(posts)} ({len(post)} chars) —")
            lines.append(post)
            lines.append("")
    return "\n".join(lines).strip()


async def send_draft_message(bot, config, db, draft_id: int) -> None:
    """Send (or re-send after an edit) a draft for review."""
    draft = db.get_draft(draft_id)
    if draft is None:
        log.error("Draft #%d disappeared before it could be sent", draft_id)
        return
    posts = json.loads(draft["posts_json"])
    message = await bot.send_message(
        chat_id=config.telegram_chat_id,
        text=format_draft(draft, posts),
        reply_markup=draft_keyboard(draft_id),
        disable_web_page_preview=True,
    )
    db.set_draft_message(draft_id, message.message_id)
    log.info("Sent draft #%d to Telegram for review", draft_id)


# --- handlers -------------------------------------------------------------


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unrestricted helper so the operator can discover their chat id."""
    if update.effective_chat is None:
        return
    await update.effective_message.reply_text(f"This chat's id is: {update.effective_chat.id}")


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = context.bot_data["config"]
    db = context.bot_data["db"]

    query = update.callback_query
    try:
        await query.answer()
    except BadRequest as exc:
        # A callback queued while the worker was down is delivered past
        # Telegram's answer deadline; the button action must still run.
        log.info("Could not answer callback query (stale?): %s", exc)
    if update.effective_chat is None or update.effective_chat.id != config.telegram_chat_id:
        log.warning("Ignoring callback from unauthorized chat %s", update.effective_chat)
        return

    action, _, raw_id = query.data.partition(":")
    draft_id = int(raw_id)
    draft = db.get_draft(draft_id)
    if draft is None:
        await query.edit_message_text(f"Draft #{draft_id} no longer exists.")
        return
    if draft["status"] == "publishing":
        await query.edit_message_text(
            format_draft(draft, json.loads(draft["posts_json"]))
            + "\n\n⚠️ A publish was already in flight when the service stopped. Check "
            "Buffer/X to see what went out, then resolve this draft in the database "
            "by hand — re-approving could double-post."
        )
        return
    if draft["status"] in ("published", "rejected", "failed"):
        await query.edit_message_text(
            format_draft(draft, json.loads(draft["posts_json"]))
            + f"\n\nAlready handled (status: {draft['status']})."
        )
        return

    posts = json.loads(draft["posts_json"])

    if action == "approve":
        log.info("Draft #%d approved; publishing %d post(s) to Buffer", draft_id, len(posts))
        # Commit "publishing" before the external call: if the process dies
        # mid-publish the draft fails safe (flagged) instead of staying
        # approvable and getting double-posted.
        db.set_draft_status(draft_id, "publishing")
        try:
            await asyncio.to_thread(publish_posts, config, posts)
        except PublishError as exc:
            log.error("Publishing draft #%d failed: %s", draft_id, exc)
            if exc.published_count > 0:
                db.set_draft_status(draft_id, "failed")
                await query.edit_message_text(
                    format_draft(draft, posts)
                    + f"\n\n⚠️ Publish failed after {exc.published_count}/{len(posts)} "
                    f"post(s) went out: {exc}\nMarked as failed — resolve manually to "
                    "avoid duplicate posts."
                )
            else:
                db.set_draft_status(draft_id, "pending")
                await query.edit_message_text(
                    format_draft(draft, posts) + f"\n\n⚠️ Publish failed: {exc}\nNothing was "
                    "posted — you can retry with Approve.",
                    reply_markup=draft_keyboard(draft_id),
                )
            return
        db.set_draft_status(draft_id, "published")
        log.info("Draft #%d published", draft_id)
        await query.edit_message_text(
            format_draft(draft, posts) + "\n\n✅ Published to X via Buffer."
        )

    elif action == "reject":
        db.set_draft_status(draft_id, "rejected")
        log.info("Draft #%d rejected", draft_id)
        await query.edit_message_text(format_draft(draft, posts) + "\n\n🚫 Rejected.")

    elif action == "edit":
        db.set_draft_status(draft_id, "awaiting_edit")
        prompt = await context.bot.send_message(
            chat_id=config.telegram_chat_id,
            text=(
                f"✏️ Editing draft #{draft_id}. Reply to this message with the new text.\n"
                f"For a thread, separate posts with a line containing only ---\n"
                f"Each post must be at most {POST_LIMIT} characters."
            ),
            reply_markup=ForceReply(selective=True),
        )
        db.add_edit_prompt(config.telegram_chat_id, prompt.message_id, draft_id)
        log.info("Draft #%d awaiting edited text", draft_id)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = context.bot_data["config"]
    db = context.bot_data["db"]
    message = update.effective_message
    if message is None or not message.text:
        return

    draft_id = None
    if message.reply_to_message is not None:
        draft_id = db.pop_edit_prompt(config.telegram_chat_id, message.reply_to_message.message_id)

    if draft_id is None:
        # Deliberately no "assume the only awaiting draft" fallback: a stray
        # note typed into the chat must never silently become post content.
        awaiting = db.drafts_awaiting_edit()
        if awaiting:
            ids = ", ".join(f"#{d['id']}" for d in awaiting)
            await message.reply_text(
                f"Draft(s) awaiting an edit: {ids}. Reply directly to the "
                "corresponding ✏️ prompt message so I know which one you mean "
                "(or press its Edit button again for a fresh prompt)."
            )
        else:
            await message.reply_text(
                "No draft is awaiting an edit. Use the ✏️ Edit button on a draft first."
            )
        return

    async def _reprompt(text: str) -> None:
        prompt = await message.reply_text(text, reply_markup=ForceReply(selective=True))
        db.add_edit_prompt(config.telegram_chat_id, prompt.message_id, draft_id)

    draft = db.get_draft(draft_id)
    if draft is None or draft["status"] != "awaiting_edit":
        await message.reply_text(f"Draft #{draft_id} is not awaiting an edit anymore.")
        return

    posts = [p.strip() for p in EDIT_SEPARATOR.split(message.text) if p.strip()]
    if not posts:
        await _reprompt(
            "That message had no post text — the draft is unchanged. Reply to this "
            "message with the new text."
        )
        return

    if len(posts) > MAX_THREAD_POSTS:
        await _reprompt(
            f"That's {len(posts)} posts; the limit is a thread of {MAX_THREAD_POSTS}. "
            "Reply to this message with fewer parts."
        )
        return

    too_long = [(i + 1, len(p)) for i, p in enumerate(posts) if len(p) > POST_LIMIT]
    if too_long:
        detail = ", ".join(f"post {i} is {n} chars" for i, n in too_long)
        await _reprompt(
            f"Too long for X: {detail} (limit {POST_LIMIT}). Reply to this message "
            "with a shorter version."
        )
        return

    db.update_draft_posts(draft_id, posts, status="pending")
    log.info("Draft #%d updated from Telegram edit; re-sending for review", draft_id)
    await send_draft_message(context.bot, config, db, draft_id)


def register_handlers(application: Application, chat_id: int) -> None:
    application.add_handler(CommandHandler("id", cmd_id))
    application.add_handler(
        CallbackQueryHandler(on_callback, pattern=r"^(approve|edit|reject):\d+$")
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Chat(chat_id), on_text)
    )
