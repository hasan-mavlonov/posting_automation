"""The source-watching loop: poll GitHub and Zenodo, draft posts for new items,
send them to Telegram for review."""

from __future__ import annotations

import asyncio
import logging

from telegram.ext import Application

from sources import SOURCE_FETCHERS
from telegram_bot import send_draft_message

log = logging.getLogger(__name__)


async def watcher_loop(application: Application) -> None:
    config = application.bot_data["config"]
    interval_seconds = config.check_interval_minutes * 60
    log.info(
        "Watcher started: repo=%s zenodo_community=%s every %d min",
        config.github_repo,
        config.zenodo_community,
        config.check_interval_minutes,
    )
    while True:
        try:
            await run_cycle(application)
        except asyncio.CancelledError:
            log.info("Watcher stopped")
            raise
        except Exception:
            log.exception("Source check cycle failed; will retry next cycle")
        await asyncio.sleep(interval_seconds)


async def run_cycle(application: Application) -> None:
    config = application.bot_data["config"]
    db = application.bot_data["db"]
    drafter = application.bot_data["drafter"]

    for source_name, fetcher in SOURCE_FETCHERS:
        try:
            items = await asyncio.to_thread(fetcher, config)
        except Exception as exc:
            log.error("Failed to fetch %s: %s", source_name, exc)
            continue

        new_items = [i for i in items if not db.is_seen(i.source, i.external_id)]
        log.info("Checked %s: %d fetched, %d new", source_name, len(items), len(new_items))
        if not new_items:
            continue

        if db.seen_count(source_name) == 0 and not config.process_backlog_on_first_run:
            for item in items:
                db.mark_seen(item.source, item.external_id)
            log.info(
                "First run for %s: marked %d existing item(s) as seen without drafting "
                "(set PROCESS_BACKLOG_ON_FIRST_RUN=true to draft the backlog)",
                source_name,
                len(items),
            )
            continue

        if len(new_items) > config.max_items_per_cycle:
            log.info(
                "%s has %d new items; drafting the first %d this cycle, the rest next cycle",
                source_name,
                len(new_items),
                config.max_items_per_cycle,
            )
            new_items = new_items[: config.max_items_per_cycle]

        for item in new_items:
            try:
                posts = await drafter.draft_posts(item)
                draft_id = db.create_draft(
                    item.source, item.external_id, item.title, item.url, posts
                )
                await send_draft_message(application.bot, config, db, draft_id)
                db.mark_seen(item.source, item.external_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "Failed to draft %s %s; will retry next cycle",
                    item.source,
                    item.external_id,
                )
