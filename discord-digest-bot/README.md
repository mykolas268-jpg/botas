# Discord Daily Tech & Business Digest Bot

Posts a news digest to one Discord channel every morning (default **08:00 Europe/Vilnius**).

- **Sources:** TechCrunch, The Verge, Ars Technica, Hacker News (top 15 via hnrss.org), Wired, NewsAPI.org (`technology` + `business`, English), plus any custom RSS feeds you add.
- **Format:** a header embed (date + story count), then one embed per category (**AI & Tech**, **Business & Finance**, **Startups & Products**) with up to 5 stories each: linked headline, source, a summary of about 100 characters.
- **Dedup:** stories whose normalised titles are >80% similar are merged. Posted URLs are stored in SQLite so nothing is reposted on later days.
- **Resilience:** each source has a 10 s timeout. A failing source is logged and skipped, and if nothing is left the bot posts a "no news today" message.

## 1. Create the bot in the Discord Developer Portal

1. Go to <https://discord.com/developers/applications> and click **New Application**.
2. Open **Bot**, click **Reset Token**, then copy the token. This is your `DISCORD_TOKEN`.
   No privileged intents are needed, so leave them all off.
3. Open **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot permissions: `View Channels`, `Send Messages`, `Embed Links`
4. Open the generated URL and invite the bot to your server.
5. In Discord, turn on **User Settings → Advanced → Developer Mode**. Then right-click the target channel and choose **Copy Channel ID**. This is your `DISCORD_CHANNEL_ID`.

## 2. Get a NewsAPI key (optional)

Sign up at <https://newsapi.org/register> and copy the key into `NEWSAPI_KEY`. Without a key the bot still runs on RSS only.

> **Free plan limits:** the Developer plan is licensed for **development/testing only**, articles are delayed by about 24 hours, and you get 100 requests per day. The bot makes 2 requests per digest. For a production server, either pay for a plan or leave the key empty.

## 3. Install and configure

Python 3.10+ is required.

```bash
cd discord-digest-bot
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env             # then edit .env
```

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `DISCORD_TOKEN` | yes | | Bot token |
| `DISCORD_CHANNEL_ID` | yes | | Channel for the scheduled digest |
| `NEWSAPI_KEY` | no | | NewsAPI.org key; empty skips NewsAPI |
| `DIGEST_HOUR` / `DIGEST_MINUTE` | no | `8` / `0` | Daily post time |
| `TIMEZONE` | no | `Europe/Vilnius` | IANA timezone name |
| `MAX_ARTICLE_AGE_HOURS` | no | `36` | Skip older articles |
| `DB_PATH` | no | `./digest.db` | SQLite file |

## 4. Run

```bash
python bot.py
```

On startup the bot logs the next scheduled run and syncs slash commands to the server that owns `DISCORD_CHANNEL_ID`, so they appear right away. Use `/digest` once to check that everything works.

The process must run continuously for the 08:00 post to happen. Run it under systemd, Docker, pm2, or a small VPS. A laptop that sleeps will miss posts. If the bot was down at 08:00 but comes back within an hour, it still posts that day's digest.

## Slash commands

| Command | Who | What it does |
|---|---|---|
| `/digest` | everyone | Builds and posts the digest in the current channel now |
| `/sources` | everyone | Lists built-in, custom and NewsAPI sources (only you see the reply) |
| `/add_feed <name> <url>` | Manage Server | Checks the feed and saves it to SQLite |
| `/remove_feed <name>` | Manage Server | Removes a custom feed (the name autocompletes) |

Server admins can change who may use each command under **Server Settings → Integrations → your bot**.

**Note:** `/digest` marks its stories as posted, just like the scheduled run. If you run it at 07:30, those stories will not appear again at 08:00.

## Project layout

```
bot.py        bot setup, APScheduler job, slash commands, embed rendering
fetcher.py    RSS + NewsAPI fetching, categorisation, dedup, selection
database.py   SQLite: seen article URLs (kept 30 days) + custom feeds
config.py     .env loading, constants, default feeds, category keywords
```

## Tuning

- **Categories** are assigned by keyword scoring (`CATEGORY_KEYWORDS` in `config.py`). Title matches count double. If no keywords match, the source's default category is used. Expect some stories to land in the wrong category, and edit the keyword lists to suit you.
- **Ranking** is newest first, with at most 2 stories per source per category, so one feed can't take over a section.
- **Built-in feeds** are listed in `DEFAULT_RSS_FEEDS` in `config.py`.
