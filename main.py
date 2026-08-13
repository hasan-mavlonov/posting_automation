"""MindForm social pipeline, v1 (X only).

One long-lived process: the Telegram bot's update loop and the source watcher
run concurrently on the same asyncio event loop. Designed to run as a Render
Background Worker.
"""

from __future__ import annotations

import asyncio
import logging

from telegram.error import Conflict
from telegram.ext import Application, ApplicationBuilder, ContextTypes

from config import load_config
from db import Database
from drafter import Drafter
from telegram_bot import register_handlers
from watcher import watcher_loop

log = logging.getLogger(__name__)


async def post_init(application: Application) -> None:
    me = await application.bot.get_me()
    log.info("Telegram bot @%s connected; starting source watcher", me.username)
    application.bot_data["wake_event"] = asyncio.Event()
    application.bot_data["watcher_task"] = asyncio.create_task(watcher_loop(application))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, Conflict):
        log.error(
            "Telegram says another process is polling this bot token. Run only ONE "
            "instance: check for a second terminal, or a deployed copy (e.g. on "
            "Render) running alongside this one."
        )
    else:
        log.error("Unhandled error while processing an update", exc_info=context.error)


async def post_stop(application: Application) -> None:
    task = application.bot_data.get("watcher_task")
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    application.bot_data["db"].close()
    log.info("Shutdown complete")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every getUpdates poll at INFO; keep Render logs readable.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    config = load_config()
    log.info(
        "Starting MindForm social pipeline: repo=%s zenodo_community=%s model=%s db=%s",
        config.github_repo,
        config.zenodo_community,
        config.anthropic_model,
        config.db_path,
    )

    application = (
        ApplicationBuilder()
        .token(config.telegram_bot_token)
        .proxy("http://127.0.0.1:7897")
        .get_updates_proxy("http://127.0.0.1:7897")
        .connect_timeout(30)
        .get_updates_connect_timeout(30)
        .post_init(post_init)
        .post_stop(post_stop)
        .build()
    )
    application.bot_data["config"] = config
    application.bot_data["db"] = Database(config.db_path)
    application.bot_data["drafter"] = Drafter(config)
    application.add_error_handler(on_error)
    register_handlers(application, config.telegram_chat_id)

    # bootstrap_retries=-1: keep retrying (with backoff) if Telegram is
    # unreachable at startup, instead of crashing the worker on a network blip.
    application.run_polling(
        allowed_updates=["message", "callback_query"],
        bootstrap_retries=0,
    )


if __name__ == "__main__":
    main()
