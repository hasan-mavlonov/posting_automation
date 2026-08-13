"""Fetchers for the two watched sources: GitHub (commits + releases) and Zenodo.

All functions are synchronous (plain requests) and are run in a worker thread
by the watcher. Each returns items oldest-first so drafts arrive in order.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

import requests

from config import Config

USER_AGENT = "mindform-posting-automation (+https://github.com/hasan-mavlonov/posting_automation)"

GITHUB_COMMITS = "github_commit"
GITHUB_RELEASES = "github_release"
ZENODO = "zenodo"

# Body text sent to the drafting model is capped so a giant release note or
# paper description can't blow up the request; titles are capped so the
# Telegram draft message stays under the 4096-char sendable limit.
MAX_BODY_CHARS = 6000
MAX_TITLE_CHARS = 200


def _clip_title(title: str) -> str:
    title = title.strip()
    if len(title) > MAX_TITLE_CHARS:
        return title[: MAX_TITLE_CHARS - 1].rstrip() + "…"
    return title


@dataclass(frozen=True)
class SourceItem:
    source: str
    external_id: str
    title: str
    body: str
    url: str


def _github_headers(config: Config) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
    }
    if config.github_token:
        headers["Authorization"] = f"Bearer {config.github_token}"
    return headers


def fetch_github_commits(config: Config) -> list[SourceItem]:
    resp = requests.get(
        f"https://api.github.com/repos/{config.github_repo}/commits",
        params={"per_page": 30},
        headers=_github_headers(config),
        timeout=30,
    )
    resp.raise_for_status()
    items = []
    for commit in resp.json():
        message = commit.get("commit", {}).get("message", "") or ""
        first_line = message.splitlines()[0].strip() if message.splitlines() else ""
        title = first_line or commit["sha"][:12]
        items.append(
            SourceItem(
                source=GITHUB_COMMITS,
                external_id=commit["sha"],
                title=_clip_title(title),
                body=message[:MAX_BODY_CHARS],
                url=commit["html_url"],
            )
        )
    items.reverse()  # API returns newest first
    return items


def fetch_github_releases(config: Config) -> list[SourceItem]:
    resp = requests.get(
        f"https://api.github.com/repos/{config.github_repo}/releases",
        params={"per_page": 15},
        headers=_github_headers(config),
        timeout=30,
    )
    resp.raise_for_status()
    items = []
    for release in resp.json():
        if release.get("draft"):
            continue
        title = release.get("name") or release.get("tag_name") or "Untitled release"
        items.append(
            SourceItem(
                source=GITHUB_RELEASES,
                external_id=str(release["id"]),
                title=_clip_title(title),
                body=(release.get("body") or "")[:MAX_BODY_CHARS],
                url=release["html_url"],
            )
        )
    items.reverse()
    return items


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_zenodo_records(config: Config) -> list[SourceItem]:
    resp = requests.get(
        "https://zenodo.org/api/records",
        params={
            "communities": config.zenodo_community,
            "size": 20,
            "sort": "mostrecent",
        },
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()
    hits = resp.json().get("hits", {}).get("hits", [])
    items = []
    for record in hits:
        metadata = record.get("metadata", {})
        record_id = str(record["id"])
        url = record.get("links", {}).get("self_html") or f"https://zenodo.org/records/{record_id}"
        items.append(
            SourceItem(
                source=ZENODO,
                external_id=record_id,
                title=_clip_title(metadata.get("title") or "Untitled record"),
                body=_strip_html(metadata.get("description") or "")[:MAX_BODY_CHARS],
                url=url,
            )
        )
    items.reverse()
    return items


SOURCE_FETCHERS = [
    (GITHUB_COMMITS, fetch_github_commits),
    (GITHUB_RELEASES, fetch_github_releases),
    (ZENODO, fetch_zenodo_records),
]

FETCHER_BY_SOURCE = dict(SOURCE_FETCHERS)

SOURCE_LABELS = {
    GITHUB_COMMITS: "GitHub commit",
    GITHUB_RELEASES: "GitHub release",
    ZENODO: "Zenodo record",
    "topic": "Topic post",
}
