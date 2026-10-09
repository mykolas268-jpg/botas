"""Configuration: loads .env, exposes constants and the default RSS feed list."""

import logging
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

log = logging.getLogger("digest.config")


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("%s=%r is not an integer, using default %s", name, raw, default)
        return default


# --- Secrets -----------------------------------------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DISCORD_CHANNEL_ID = _int_env("DISCORD_CHANNEL_ID", 0)
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "").strip()

# --- Schedule ----------------------------------------------------------------
DIGEST_HOUR = _int_env("DIGEST_HOUR", 8)
DIGEST_MINUTE = _int_env("DIGEST_MINUTE", 0)
TIMEZONE_NAME = os.getenv("TIMEZONE", "Europe/Vilnius").strip() or "Europe/Vilnius"
try:
    TIMEZONE = ZoneInfo(TIMEZONE_NAME)
except ZoneInfoNotFoundError:
    log.warning("Unknown TIMEZONE %r, falling back to Europe/Vilnius", TIMEZONE_NAME)
    TIMEZONE_NAME = "Europe/Vilnius"
    TIMEZONE = ZoneInfo(TIMEZONE_NAME)

# --- Fetching / digest tuning ------------------------------------------------
FETCH_TIMEOUT_SECONDS = 10
HN_TOP_COUNT = 15
STORIES_PER_CATEGORY = 5
MAX_PER_SOURCE_PER_CATEGORY = 2  # keeps one source from flooding a category
SUMMARY_MAX_CHARS = 100
DEDUP_SIMILARITY_THRESHOLD = 0.80
MAX_ARTICLE_AGE_HOURS = _int_env("MAX_ARTICLE_AGE_HOURS", 36)
SEEN_RETENTION_DAYS = 30
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "digest.db")))
USER_AGENT = "Mozilla/5.0 (compatible; DiscordDigestBot/1.0)"

# --- Categories --------------------------------------------------------------
CAT_TECH = "AI & Tech"
CAT_BUSINESS = "Business & Finance"
CAT_STARTUPS = "Startups & Products"
CATEGORIES = [CAT_TECH, CAT_BUSINESS, CAT_STARTUPS]

CATEGORY_COLORS = {
    CAT_TECH: 0x5865F2,
    CAT_BUSINESS: 0x2ECC71,
    CAT_STARTUPS: 0xE67E22,
}
CATEGORY_EMOJI = {
    CAT_TECH: "🤖",
    CAT_BUSINESS: "💼",
    CAT_STARTUPS: "🚀",
}

# Keyword scoring for categorisation. Matched as whole words / phrases
# against lowercased "title + summary". Title matches count double.
CATEGORY_KEYWORDS = {
    CAT_TECH: [
        "ai", "artificial intelligence", "machine learning", "llm", "openai",
        "anthropic", "claude", "chatgpt", "gemini", "nvidia", "gpu", "chip",
        "chips", "semiconductor", "software", "hardware", "apple", "google",
        "microsoft", "linux", "windows", "android", "ios", "security",
        "vulnerability", "hack", "hackers", "malware", "cyberattack", "robot",
        "robotics", "quantum", "open source", "programming", "developer",
        "browser", "cloud", "data center", "model", "neural", "algorithm",
    ],
    CAT_BUSINESS: [
        "stock", "stocks", "shares", "market", "markets", "earnings",
        "revenue", "profit", "loss", "quarter", "quarterly", "investor",
        "investors", "economy", "economic", "inflation", "interest rate",
        "fed", "ecb", "tariff", "tariffs", "bank", "banking", "ipo",
        "merger", "acquisition", "acquires", "acquire", "deal", "antitrust",
        "lawsuit", "regulator", "regulation", "sec", "ceo", "layoffs",
        "billion", "trillion", "crypto", "bitcoin", "wall street", "dow",
        "nasdaq", "s&p", "price", "prices", "sales",
    ],
    CAT_STARTUPS: [
        "startup", "startups", "founder", "founders", "seed", "series a",
        "series b", "series c", "funding", "raises", "raised", "venture",
        "vc", "valuation", "unicorn", "y combinator", "yc", "launch",
        "launches", "launched", "product", "app", "beta", "show hn",
        "release", "releases", "unveils", "announces", "new feature",
    ],
}

# --- Default feeds -----------------------------------------------------------
# default_category is used when keyword scoring finds nothing.
DEFAULT_RSS_FEEDS = [
    {"name": "TechCrunch", "url": "https://techcrunch.com/feed/", "default_category": CAT_STARTUPS},
    {"name": "The Verge", "url": "https://www.theverge.com/rss/index.xml", "default_category": CAT_TECH},
    {"name": "Ars Technica", "url": "https://feeds.arstechnica.com/arstechnica/index", "default_category": CAT_TECH},
    {"name": "Hacker News", "url": f"https://hnrss.org/frontpage?count={HN_TOP_COUNT}", "default_category": CAT_TECH},
    {"name": "Wired", "url": "https://www.wired.com/feed/rss", "default_category": CAT_TECH},
]

NEWSAPI_URL = "https://newsapi.org/v2/top-headlines"
NEWSAPI_CATEGORIES = {
    "technology": CAT_TECH,
    "business": CAT_BUSINESS,
}


def validate_for_bot() -> None:
    """Exit with a clear message if the settings the bot needs are missing."""
    missing = []
    if not DISCORD_TOKEN:
        missing.append("DISCORD_TOKEN")
    if not DISCORD_CHANNEL_ID:
        missing.append("DISCORD_CHANNEL_ID")
    if missing:
        sys.exit(f"Missing required settings in .env: {', '.join(missing)}")
    if not NEWSAPI_KEY:
        log.warning("NEWSAPI_KEY not set - NewsAPI source will be skipped")
    if not (0 <= DIGEST_HOUR <= 23 and 0 <= DIGEST_MINUTE <= 59):
        sys.exit(f"Invalid DIGEST_HOUR/DIGEST_MINUTE: {DIGEST_HOUR}:{DIGEST_MINUTE}")
