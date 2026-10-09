"""SQLite storage for already-posted article URLs and custom RSS feeds."""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAMS = {"ref", "source", "fbclid", "gclid", "mc_cid", "mc_eid"}


def normalize_url(url: str) -> str:
    """Strip tracking params, fragments and trailing slashes so the same
    article linked from two places is recognised as one."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower().removeprefix("www."), path, urlencode(query), "")
    )


class Database:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS seen_articles (
                url       TEXT PRIMARY KEY,
                title     TEXT,
                posted_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS custom_feeds (
                name     TEXT PRIMARY KEY COLLATE NOCASE,
                url      TEXT NOT NULL,
                added_by TEXT,
                added_at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    # --- seen articles -------------------------------------------------------
    def filter_unseen(self, urls: list[str]) -> set[str]:
        """Return the subset of `urls` that has never been posted."""
        if not urls:
            return set()
        norm = {u: normalize_url(u) for u in urls}
        placeholders = ",".join("?" * len(norm))
        rows = self.conn.execute(
            f"SELECT url FROM seen_articles WHERE url IN ({placeholders})",
            list(set(norm.values())),
        ).fetchall()
        seen = {r["url"] for r in rows}
        return {u for u, n in norm.items() if n not in seen}

    def mark_seen(self, articles: list[tuple[str, str]]) -> None:
        """articles: list of (url, title)."""
        now = datetime.now(timezone.utc).isoformat()
        self.conn.executemany(
            "INSERT OR IGNORE INTO seen_articles (url, title, posted_at) VALUES (?, ?, ?)",
            [(normalize_url(u), t, now) for u, t in articles],
        )
        self.conn.commit()

    def prune_seen(self, older_than_days: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        cur = self.conn.execute("DELETE FROM seen_articles WHERE posted_at < ?", (cutoff,))
        self.conn.commit()
        return cur.rowcount

    # --- custom feeds --------------------------------------------------------
    def add_feed(self, name: str, url: str, added_by: str) -> bool:
        """Returns False if a feed with that name already exists."""
        try:
            self.conn.execute(
                "INSERT INTO custom_feeds (name, url, added_by, added_at) VALUES (?, ?, ?, ?)",
                (name, url, added_by, datetime.now(timezone.utc).isoformat()),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def remove_feed(self, name: str) -> bool:
        cur = self.conn.execute("DELETE FROM custom_feeds WHERE name = ?", (name,))
        self.conn.commit()
        return cur.rowcount > 0

    def list_feeds(self) -> list[dict]:
        rows = self.conn.execute("SELECT name, url FROM custom_feeds ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    # --- small key/value store ---------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
