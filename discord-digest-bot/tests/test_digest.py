import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import discord
import pytest
from aiohttp import web

import bot
import config
import fetcher
from database import Database, normalize_url


def rss(items, now=None):
    """items: list of (title, link, description, pubdate-or-None)."""
    now = now or datetime.now(timezone.utc)
    body = ""
    for title, link, desc, pub in items:
        date = "" if pub is False else f"<pubDate>{format_datetime(pub or now)}</pubDate>"
        body += f"<item><title>{title}</title><link>{link}</link>{date}<description>{desc}</description></item>"
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{body}</channel></rss>'.encode()


def art(title, url, source="S", summary="", published=None, default=config.CAT_TECH):
    return fetcher.Article(title, url, source, summary, published or datetime.now(timezone.utc), default)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


# --- text helpers ------------------------------------------------------------
def test_truncate_on_word_boundary():
    out = fetcher.truncate("word " * 50, 100)
    assert len(out) <= 100 and out.endswith("…") and not out.endswith(" …")
    assert fetcher.truncate("short", 100) == "short"


def test_clean_text_strips_html_and_entities():
    assert fetcher.clean_text("<p>A &amp; B</p>\n<b>c</b>") == "A & B c"


def test_normalize_url_drops_tracking():
    assert normalize_url("https://www.X.com/a/?utm_source=hn&id=3#top") == "https://x.com/a?id=3"
    assert normalize_url("https://x.com/a/") == normalize_url("https://x.com/a?fbclid=1")


# --- parsing -----------------------------------------------------------------
def test_parse_feed_hn_summary_and_invalid_entries():
    hn_desc = ("<![CDATA[<p>Article URL: x</p><p>Comments URL: y</p>"
               "<p>Points: 412</p><p># Comments: 98</p>]]>")
    data = rss([
        ("Show HN: thing", "https://e.com/a", hn_desc, None),
        ("No link", "javascript:alert(1)", "d", None),
        ("", "https://e.com/empty", "d", None),
    ])
    arts = fetcher._parse_feed(data, "Hacker News", config.CAT_TECH)
    assert [a.title for a in arts] == ["Show HN: thing"]
    assert arts[0].summary == "412 points · 98 comments on Hacker News"
    assert arts[0].published is not None


def test_parse_feed_rejects_garbage():
    with pytest.raises(ValueError):
        fetcher._parse_feed(b"\x00\x01 not xml <<<", "x", config.CAT_TECH)


# --- dedup / categorise ------------------------------------------------------
def test_dedup_similar_titles_and_records_dropped_url():
    a = art("OpenAI launches GPT-6 today", "https://a.com/1")
    b = art("OpenAI launches GPT-6, today!", "https://b.com/2")
    c = art("Completely different headline", "https://c.com/3")
    kept = fetcher.deduplicate([a, b, c])
    assert kept == [a, c]
    assert a.duplicate_urls == ["https://b.com/2"]


def test_dedup_same_url_different_title():
    kept = fetcher.deduplicate([art("One", "https://a.com/x"), art("Two", "https://a.com/x/?utm_source=z")])
    assert len(kept) == 1


def test_categorize_keywords_and_fallback():
    assert fetcher.categorize(art("Startup Acme raises $40M Series B", "u")) == config.CAT_STARTUPS
    assert fetcher.categorize(art("Stocks fall as inflation rises", "u")) == config.CAT_BUSINESS
    assert fetcher.categorize(art("Nothing matches here", "u", default=config.CAT_BUSINESS)) == config.CAT_BUSINESS
    # "ai" must match as a word, not inside "said"/"rain"
    assert fetcher.categorize(art("He said rain", "u", default=config.CAT_STARTUPS)) == config.CAT_STARTUPS


def test_pick_top_caps_per_source_then_fills():
    arts = [art(f"t{i}", f"u{i}", source="A") for i in range(4)] + [art("b", "ub", source="B")]
    top = fetcher._pick_top(arts, 5, 2)
    assert [a.url for a in top[:3]] == ["u0", "u1", "ub"]
    assert len(top) == 5


# --- build_digest end to end (network mocked) --------------------------------
def test_build_digest_filters_old_and_seen(db, monkeypatch):
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=config.MAX_ARTICLE_AGE_HOURS + 1)
    articles = [
        art("Stocks rally on earnings", "https://x.com/fresh", published=now),
        # older near-duplicate: the newer copy above must win
        art("Stocks rally on earnings beat", "https://y.com/fresh-dup", published=now - timedelta(hours=1)),
        art("Ancient story", "https://x.com/old", published=old),
        art("Already posted", "https://x.com/seen"),
    ]

    async def fake_fetch_all(_db):
        return [fetcher.Article(**{**a.__dict__, "duplicate_urls": []}) for a in articles]

    monkeypatch.setattr(fetcher, "fetch_all", fake_fetch_all)
    db.mark_seen([("https://x.com/seen", "Already posted")])

    digest = asyncio.run(fetcher.build_digest(db))
    urls = [a.url for v in digest.values() for a in v]
    assert urls == ["https://x.com/fresh"]

    picked = digest[config.CAT_BUSINESS][0]
    db.mark_seen([(u, picked.title) for u in (picked.url, *picked.duplicate_urls)])
    assert asyncio.run(fetcher.build_digest(db)) == {}  # duplicate must not resurface tomorrow


# --- real HTTP path against a local server -----------------------------------
@pytest.fixture
def server(monkeypatch):
    """Starts a local aiohttp server; yields its base URL. Routes: /ok, /slow, /500, /newsapi."""
    async def ok(_):
        return web.Response(body=rss([("Local story", "https://l.com/1", "desc", None)]),
                            content_type="application/rss+xml")

    async def slow(_):
        await asyncio.sleep(5)
        return web.Response(text="late")

    async def err(_):
        return web.Response(status=500)

    async def newsapi(request):
        assert request.query["language"] == "en"
        return web.json_response({"status": "ok", "articles": [
            {"title": "Fed holds rates - Reuters", "url": "https://r.com/fed", "source": {"name": "Reuters"},
             "description": "Desc", "publishedAt": "2026-10-09T06:00:00Z"},
            {"title": "[Removed]", "url": "https://removed.com", "source": {"name": "x"}},
        ]})

    loop = asyncio.new_event_loop()
    app = web.Application()
    app.add_routes([web.get("/ok", ok), web.get("/slow", slow), web.get("/500", err), web.get("/newsapi", newsapi)])
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "127.0.0.1", 0)
    loop.run_until_complete(site.start())
    port = site._server.sockets[0].getsockname()[1]
    yield loop, f"http://127.0.0.1:{port}"
    loop.run_until_complete(runner.cleanup())
    loop.close()


def test_fetch_all_skips_failures_and_timeouts(server, db, monkeypatch):
    loop, base = server
    monkeypatch.setattr(config, "FETCH_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(config, "NEWSAPI_KEY", "k")
    monkeypatch.setattr(config, "NEWSAPI_URL", f"{base}/newsapi")
    monkeypatch.setattr(config, "NEWSAPI_CATEGORIES", {"business": config.CAT_BUSINESS})
    monkeypatch.setattr(config, "DEFAULT_RSS_FEEDS", [
        {"name": "ok", "url": f"{base}/ok", "default_category": config.CAT_TECH},
        {"name": "slow", "url": f"{base}/slow", "default_category": config.CAT_TECH},
        {"name": "broken", "url": f"{base}/500", "default_category": config.CAT_TECH},
        {"name": "dns", "url": "http://does-not-exist.invalid/feed", "default_category": config.CAT_TECH},
    ])
    started = loop.time()
    arts = loop.run_until_complete(fetcher.fetch_all(db))
    assert loop.time() - started < 4  # slow feed was cut off by the timeout
    by_url = {a.url: a for a in arts}
    assert set(by_url) == {"https://l.com/1", "https://r.com/fed"}
    assert by_url["https://r.com/fed"].title == "Fed holds rates"
    assert by_url["https://r.com/fed"].source == "Reuters"


def test_validate_feed(server):
    loop, base = server
    assert loop.run_until_complete(fetcher.validate_feed(f"{base}/ok")) == 1
    with pytest.raises(Exception):
        loop.run_until_complete(fetcher.validate_feed(f"{base}/500"))


# --- database ----------------------------------------------------------------
def test_custom_feeds_crud(db):
    assert db.add_feed("MyFeed", "https://m.com/rss", "me")
    assert not db.add_feed("myfeed", "https://other", "me")  # name is case-insensitive
    assert db.list_feeds() == [{"name": "MyFeed", "url": "https://m.com/rss"}]
    assert db.remove_feed("MYFEED")
    assert not db.remove_feed("MyFeed")
    assert db.list_feeds() == []


def test_seen_filter_and_prune(db):
    db.mark_seen([("https://www.a.com/x/?utm_medium=1", "t")])
    assert db.filter_unseen(["https://a.com/x", "https://a.com/y"]) == {"https://a.com/y"}
    assert db.prune_seen(older_than_days=-1) == 1
    assert db.filter_unseen(["https://a.com/x"]) == {"https://a.com/x"}


# --- embeds ------------------------------------------------------------------
def test_build_embeds_and_chunking_respect_discord_limits():
    long_title = "Very long headline about [brackets] and *stars* " * 4
    digest = {
        cat: [art(long_title + str(i), "https://example.com/" + "p" * 250 + f"({i})", summary="s" * 300)
              for i in range(5)]
        for cat in config.CATEGORIES
    }
    embeds = bot.build_embeds(digest)
    assert len(embeds) == 4
    assert "15 stories" in embeds[0].description
    assert all(len(e.description) <= 4096 for e in embeds)
    assert "%28" in embeds[1].description and "[brackets]" not in embeds[1].description

    chunks = bot.chunk_embeds(embeds)
    assert len(chunks) > 1  # this payload exceeds 6000 chars in one message
    assert [e for c in chunks for e in c] == embeds
    assert all(sum(len(e) for e in c) <= 6000 and len(c) <= 10 for c in chunks)


def test_chunk_embeds_small_payload_single_message():
    embeds = [discord.Embed(title="a", description="b") for _ in range(4)]
    assert bot.chunk_embeds(embeds) == [embeds]
