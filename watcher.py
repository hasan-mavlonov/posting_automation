"""The source-watching loop: poll GitHub and Zenodo, draft posts for new items,
send them to Telegram for review."""

from __future__ import annotations

import asyncio
import logging

from telegram.ext import Application

from sources import (
    GITHUB_COMMITS,
    GITHUB_RELEASES,
    SOURCE_FETCHERS,
    ZENODO,
    SourceItem,
)
from telegram_bot import send_draft_message

log = logging.getLogger(__name__)

# Sentinel seen-marker recorded on the first cycle per source, so the baseline
# sticks even when the source has zero items at deploy time. Cannot collide
# with real ids (commit SHAs, GitHub release ids, Zenodo record ids).
BASELINE_SENTINEL = "__baseline__"


def _enabled_fetchers(config):
    """Sources the watcher auto-drafts from; the rest stay on-demand via /menu."""
    enabled = []
    for name, fetcher in SOURCE_FETCHERS:
        if name == GITHUB_COMMITS and not config.watch_commits:
            continue
        if name == GITHUB_RELEASES and not config.watch_releases:
            continue
        if name == ZENODO and (not config.watch_zenodo or not config.zenodo_community):
            continue
        enabled.append((name, fetcher))
    return enabled


async def watcher_loop(application: Application) -> None:
    config = application.bot_data["config"]
    db = application.bot_data["db"]
    wake: asyncio.Event = application.bot_data["wake_event"]
    interval_seconds = config.check_interval_minutes * 60
    watched = ", ".join(name for name, _ in _enabled_fetchers(config)) or "nothing"
    log.info(
        "Watcher started: repo=%s zenodo_community=%s auto-drafting from [%s] every %d min",
        config.github_repo,
        config.zenodo_community,
        watched,
        config.check_interval_minutes,
    )
    while True:
        force = application.bot_data.pop("force_cycle", False)
        try:
            if db.get_setting("auto_drafting", "on") == "on" or force:
                await run_cycle(application)
            else:
                log.info("Auto-drafting is paused; skipping source check (resume via /menu)")
        except asyncio.CancelledError:
            log.info("Watcher stopped")
            raise
        except Exception:
            log.exception("Source check cycle failed; will retry next cycle")
        # Sleep until the next scheduled cycle, or earlier if the bot menu's
        # "Check sources now" sets the wake event.
        try:
            await asyncio.wait_for(wake.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass
        wake.clear()


async def run_cycle(application: Application) -> None:
    config = application.bot_data["config"]
    db = application.bot_data["db"]
    drafter = application.bot_data["drafter"]

    for source_name, fetcher in _enabled_fetchers(config):
        try:
            items = await asyncio.to_thread(fetcher, config)
        except Exception as exc:
            log.error("Failed to fetch %s: %s", source_name, exc)
            continue

        # First cycle for this source: baseline the existing history so the
        # backlog isn't posted. Must run even when the source is empty,
        # otherwise the source's first-ever item would be swallowed later.
        if db.seen_count(source_name) == 0 and not config.process_backlog_on_first_run:
            for item in items:
                db.mark_seen(item.source, item.external_id)
            db.mark_seen(source_name, BASELINE_SENTINEL)
            log.info(
                "First run for %s: baselined %d existing item(s) without drafting "
                "(set PROCESS_BACKLOG_ON_FIRST_RUN=true to draft the backlog)",
                source_name,
                len(items),
            )
            continue

        new_items = [i for i in items if not db.is_seen(i.source, i.external_id)]
        log.info("Checked %s: %d fetched, %d new", source_name, len(items), len(new_items))

        # Items deferred by the per-cycle cap in earlier cycles are persisted,
        # so they survive falling out of the fetch window. Process them first
        # (they are the oldest).
        carried: list[SourceItem] = []
        for row in db.pending_items(source_name):
            if db.is_seen(row["source"], row["external_id"]):
                db.remove_pending_item(row["source"], row["external_id"])
                continue
            carried.append(
                SourceItem(
                    source=row["source"],
                    external_id=row["external_id"],
                    title=row["title"],
                    body=row["body"],
                    url=row["url"],
                )
            )
        if carried:
            log.info("%s: %d deferred item(s) carried over", source_name, len(carried))
            carried_ids = {i.external_id for i in carried}
            new_items = carried + [i for i in new_items if i.external_id not in carried_ids]

        if not new_items:
            continue

        if len(new_items) > config.max_items_per_cycle:
            overflow = new_items[config.max_items_per_cycle:]
            for item in overflow:
                db.queue_pending_item(
                    item.source, item.external_id, item.title, item.body, item.url
                )
            log.info(
                "%s has %d new items; drafting the first %d this cycle, %d queued for later",
                source_name,
                len(new_items),
                config.max_items_per_cycle,
                len(overflow),
            )
            new_items = new_items[: config.max_items_per_cycle]

        for item in new_items:
            try:
                # A draft may already exist if a previous run died between
                # sending it and recording the item as seen - heal instead of
                # drafting (and possibly posting) the same item twice.
                existing = db.find_draft(item.source, item.external_id)
                if existing is not None:
                    log.info(
                        "Draft #%d already exists for %s %s; backfilling bookkeeping",
                        existing["id"],
                        item.source,
                        item.external_id,
                    )
                    if existing["telegram_message_id"] is None and existing["status"] == "pending":
                        await send_draft_message(application.bot, config, db, existing["id"])
                else:
                    posts = await drafter.draft_posts(item)
                    draft_id = db.create_draft(
                        item.source, item.external_id, item.title, item.url, posts
                    )
                    await send_draft_message(application.bot, config, db, draft_id)
                db.mark_seen(item.source, item.external_id)
                db.remove_pending_item(item.source, item.external_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "Failed to draft %s %s; will retry next cycle",
                    item.source,
                    item.external_id,
                )
