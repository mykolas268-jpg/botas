"""run_once.py: once-per-day gating and a real post through discord.py to a fake Discord API."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import discord
from aiohttp import web

import config
import fetcher
import run_once
from database import Database
from test_check import CHANNEL_ID, fake_discord, jr

TZ = config.TIMEZONE


def test_should_post_gating(monkeypatch):
    monkeypatch.setattr(config, "DIGEST_HOUR", 8)
    monkeypatch.setattr(config, "DIGEST_MINUTE", 0)
    morning = datetime(2026, 10, 9, 8, 3, tzinfo=TZ)
    assert run_once.should_post(morning, None)[0]
    assert run_once.should_post(morning, "2026-10-08")[0]
    assert not run_once.should_post(morning, "2026-10-09")[0]          # second cron run same day
    assert not run_once.should_post(morning.replace(hour=7, minute=59), None)[0]  # too early
    assert run_once.should_post(morning.replace(hour=7), "2026-10-09", force=True)[0]


def test_cron_hours_cover_vilnius_summer_and_winter():
    # Workflow fires at 05:03, 06:03, 07:03 UTC; at least one must be >= 08:00 Vilnius and
    # the first such run must be before 10:00 local, in both DST regimes.
    for day in (datetime(2026, 7, 1, tzinfo=timezone.utc), datetime(2026, 12, 1, tzinfo=timezone.utc)):
        locals_ = [(day + timedelta(hours=h, minutes=3)).astimezone(TZ) for h in (5, 6, 7)]
        first_ok = next(t for t in locals_ if (t.hour, t.minute) >= (8, 0))
        assert first_ok.hour in (8, 9), (day, locals_)


def test_post_digest_sends_embeds_and_marks_seen(monkeypatch, tmp_path):
    sent = []

    async def messages(request):
        sent.append(await request.json())
        return jr({"id": "1", "channel_id": str(CHANNEL_ID), "type": 0, "content": "", "tts": False,
                   "mention_everyone": False, "mentions": [], "mention_roles": [], "attachments": [],
                   "embeds": [], "pinned": False, "timestamp": "2026-10-09T05:00:00+00:00",
                   "edited_timestamp": None, "author": {"id": "111", "username": "digest",
                                                        "discriminator": "0", "avatar": None}})

    async def fake_build(db):
        now = datetime.now(timezone.utc)
        a = fetcher.Article("Startup raises $10M", "https://x.com/a", "TC", "summary", now, config.CAT_STARTUPS)
        a.duplicate_urls.append("https://y.com/a-copy")
        return {config.CAT_STARTUPS: [a]}

    app = fake_discord()
    app.router.add_post(f"/api/v10/channels/{CHANNEL_ID}/messages", messages)
    monkeypatch.setattr(fetcher, "build_digest", fake_build)
    monkeypatch.setattr(config, "DISCORD_TOKEN", "fake-token")
    monkeypatch.setattr(config, "DISCORD_CHANNEL_ID", CHANNEL_ID)
    db = Database(tmp_path / "r.db")

    async def go():
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setattr(discord.http.Route, "BASE", f"http://127.0.0.1:{port}/api/v10")
        try:
            return await run_once.post_digest(db)
        finally:
            await runner.cleanup()

    assert asyncio.run(go()) == 1
    assert len(sent) == 1
    titles = [e.get("title") for e in sent[0]["embeds"]]
    assert titles[0] == "📰 Daily Tech & Business Digest" and "Startups & Products" in titles[1]
    assert "https://x.com/a" in json.dumps(sent[0])
    assert db.filter_unseen(["https://x.com/a", "https://y.com/a-copy"]) == set()
    db.close()


def test_main_skips_second_run_same_day(monkeypatch, tmp_path):
    db_path = tmp_path / "m.db"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "DISCORD_TOKEN", "fake-token")
    monkeypatch.setattr(config, "DISCORD_CHANNEL_ID", CHANNEL_ID)
    db = Database(db_path)
    db.set_meta(run_once.LAST_POST_KEY, datetime.now(TZ).date().isoformat())
    db.close()

    async def boom(_db):
        raise AssertionError("must not post twice in one day")

    monkeypatch.setattr(run_once, "post_digest", boom)
    assert asyncio.run(run_once.main([])) == 0
