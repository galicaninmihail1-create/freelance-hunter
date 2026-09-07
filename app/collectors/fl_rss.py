from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from re import sub

import feedparser
import httpx

from app.collectors.base import BaseCollector
from app.config import OFFICIAL_FL_RSS_NAMES
from app.models import RawJob

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CollectorIssue:
    stage: str
    error_kind: str
    message: str


@dataclass(frozen=True)
class FeedCollectionStats:
    source: str
    feed_name: str
    feed_url: str
    items_seen: int
    items_emitted: int
    error_kind: str | None = None


def _clean_html(value: str | None) -> str:
    return " ".join(unescape(sub(r"<[^>]+>", " ", value or "")).split())


def _published(entry: object) -> datetime | None:
    raw = getattr(entry, "published", None) or getattr(entry, "updated", None)
    if not raw:
        return None
    try:
        value = parsedate_to_datetime(raw)
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


class FLRSSCollector(BaseCollector):
    """Reads RSS URLs deliberately configured from FL.ru's own subscription UI."""
    def __init__(self, urls: tuple[str, ...], client: httpx.AsyncClient | None = None):
        self.urls, self.client = urls, client
        self.last_errors: list[CollectorIssue] = []
        self.last_feed_stats: list[FeedCollectionStats] = []

    async def collect(self) -> list[RawJob]:
        self.last_errors = []
        self.last_feed_stats = []
        if not self.urls:
            return []
        owned_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=httpx.Timeout(15.0), follow_redirects=True)
        try:
            results = await asyncio.gather(*(self._collect_one(url, client) for url in self.urls), return_exceptions=True)
        finally:
            if owned_client:
                await client.aclose()
        jobs: list[RawJob] = []
        for url, result in zip(self.urls, results, strict=True):
            feed_name = OFFICIAL_FL_RSS_NAMES.get(url, url)
            if isinstance(result, Exception):
                self.last_feed_stats.append(FeedCollectionStats(
                    source="fl.ru", feed_name=feed_name, feed_url=url,
                    items_seen=0, items_emitted=0, error_kind=result.__class__.__name__,
                ))
                self.last_errors.append(CollectorIssue(
                    stage="rss", error_kind=result.__class__.__name__,
                    message=f"{result.__class__.__name__}: RSS polling failed",
                ))
                logger.warning("FL.ru RSS polling failed (%s)", result.__class__.__name__)
            else:
                feed_jobs, items_seen = result
                self.last_feed_stats.append(FeedCollectionStats(
                    source="fl.ru", feed_name=feed_name, feed_url=url,
                    items_seen=items_seen, items_emitted=len(feed_jobs),
                ))
                jobs.extend(feed_jobs)
        return jobs

    async def _collect_one(self, url: str, client: httpx.AsyncClient) -> tuple[list[RawJob], int]:
        response = None
        for attempt in range(3):
            try:
                response = await client.get(url, headers={"User-Agent": "FreelanceHunter/1.0 (+RSS reader)"})
                response.raise_for_status()
                break
            except httpx.HTTPError:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.4 * (attempt + 1))
        assert response is not None
        feed = feedparser.parse(response.content)
        if getattr(feed, "bozo", False):
            self.last_errors.append(CollectorIssue(
                stage="rss_parse", error_kind="MalformedFeed",
                message="Feed parser reported malformed XML",
            ))
            logger.warning("Malformed FL.ru RSS feed: %s", url)
        result: list[RawJob] = []
        for entry in feed.entries:
            try:
                link = getattr(entry, "link", None)
                source_id = getattr(entry, "id", None) or (link.rstrip("/").split("/")[-1] if link else None)
                title = _clean_html(getattr(entry, "title", ""))
                if not title:
                    raise ValueError("entry without title")
                description = _clean_html(getattr(entry, "summary", None) or getattr(entry, "description", None))
                feed_name = OFFICIAL_FL_RSS_NAMES.get(url, url)
                result.append(RawJob(source="fl.ru", source_job_id=str(source_id) if source_id else None, url=link,
                                     title=title, description=description, budget_text=description,
                                     published_at=_published(entry), category=getattr(entry, "category", None),
                                     metadata={"feed_name": feed_name, "feed_url": url}))
            except Exception as exc:
                self.last_errors.append(CollectorIssue(
                    stage="rss_item", error_kind=exc.__class__.__name__,
                    message=f"{exc.__class__.__name__}: malformed RSS entry skipped",
                ))
                logger.warning("Skipping malformed FL.ru RSS entry (%s)", exc.__class__.__name__)
        return result, len(feed.entries)
