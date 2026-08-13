"""Publish approved posts to X through the Buffer API.

Synchronous (plain requests); the Telegram handler runs it in a worker thread.
"""

from __future__ import annotations

import logging

import requests

from config import Config

log = logging.getLogger(__name__)

BUFFER_CREATE_URL = "https://api.bufferapp.com/1/updates/create.json"


class PublishError(Exception):
    def __init__(self, message: str, published_count: int = 0):
        super().__init__(message)
        self.published_count = published_count


def publish_posts(config: Config, posts: list[str]) -> list[str]:
    """Send each post to Buffer for immediate publishing.

    Thread parts are published as sequential separate posts (the classic
    Buffer API has no native thread support). Returns Buffer update ids.
    """
    proxies = {"http": config.proxy_url, "https": config.proxy_url} if config.proxy_url else None
    update_ids: list[str] = []
    for index, text in enumerate(posts):
        try:
            resp = requests.post(
                BUFFER_CREATE_URL,
                data={
                    "access_token": config.buffer_access_token,
                    "profile_ids[]": config.buffer_profile_id,
                    "text": text,
                    "now": "true",
                },
                proxies=proxies,
                timeout=30,
            )
        except requests.RequestException as exc:
            raise PublishError(
                f"network error talking to Buffer on post {index + 1}/{len(posts)}: {exc}",
                published_count=index,
            ) from exc

        try:
            payload = resp.json()
        except ValueError:
            payload = {}

        if resp.status_code >= 400 or not payload.get("success", False):
            detail = payload.get("message") or resp.text[:300]
            raise PublishError(
                f"Buffer rejected post {index + 1}/{len(posts)} "
                f"(HTTP {resp.status_code}): {detail}",
                published_count=index,
            )

        updates = payload.get("updates") or []
        update_id = updates[0].get("id", "unknown") if updates else "unknown"
        update_ids.append(str(update_id))
        log.info("Published post %d/%d to Buffer (update id %s)", index + 1, len(posts), update_id)

    return update_ids
