"""Draft X posts from source items with the Anthropic API.

Structured JSON output guarantees a parseable {"posts": [...]} response; the
280-character limit is enforced here (with one rewrite round-trip) because the
schema layer can't express length constraints.
"""

from __future__ import annotations

import json
import logging

from anthropic import AsyncAnthropic

from config import Config
from sources import SOURCE_LABELS, SourceItem

log = logging.getLogger(__name__)

POST_LIMIT = 280
MAX_THREAD_POSTS = 4

SYSTEM_PROMPT = """\
You write posts for the X (Twitter) account of MindForm AI, a small AI research lab.

You are given one source item: a GitHub commit, a GitHub release, or a research record
from Zenodo. Write a post announcing it.

Rules:
- Ground every claim in the provided material. Never invent features, numbers, results,
  or details that are not in the text. If the material is thin (a one-line commit
  message), keep the post correspondingly modest.
- Each post must be at most 280 characters, counting the URL.
- Strongly prefer a single post. Only produce a thread of 2-4 posts if the content
  genuinely needs the room.
- In a thread, prefix each post with its position: "1/ ", "2/ ", and so on.
- Include the item's URL exactly once, at the end of the first post.
- Plain, direct, technical tone. No hype words, no emoji, at most one hashtag and only
  when it clearly helps discovery.
"""

POSTS_SCHEMA = {
    "type": "object",
    "properties": {
        "posts": {
            "type": "array",
            "items": {"type": "string"},
        }
    },
    "required": ["posts"],
    "additionalProperties": False,
}


class DraftingError(Exception):
    pass


class Drafter:
    def __init__(self, config: Config):
        self._config = config
        self._client = AsyncAnthropic(api_key=config.anthropic_api_key)

    async def draft_posts(self, item: SourceItem) -> list[str]:
        """Return 1-4 posts, each within the character limit."""
        messages = [{"role": "user", "content": self._build_prompt(item)}]
        posts: list[str] = []
        for attempt in range(2):
            raw = await self._request(messages)
            posts = self._parse(raw)
            too_long = [(i + 1, len(p)) for i, p in enumerate(posts) if len(p) > POST_LIMIT]
            if not too_long:
                return posts
            if attempt == 0:
                detail = ", ".join(f"post {i} is {n} chars" for i, n in too_long)
                messages.append({"role": "assistant", "content": raw})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"Too long: {detail}. Rewrite so every post is at most "
                            f"{POST_LIMIT} characters, keeping the same facts and the URL. "
                            "Return the same JSON shape."
                        ),
                    }
                )
        log.warning(
            "Draft for %s %s still over %d chars after retry; truncating",
            item.source,
            item.external_id,
            POST_LIMIT,
        )
        return [p if len(p) <= POST_LIMIT else p[: POST_LIMIT - 1].rstrip() + "…" for p in posts]

    def _build_prompt(self, item: SourceItem) -> str:
        label = SOURCE_LABELS.get(item.source, item.source)
        body = item.body.strip() or "(no further description provided)"
        return (
            f"Source type: {label}\n"
            f"Title: {item.title}\n"
            f"URL: {item.url}\n\n"
            f"Content:\n{body}"
        )

    async def _request(self, messages: list[dict]) -> str:
        extra_kwargs = {}
        if self._config.anthropic_model.startswith(("claude-opus-5", "claude-fable-5")):
            # Server-side refusal fallback: if the safety classifiers decline a
            # request, the API retries it on Anthropic's recommended fallback
            # model within the same call.
            extra_kwargs["extra_headers"] = {"anthropic-beta": "server-side-fallback-2026-07-01"}
            extra_kwargs["extra_body"] = {"fallbacks": "default"}
        response = await self._client.messages.create(
            model=self._config.anthropic_model,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            output_config={"format": {"type": "json_schema", "schema": POSTS_SCHEMA}},
            messages=messages,
            **extra_kwargs,
        )
        if response.stop_reason == "refusal":
            raise DraftingError("the model declined to draft this item (stop_reason=refusal)")
        if response.stop_reason == "max_tokens":
            raise DraftingError("draft response was truncated (stop_reason=max_tokens)")
        text = "".join(block.text for block in response.content if block.type == "text")
        if not text.strip():
            raise DraftingError("model returned an empty response")
        return text

    def _parse(self, raw: str) -> list[str]:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DraftingError(f"model returned invalid JSON: {exc}") from exc
        posts = data.get("posts")
        if not isinstance(posts, list):
            raise DraftingError("model response is missing the 'posts' array")
        cleaned = [p.strip() for p in posts if isinstance(p, str) and p.strip()]
        if not cleaned:
            raise DraftingError("model returned no usable posts")
        if len(cleaned) > MAX_THREAD_POSTS:
            log.warning("Model returned %d posts; keeping the first %d", len(cleaned), MAX_THREAD_POSTS)
            cleaned = cleaned[:MAX_THREAD_POSTS]
        return cleaned
