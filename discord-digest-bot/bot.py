"""Discord daily tech/business news digest bot: setup, scheduler, slash commands."""

import asyncio
import logging
from datetime import datetime
from urllib.parse import urlsplit

import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from discord import app_commands

import config
import fetcher
from database import Database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("digest.bot")

EMBED_DESC_LIMIT = 4096
MAX_EMBEDS_PER_MESSAGE = 10


# --- Embed building ----------------------------------------------------------
def _link_safe_title(title: str) -> str:
    # Square brackets would break the [text](url) markdown link.
    title = title.replace("[", "(").replace("]", ")")
    return discord.utils.escape_markdown(title)


def _link_safe_url(url: str) -> str:
    return url.replace("(", "%28").replace(")", "%29").replace(" ", "%20")


def build_embeds(digest: dict[str, list[fetcher.Article]]) -> list[discord.Embed]:
    total = sum(len(v) for v in digest.values())
    now = datetime.now(config.TIMEZONE)
    header = discord.Embed(
        title="📰 Daily Tech & Business Digest",
        description=f"**{now.strftime('%A, %d %B %Y')}**\n{total} stories across {len(digest)} categories",
        color=0x2B2D31,
    )
    embeds = [header]

    for category, articles in digest.items():
        blocks = []
        for i, a in enumerate(articles, 1):
            summary = fetcher.truncate(a.summary) if a.summary else ""
            line = f"**{i}. [{_link_safe_title(a.title)}]({_link_safe_url(a.url)})**\n*{a.source}*"
            if summary:
                line += f" — {discord.utils.escape_markdown(summary)}"
            blocks.append(line)
        description = "\n\n".join(blocks)
        if len(description) > EMBED_DESC_LIMIT:  # extremely long URLs; very unlikely
            description = description[: EMBED_DESC_LIMIT - 1] + "…"
        embeds.append(
            discord.Embed(
                title=f"{config.CATEGORY_EMOJI[category]} {category}",
                description=description,
                color=config.CATEGORY_COLORS[category],
            )
        )
    embeds[-1].set_footer(text="Sources: TechCrunch, The Verge, Ars Technica, Hacker News, Wired, NewsAPI + custom feeds")
    return embeds


NO_NEWS_MESSAGE = "📭 No new tech/business stories today — every source was empty, failed, or already covered."


# --- Bot ---------------------------------------------------------------------
class DigestBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.db = Database(config.DB_PATH)
        self.scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)
        self.digest_lock = asyncio.Lock()
        self._synced = False

    async def setup_hook(self) -> None:
        self.scheduler.add_job(
            self.scheduled_digest,
            CronTrigger(hour=config.DIGEST_HOUR, minute=config.DIGEST_MINUTE, timezone=config.TIMEZONE),
            id="daily_digest",
            misfire_grace_time=3600,  # still post if the bot was briefly down at 08:00
            coalesce=True,
            max_instances=1,
            replace_existing=True,
        )
        self.scheduler.start()
        job = self.scheduler.get_job("daily_digest")
        log.info("Digest scheduled daily at %02d:%02d %s (next run: %s)",
                 config.DIGEST_HOUR, config.DIGEST_MINUTE, config.TIMEZONE_NAME, job.next_run_time)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)
        if self._synced:
            return
        self._synced = True
        channel = await self._get_digest_channel()
        if channel is not None and getattr(channel, "guild", None):
            # Guild sync is instant; global sync can take up to an hour to show up.
            self.tree.copy_global_to(guild=channel.guild)
            cmds = await self.tree.sync(guild=channel.guild)
            log.info("Synced %d slash commands to guild %s", len(cmds), channel.guild.name)
        else:
            cmds = await self.tree.sync()
            log.info("Synced %d slash commands globally (may take up to 1h to appear)", len(cmds))

    async def close(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        self.db.close()
        await super().close()

    async def _get_digest_channel(self) -> discord.abc.Messageable | None:
        channel = self.get_channel(config.DISCORD_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.fetch_channel(config.DISCORD_CHANNEL_ID)
            except discord.DiscordException as e:
                log.error("Cannot access DISCORD_CHANNEL_ID=%s: %r", config.DISCORD_CHANNEL_ID, e)
                return None
        return channel

    async def make_digest(self) -> tuple[list[discord.Embed] | None, list[tuple[str, str]]]:
        """Returns (embeds or None if no news, [(url, title)] to mark as seen after posting)."""
        digest = await fetcher.build_digest(self.db)
        if not digest:
            return None, []
        seen = [
            (url, a.title)
            for arts in digest.values()
            for a in arts
            for url in (a.url, *a.duplicate_urls)
        ]
        return build_embeds(digest), seen

    async def scheduled_digest(self) -> None:
        try:
            async with self.digest_lock:
                channel = await self._get_digest_channel()
                if channel is None:
                    return
                embeds, seen = await self.make_digest()
                if embeds is None:
                    await channel.send(NO_NEWS_MESSAGE)
                else:
                    await channel.send(embeds=embeds[:MAX_EMBEDS_PER_MESSAGE])
                    self.db.mark_seen(seen)
                pruned = self.db.prune_seen(config.SEEN_RETENTION_DAYS)
                log.info("Scheduled digest posted (%d URLs marked seen, %d old URLs pruned)", len(seen), pruned)
        except Exception:
            log.exception("Scheduled digest failed")


bot = DigestBot()


# --- Slash commands ----------------------------------------------------------
@bot.tree.command(name="digest", description="Post the news digest right now in this channel")
async def digest_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    async with bot.digest_lock:
        embeds, seen = await bot.make_digest()
        if embeds is None:
            await interaction.followup.send(NO_NEWS_MESSAGE)
            return
        await interaction.followup.send(embeds=embeds[:MAX_EMBEDS_PER_MESSAGE])
        bot.db.mark_seen(seen)


@bot.tree.command(name="sources", description="List all active news sources")
async def sources_cmd(interaction: discord.Interaction):
    sources = fetcher.active_sources(bot.db)
    lines = {"rss": [], "custom": [], "newsapi": []}
    for s in sources:
        lines[s["kind"]].append(f"• **{discord.utils.escape_markdown(s['name'])}** — <{s['url']}>"
                                if s["kind"] != "newsapi" else f"• **{s['name']}**")
    embed = discord.Embed(title="📡 Active news sources", color=0x5865F2)
    embed.add_field(name="Built-in RSS", value="\n".join(lines["rss"]), inline=False)
    embed.add_field(name="Custom RSS", value="\n".join(lines["custom"])[:1024] or "*none — add with /add_feed*", inline=False)
    embed.add_field(
        name="NewsAPI",
        value="\n".join(lines["newsapi"]) or "*disabled — NEWSAPI_KEY not set*",
        inline=False,
    )
    embed.set_footer(text=f"Daily at {config.DIGEST_HOUR:02d}:{config.DIGEST_MINUTE:02d} {config.TIMEZONE_NAME}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="add_feed", description="Add a custom RSS feed to the digest")
@app_commands.describe(name="Short display name for the feed", url="RSS/Atom feed URL (http/https)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def add_feed_cmd(interaction: discord.Interaction, name: app_commands.Range[str, 1, 50], url: str):
    name, url = name.strip(), url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        await interaction.response.send_message("❌ URL must start with http:// or https://", ephemeral=True)
        return
    if any(f["name"].lower() == name.lower() for f in config.DEFAULT_RSS_FEEDS):
        await interaction.response.send_message(f"❌ **{name}** is a built-in feed name.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        count = await fetcher.validate_feed(url)
    except Exception as e:
        reason = "timed out" if isinstance(e, asyncio.TimeoutError) else str(e) or type(e).__name__
        await interaction.followup.send(f"❌ Could not read that feed: {reason}")
        return
    if count == 0:
        await interaction.followup.send("❌ The feed parsed but contains no usable entries.")
        return
    if not bot.db.add_feed(name, url, str(interaction.user)):
        await interaction.followup.send(f"❌ A custom feed named **{name}** already exists.")
        return
    log.info("%s added feed %s -> %s", interaction.user, name, url)
    await interaction.followup.send(f"✅ Added **{name}** ({count} entries found). It will be included in the next digest.")


@bot.tree.command(name="remove_feed", description="Remove a custom RSS feed")
@app_commands.describe(name="Name of the custom feed to remove")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def remove_feed_cmd(interaction: discord.Interaction, name: str):
    if bot.db.remove_feed(name.strip()):
        log.info("%s removed feed %s", interaction.user, name)
        await interaction.response.send_message(f"🗑️ Removed **{name}**.", ephemeral=True)
    elif any(f["name"].lower() == name.strip().lower() for f in config.DEFAULT_RSS_FEEDS):
        await interaction.response.send_message("❌ Built-in feeds can't be removed (edit `config.py` instead).", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ No custom feed named **{name}**.", ephemeral=True)


@remove_feed_cmd.autocomplete("name")
async def remove_feed_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=f["name"], value=f["name"])
        for f in bot.db.list_feeds()
        if current.lower() in f["name"].lower()
    ][:25]


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.exception("Slash command error in /%s", interaction.command.name if interaction.command else "?",
                  exc_info=error)
    msg = "⚠️ Something went wrong while running that command. Check the bot logs."
    if isinstance(error, app_commands.MissingPermissions):
        msg = "❌ You need the Manage Server permission for this."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.DiscordException:
        pass


def main() -> None:
    config.validate_for_bot()
    bot.run(config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
