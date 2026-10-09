"""News fetching (RSS + NewsAPI), categorisation and de-duplication."""

from __future__ import annotations

import asyncio
import calendar
import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

import aiohttp
import feedparser

import config
from database import Database, normalize_url

log = logging.getLogger("digest.fetcher")

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]+")
_HN_POINTS_RE = re.compile(r"Points:\s*(\d+)")
_HN_COMMENTS_RE = re.compile(r"#\s*Comments:\s*(\d+)")

# Precompiled whole-word patterns per category.
_KEYWORD_PATTERNS = {
    cat: [re.compile(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])") for kw in kws]
    for cat, kws in config.CATEGORY_KEYWORDS.items()
}


@dataclass
class Article:
    title: str
    url: str
    source: str
    summary: str
    published: datetime | None
    default_category: str
    category: str = field(default="")
    # URLs of near-duplicate copies dropped in favour of this one; marked seen with it
    duplicate_urls: list[str] = field(default_factory=list)


# --- Text helpers ------------------------------------------------------------
def clean_text(raw: str | None) -> str:
    if not raw:
        return ""
    text = html.unescape(_TAG_RE.sub(" ", raw))
    return _WS_RE.sub(" ", text).strip()


def truncate(text: str, limit: int = config.SUMMARY_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:-") + "…"


def normalize_title(title: str) -> str:
    return _WS_RE.sub(" ", _NON_ALNUM_RE.sub(" ", title.lower())).strip()


# --- Network -----------------------------------------------------------------
def _session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=config.FETCH_TIMEOUT_SECONDS),
        headers={"User-Agent": config.USER_AGENT},
        trust_env=True,  # honour HTTP(S)_PROXY env vars
    )


async def _get_bytes(session: aiohttp.ClientSession, url: str, **kwargs) -> bytes:
    async with session.get(url, **kwargs) as resp:
        resp.raise_for_status()
        return await resp.read()


def _entry_datetime(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    # feedparser returns UTC struct_time
    return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)


def _hn_summary(raw: str) -> str:
    points = _HN_POINTS_RE.search(raw)
    comments = _HN_COMMENTS_RE.search(raw)
    parts = []
    if points:
        parts.append(f"{points.group(1)} points")
    if comments:
        parts.append(f"{comments.group(1)} comments")
    return " · ".join(parts) + " on Hacker News" if parts else "Trending on Hacker News"


def _parse_feed(data: bytes, name: str, default_category: str) -> list[Article]:
    parsed = feedparser.parse(data)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"not a valid RSS/Atom feed ({parsed.get('bozo_exception')})")
    articles = []
    for entry in parsed.entries:
        title = clean_text(entry.get("title"))
        link = (entry.get("link") or "").strip()
        if not title or not link.startswith(("http://", "https://")):
            continue
        raw_summary = entry.get("summary") or entry.get("description") or ""
        if "Comments URL:" in raw_summary:  # hnrss.org entries
            summary = _hn_summary(raw_summary)
        else:
            summary = clean_text(raw_summary)
        articles.append(
            Article(
                title=title,
                url=link,
                source=name,
                summary=summary,
                published=_entry_datetime(entry),
                default_category=default_category,
            )
        )
    return articles


async def fetch_rss(
    session: aiohttp.ClientSession, name: str, url: str, default_category: str
) -> list[Article]:
    data = await _get_bytes(session, url)
    articles = await asyncio.to_thread(_parse_feed, data, name, default_category)
    log.info("RSS %-14s -> %d items", name, len(articles))
    return articles


async def fetch_newsapi(session: aiohttp.ClientSession, api_category: str) -> list[Article]:
    params = {
        "category": api_category,
        "language": "en",
        "pageSize": 30,
        "apiKey": config.NEWSAPI_KEY,
    }
    async with session.get(config.NEWSAPI_URL, params=params) as resp:
        payload = await resp.json(content_type=None)
        if resp.status != 200 or payload.get("status") != "ok":
            raise RuntimeError(f"HTTP {resp.status}: {payload.get('code')} - {payload.get('message')}")

    default_category = config.NEWSAPI_CATEGORIES[api_category]
    articles = []
    for item in payload.get("articles", []):
        title = clean_text(item.get("title"))
        url = (item.get("url") or "").strip()
        if not title or title == "[Removed]" or not url.startswith(("http://", "https://")):
            continue
        source = ((item.get("source") or {}).get("name") or "NewsAPI").strip()
        # NewsAPI titles usually end with " - Source Name"; drop the suffix.
        suffix = f" - {source}"
        if title.endswith(suffix):
            title = title[: -len(suffix)].strip()
        published = None
        if item.get("publishedAt"):
            try:
                published = datetime.fromisoformat(item["publishedAt"].replace("Z", "+00:00"))
            except ValueError:
                pass
        articles.append(
            Article(
                title=title,
                url=url,
                source=source,
                summary=clean_text(item.get("description")),
                published=published,
                default_category=default_category,
            )
        )
    log.info("NewsAPI %-10s -> %d items", api_category, len(articles))
    return articles


async def validate_feed(url: str) -> int:
    """Fetch a feed once and return its entry count. Raises on failure."""
    async with _session() as session:
        articles = await fetch_rss(session, "validation", url, config.CAT_TECH)
    return len(articles)


# --- Sources -----------------------------------------------------------------
def active_sources(db: Database) -> list[dict]:
    """All sources the next digest will use: {'name', 'url', 'kind', 'default_category'}."""
    sources = [{**f, "kind": "rss"} for f in config.DEFAULT_RSS_FEEDS]
    for f in db.list_feeds():
        sources.append({**f, "kind": "custom", "default_category": config.CAT_TECH})
    if config.NEWSAPI_KEY:
        for api_cat, cat in config.NEWSAPI_CATEGORIES.items():
            sources.append({
                "name": f"NewsAPI ({api_cat})",
                "url": config.NEWSAPI_URL,
                "kind": "newsapi",
                "api_category": api_cat,
                "default_category": cat,
            })
    return sources


async def fetch_all(db: Database) -> list[Article]:
    """Fetch every active source concurrently. Failing sources are logged and skipped."""
    sources = active_sources(db)
    async with _session() as session:
        tasks = []
        for src in sources:
            if src["kind"] == "newsapi":
                tasks.append(fetch_newsapi(session, src["api_category"]))
            else:
                tasks.append(fetch_rss(session, src["name"], src["url"], src["default_category"]))
        results = await asyncio.gather(*tasks, return_exceptions=True)

    articles: list[Article] = []
    for src, result in zip(sources, results):
        if isinstance(result, BaseException):
            reason = "timeout" if isinstance(result, asyncio.TimeoutError) else f"{type(result).__name__}: {result}"
            log.warning("Source %s failed, skipping: %s", src["name"], reason)
            continue
        articles.extend(result)
    return articles


# --- Processing --------------------------------------------------------------
def categorize(article: Article) -> str:
    title = article.title.lower()
    body = article.summary.lower()
    scores = {}
    for cat, patterns in _KEYWORD_PATTERNS.items():
        score = 0
        for p in patterns:
            if p.search(title):
                score += 2
            elif p.search(body):
                score += 1
        scores[cat] = score
    best = max(scores.values())
    if best == 0:
        return article.default_category
    # Ties go to the source's default category if it is among the leaders.
    leaders = [c for c, s in scores.items() if s == best]
    return article.default_category if article.default_category in leaders else leaders[0]


def _sort_key(a: Article):
    ts = a.published.timestamp() if a.published else 0.0
    return (ts, len(a.summary))


def deduplicate(articles: list[Article], threshold: float = config.DEDUP_SIMILARITY_THRESHOLD) -> list[Article]:
    """Drop articles whose normalised title is >threshold similar to one already kept,
    or whose URL is identical. Input order decides which copy wins."""
    kept: list[Article] = []
    kept_norm: list[str] = []
    kept_urls: set[str] = set()
    for art in articles:
        url = normalize_url(art.url)
        if url in kept_urls:
            continue
        norm = normalize_title(art.title)
        original = None
        for kept_art, other in zip(kept, kept_norm):
            sm = SequenceMatcher(None, norm, other)
            # quick_ratio is a cheap upper bound; skip the full ratio when it can't pass
            if sm.quick_ratio() > threshold and sm.ratio() > threshold:
                original = kept_art
                break
        if original is not None:
            original.duplicate_urls.append(art.url)
            continue
        kept.append(art)
        kept_norm.append(norm)
        kept_urls.add(url)
    return kept


def _pick_top(articles: list[Article], n: int, per_source: int) -> list[Article]:
    picked, counts = [], {}
    for a in articles:
        if counts.get(a.source, 0) < per_source:
            picked.append(a)
            counts[a.source] = counts.get(a.source, 0) + 1
            if len(picked) == n:
                return picked
    # Not enough variety - fill the remaining slots ignoring the per-source cap.
    for a in articles:
        if a not in picked:
            picked.append(a)
            if len(picked) == n:
                break
    return picked


async def build_digest(db: Database) -> dict[str, list[Article]]:
    """Fetch, filter, dedupe, categorise and select stories.
    Returns {category: [articles]} with only non-empty categories."""
    articles = await fetch_all(db)
    log.info("Fetched %d raw articles", len(articles))

    cutoff = datetime.now(timezone.utc) - timedelta(hours=config.MAX_ARTICLE_AGE_HOURS)
    articles = [a for a in articles if a.published is None or a.published >= cutoff]

    unseen = db.filter_unseen([a.url for a in articles])
    articles = [a for a in articles if a.url in unseen]

    # Newest (and, on ties, best-described) first, so dedup keeps the freshest copy.
    articles.sort(key=_sort_key, reverse=True)
    articles = deduplicate(articles)
    log.info("%d articles after age/seen filtering and dedup", len(articles))

    by_cat: dict[str, list[Article]] = {c: [] for c in config.CATEGORIES}
    for a in articles:
        a.category = categorize(a)
        by_cat[a.category].append(a)

    digest = {}
    for cat in config.CATEGORIES:
        top = _pick_top(by_cat[cat], config.STORIES_PER_CATEGORY, config.MAX_PER_SOURCE_PER_CATEGORY)
        if top:
            digest[cat] = top
    return digest
