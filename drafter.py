"""Draft X posts from source items with the Anthropic API.

Structured JSON output guarantees a parseable {"posts": [...]} response; the
280-character limit is enforced here (with one rewrite round-trip) because the
schema layer can't express length constraints.
"""

from __future__ import annotations

import json
import logging

import httpx
from anthropic import AsyncAnthropic

from config import Config
from sources import SOURCE_LABELS, SourceItem

log = logging.getLogger(__name__)

POST_LIMIT = 280
MAX_THREAD_POSTS = 4

SYSTEM_PROMPT = """\
You write X (Twitter) posts for Hasan Mavlonov, founder of MindForm AI.

About MindForm: a persistent personality layer for AI agents. The thesis: memory alone
isn't enough — agents can remember everything and still feel disconnected, because
continuity comes from an evolving identity, not stored facts. Website: mindform-ai.com

You are given one source item: a GitHub commit, a GitHub release, a research record
from Zenodo, an update on the MindForm website, or a topic the founder typed. Write
a post about it. For website updates, the content shows the new or changed text
plus the full page — post about what's new.

Voice depends on the item type.

For GitHub releases, Zenodo records, website updates, and typed topics — the
founder's voice:
- Short declarative sentences. One thought per line, with a blank line between
  thoughts.
- Open with a hook: the problem or the gap, never "New release:" or "New on Zenodo:".
- Then what shipped or what the work shows, grounded strictly in the material.
- Tie it to the MindForm mission only when the item genuinely is MindForm's own work.
- End with the link.
Example of the voice (a real earlier post):
"AI agents don't have an identity.

They can mimic personality. They can follow instructions. But they don't grow.

That's the gap we've been working on for 5 months.

We're building MindForm — a persistent personality layer for AI agents.

Interested? -> mindform-ai.com"

For GitHub commits — a plain dev update: one or two factual sentences about what
changed and why it matters. No mission framing, no hook.

Hard rules for every post:
- Ground every claim in the provided material. Never invent features, numbers,
  results, or details that are not in the text. If the material is thin, keep the
  post correspondingly modest.
- Each post must be at most 280 characters, counting the URL and line breaks.
- Strongly prefer a single post. Only produce a thread of 2-4 posts if the content
  genuinely needs the room; prefix thread posts with "1/ ", "2/ ", and so on.
- If a URL is provided, include it exactly once, at the end of the first post. If no
  URL is provided, do not invent one.
- No hashtags, no emoji, no hype words.
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
        if config.proxy_url:
            self._client = AsyncAnthropic(
                api_key=config.anthropic_api_key,
                http_client=httpx.AsyncClient(proxy=config.proxy_url, timeout=60.0),
            )
        else:
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
        # X weighs "…" as 2 characters, hence the -2.
        return [p if len(p) <= POST_LIMIT else p[: POST_LIMIT - 2].rstrip() + "…" for p in posts]

    def _build_prompt(self, item: SourceItem) -> str:
        label = SOURCE_LABELS.get(item.source, item.source)
        body = item.body.strip() or "(no further description provided)"
        url_line = item.url if item.url else "(none)"
        return (
            f"Source type: {label}\n"
            f"Title: {item.title}\n"
            f"URL: {url_line}\n\n"
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
        # max_tokens covers thinking + the JSON output: on claude-opus-5
        # adaptive thinking is on by default and shares this budget.
        response = await self._client.messages.create(
            model=self._config.anthropic_model,
            max_tokens=16000,
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
