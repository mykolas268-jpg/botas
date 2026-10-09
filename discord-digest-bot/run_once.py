"""Post the digest once and exit - for cron / GitHub Actions instead of the always-on bot.

    python run_once.py          # posts only if it's past DIGEST_HOUR:DIGEST_MINUTE local time
                                # and nothing was posted yet today
    python run_once.py --force  # post right now regardless

Being "idempotent per day" lets the scheduler call this several times around 08:00
(GitHub cron is UTC-only, often late, and blind to daylight saving) with exactly one post.
Exit code is non-zero on failure so the scheduler marks the run as failed.
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime

import discord

import config
import fetcher
from bot import NO_NEWS_MESSAGE, build_embeds, chunk_embeds, seen_entries
from database import Database

log = logging.getLogger("digest.run_once")
LAST_POST_KEY = "last_post_date"


def should_post(now_local: datetime, last_post_date: str | None, force: bool = False) -> tuple[bool, str]:
    today = now_local.date().isoformat()
    if force:
        return True, "forced"
    if last_post_date == today:
        return False, f"already posted today ({today})"
    if (now_local.hour, now_local.minute) < (config.DIGEST_HOUR, config.DIGEST_MINUTE):
        return False, (f"too early: {now_local:%H:%M} < {config.DIGEST_HOUR:02d}:{config.DIGEST_MINUTE:02d} "
                       f"{config.TIMEZONE_NAME}")
    return True, "scheduled"


async def post_digest(db: Database) -> int:
    """Log in over REST only (no gateway), post, mark seen. Returns number of stories posted."""
    client = discord.Client(intents=discord.Intents.default())
    try:
        await client.login(config.DISCORD_TOKEN)
        channel = await client.fetch_channel(config.DISCORD_CHANNEL_ID)
        digest = await fetcher.build_digest(db)
        if not digest:
            await channel.send(NO_NEWS_MESSAGE)
            return 0
        for chunk in chunk_embeds(build_embeds(digest)):
            await channel.send(embeds=chunk)
        db.mark_seen(seen_entries(digest))
        return sum(len(v) for v in digest.values())
    finally:
        await client.close()


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="post now, ignoring time and once-per-day checks")
    args = parser.parse_args(argv)

    config.validate_for_bot()
    db = Database(config.DB_PATH)
    try:
        now = datetime.now(config.TIMEZONE)
        ok, reason = should_post(now, db.get_meta(LAST_POST_KEY), args.force)
        if not ok:
            log.info("Not posting: %s", reason)
            return 0
        log.info("Posting digest (%s)", reason)
        try:
            count = await post_digest(db)
        except discord.LoginFailure:
            log.error("Discord rejected DISCORD_TOKEN - reset it in the Developer Portal and update the secret")
            return 1
        except discord.HTTPException as e:
            log.error("Discord API error (check DISCORD_CHANNEL_ID and bot permissions): %s", e)
            return 1
        db.set_meta(LAST_POST_KEY, now.date().isoformat())
        db.prune_seen(config.SEEN_RETENTION_DAYS)
        log.info("Done: %d stories posted", count)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
