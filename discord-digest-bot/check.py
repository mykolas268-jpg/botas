"""Preflight check: verifies config, Discord access, NewsAPI and every feed without posting anything.

    python check.py            # check everything, exit code 1 if something required is broken
    python check.py --preview  # also print the digest that would be posted right now
"""

import argparse
import asyncio
import logging
import sys

import aiohttp
import discord

import config
import fetcher
from database import Database

OK, WARN, FAIL = "✅", "⚠️ ", "❌"
REQUIRED_PERMS = ("view_channel", "send_messages", "embed_links")


class Report:
    def __init__(self):
        self.failed = False

    def line(self, status: str, msg: str) -> None:
        if status == FAIL:
            self.failed = True
        print(f"  {status} {msg}")


def check_config(r: Report) -> None:
    print("Config")
    r.line(OK if config.DISCORD_TOKEN else FAIL, "DISCORD_TOKEN " + ("set" if config.DISCORD_TOKEN else "missing"))
    r.line(OK if config.DISCORD_CHANNEL_ID else FAIL,
           f"DISCORD_CHANNEL_ID {config.DISCORD_CHANNEL_ID or 'missing'}")
    r.line(OK if config.NEWSAPI_KEY else WARN,
           "NEWSAPI_KEY " + ("set" if config.NEWSAPI_KEY else "not set - NewsAPI will be skipped"))
    valid_time = 0 <= config.DIGEST_HOUR <= 23 and 0 <= config.DIGEST_MINUTE <= 59
    r.line(OK if valid_time else FAIL,
           f"Schedule {config.DIGEST_HOUR:02d}:{config.DIGEST_MINUTE:02d} {config.TIMEZONE_NAME}")


async def check_discord(r: Report) -> None:
    print("Discord")
    if not (config.DISCORD_TOKEN and config.DISCORD_CHANNEL_ID):
        r.line(FAIL, "skipped - token or channel ID missing")
        return
    client = discord.Client(intents=discord.Intents.default())
    try:
        try:
            await client.login(config.DISCORD_TOKEN)  # HTTP only, no gateway connection
        except discord.LoginFailure:
            r.line(FAIL, "token rejected by Discord - reset it in the Developer Portal (Bot tab)")
            return
        r.line(OK, f"logged in as {client.user} (id {client.user.id})")

        try:
            channel = await client.fetch_channel(config.DISCORD_CHANNEL_ID)
        except discord.NotFound:
            r.line(FAIL, f"channel {config.DISCORD_CHANNEL_ID} not found - wrong ID?")
            return
        except discord.Forbidden:
            r.line(FAIL, f"no access to channel {config.DISCORD_CHANNEL_ID} - is the bot in that server "
                         "and allowed to view the channel?")
            return

        guild_id = getattr(channel, "guild", None) and channel.guild.id
        if not guild_id:
            r.line(FAIL, f"channel {config.DISCORD_CHANNEL_ID} is not a server text channel")
            return
        guild = await client.fetch_guild(guild_id)
        channel = await guild.fetch_channel(config.DISCORD_CHANNEL_ID)  # bound to a guild with roles loaded
        member = await guild.fetch_member(client.user.id)
        r.line(OK, f"channel #{channel.name} in server '{guild.name}'")

        perms = channel.permissions_for(member)
        missing = [p for p in REQUIRED_PERMS if not getattr(perms, p)]
        if missing:
            r.line(FAIL, "missing channel permissions: " + ", ".join(missing))
        else:
            r.line(OK, "permissions: " + ", ".join(REQUIRED_PERMS))
    except discord.HTTPException as e:
        r.line(FAIL, f"Discord API error: {e}")
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
        r.line(FAIL, f"cannot reach Discord ({type(e).__name__}: {e}) - check internet/DNS/firewall")
    finally:
        await client.close()


async def check_sources(r: Report, db: Database) -> None:
    print("News sources")
    sources = fetcher.active_sources(db)
    async with fetcher._session() as session:
        async def probe(src):
            if src["kind"] == "newsapi":
                return await fetcher.fetch_newsapi(session, src["api_category"])
            return await fetcher.fetch_rss(session, src["name"], src["url"], src["default_category"])

        results = await asyncio.gather(*(probe(s) for s in sources), return_exceptions=True)

    working = 0
    for src, res in zip(sources, results):
        if isinstance(res, BaseException):
            reason = "timed out" if isinstance(res, asyncio.TimeoutError) else f"{type(res).__name__}: {res}"
            r.line(WARN, f"{src['name']}: FAILED ({reason})")
        elif not res:
            r.line(WARN, f"{src['name']}: reachable but 0 usable items")
        else:
            working += 1
            dated = sum(1 for a in res if a.published)
            r.line(OK, f"{src['name']}: {len(res)} items ({dated} dated)")
    # Individual feed failures are tolerated at runtime; all of them failing is not.
    if working == 0:
        r.line(FAIL, "no source returned anything - check network/firewall")


async def preview(db: Database) -> None:
    print("\nDigest preview (nothing is posted or marked as seen)")
    digest = await fetcher.build_digest(db)
    if not digest:
        print("  (no stories - the bot would post the 'no news today' message)")
        return
    for cat, arts in digest.items():
        print(f"\n  {config.CATEGORY_EMOJI[cat]} {cat}")
        for i, a in enumerate(arts, 1):
            print(f"   {i}. {a.title}\n      {a.source} — {fetcher.truncate(a.summary)}\n      {a.url}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--preview", action="store_true", help="print the digest that would be posted now")
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)  # keep fetcher INFO/WARNING logs out of the report

    r = Report()
    db = Database(config.DB_PATH)
    try:
        check_config(r)
        await check_discord(r)
        await check_sources(r, db)
        if args.preview:
            await preview(db)
    finally:
        db.close()
    print("\n" + ("❌ Not ready - fix the items marked ❌ above." if r.failed else "✅ Ready to run: python bot.py"))
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
