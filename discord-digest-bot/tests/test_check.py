"""check.py against a fake Discord REST API (no real token needed)."""
import asyncio
import json

import discord
from aiohttp import web

import check
import config

BOT_ID, CHANNEL_ID, GUILD_ID, ROLE_ID = "111", 222, "333", "444"
VIEW, SEND, EMBED = 1 << 10, 1 << 11, 1 << 14
USER = {"id": BOT_ID, "username": "digest", "discriminator": "0", "global_name": None, "avatar": None, "bot": True}


def jr(payload, status=200):
    # discord.py only decodes bodies whose Content-Type is exactly "application/json"
    return web.Response(body=json.dumps(payload).encode(), status=status,
                        headers={"Content-Type": "application/json"})


def fake_discord(role_perms=VIEW | SEND | EMBED, token_ok=True, channel_exists=True):
    async def me(request):
        if not token_ok:
            return jr({"message": "401: Unauthorized", "code": 0}, status=401)
        return jr(USER)

    async def app_info(_):
        return jr({"id": BOT_ID, "name": "digest", "icon": None, "description": "",
                                  "bot_public": True, "bot_require_code_grant": False,
                                  "verify_key": "k", "owner": USER, "flags": 0})

    async def channel(request):
        if not channel_exists:
            return jr({"message": "Unknown Channel", "code": 10003}, status=404)
        return jr({"id": str(CHANNEL_ID), "type": 0, "guild_id": GUILD_ID, "name": "news",
                                  "position": 0, "permission_overwrites": [], "nsfw": False})

    async def guild(_):
        return jr({
            "id": GUILD_ID, "name": "Test Server", "owner_id": "999", "features": [], "emojis": [],
            "stickers": [], "icon": None, "verification_level": 0, "default_message_notifications": 0,
            "explicit_content_filter": 0, "mfa_level": 0, "premium_tier": 0, "nsfw_level": 0,
            "preferred_locale": "en-US", "afk_timeout": 300, "system_channel_flags": 0,
            "roles": [
                {"id": GUILD_ID, "name": "@everyone", "permissions": "0", "position": 0, "color": 0,
                 "hoist": False, "managed": False, "mentionable": False},
                {"id": ROLE_ID, "name": "Digest", "permissions": str(role_perms), "position": 1, "color": 0,
                 "hoist": False, "managed": True, "mentionable": False},
            ],
        })

    async def member(_):
        return jr({"user": USER, "roles": [ROLE_ID], "joined_at": "2026-01-01T00:00:00+00:00",
                                  "deaf": False, "mute": False, "flags": 0})

    app = web.Application()
    app.add_routes([
        web.get("/api/v10/users/@me", me),
        web.get("/api/v10/oauth2/applications/@me", app_info),
        web.get(f"/api/v10/channels/{CHANNEL_ID}", channel),
        web.get(f"/api/v10/guilds/{GUILD_ID}", guild),
        web.get(f"/api/v10/guilds/{GUILD_ID}/members/{BOT_ID}", member),
    ])
    return app


def run_check(monkeypatch, app):
    async def go():
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setattr(discord.http.Route, "BASE", f"http://127.0.0.1:{port}/api/v10")
        r = check.Report()
        try:
            await check.check_discord(r)
        finally:
            await runner.cleanup()
        return r

    monkeypatch.setattr(config, "DISCORD_TOKEN", "fake-token")
    monkeypatch.setattr(config, "DISCORD_CHANNEL_ID", CHANNEL_ID)
    return asyncio.run(go())


def test_all_good(monkeypatch, capsys):
    r = run_check(monkeypatch, fake_discord())
    out = capsys.readouterr().out
    assert not r.failed, out
    assert "#news in server 'Test Server'" in out
    assert "permissions: view_channel, send_messages, embed_links" in out


def test_missing_embed_links(monkeypatch, capsys):
    r = run_check(monkeypatch, fake_discord(role_perms=VIEW | SEND))
    assert r.failed
    assert "missing channel permissions: embed_links" in capsys.readouterr().out


def test_bad_token(monkeypatch, capsys):
    r = run_check(monkeypatch, fake_discord(token_ok=False))
    assert r.failed
    assert "token rejected" in capsys.readouterr().out


def test_unknown_channel(monkeypatch, capsys):
    r = run_check(monkeypatch, fake_discord(channel_exists=False))
    assert r.failed
    assert "not found - wrong ID?" in capsys.readouterr().out


def test_missing_config_skips_discord(monkeypatch, capsys):
    monkeypatch.setattr(config, "DISCORD_TOKEN", "")
    r = check.Report()
    asyncio.run(check.check_discord(r))
    assert r.failed and "skipped" in capsys.readouterr().out


def test_sources_all_failing_is_fatal(monkeypatch, capsys, tmp_path):
    from database import Database
    monkeypatch.setattr(config, "NEWSAPI_KEY", "")
    monkeypatch.setattr(config, "DEFAULT_RSS_FEEDS", [
        {"name": "dead", "url": "http://does-not-exist.invalid/rss", "default_category": config.CAT_TECH}])
    db = Database(tmp_path / "c.db")
    r = check.Report()
    asyncio.run(check.check_sources(r, db))
    db.close()
    out = capsys.readouterr().out
    assert r.failed and "dead: FAILED" in out and "no source returned anything" in out


def test_discord_unreachable_is_reported_not_crash(monkeypatch, capsys):
    import socket
    with socket.socket() as sock:  # grab a free port, then close it so nothing listens there
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setattr(discord.http.Route, "BASE", f"http://127.0.0.1:{port}/api/v10")
    monkeypatch.setattr(config, "DISCORD_TOKEN", "fake-token")
    monkeypatch.setattr(config, "DISCORD_CHANNEL_ID", CHANNEL_ID)
    r = check.Report()
    asyncio.run(check.check_discord(r))
    assert r.failed and "cannot reach Discord" in capsys.readouterr().out
