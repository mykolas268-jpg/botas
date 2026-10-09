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

## 4. Check, then run

```bash
python check.py            # verifies token, channel, permissions, NewsAPI key and every feed
python check.py --preview  # same, plus prints the digest it would post right now (nothing is posted)
python bot.py
```

`check.py` exits with code 1 and a ❌ line for anything that would stop the bot from working, such as a rejected token, a wrong channel ID, a missing *Send Messages* or *Embed Links* permission, or no reachable sources. A single failing feed is only a ⚠️ warning, because the bot skips it at runtime.

On startup the bot logs the next scheduled run and syncs slash commands to the server that owns `DISCORD_CHANNEL_ID`, so they appear right away.

The process must run continuously for the 08:00 post to happen. A laptop that sleeps will miss posts. If the bot was down at 08:00 but comes back within an hour, it still posts that day's digest.

### Free hosting with GitHub Actions (no server, no slash commands)

`.github/workflows/daily-digest.yml` runs `run_once.py` on GitHub's machines every morning. It posts the digest and exits.

1. In the repository on GitHub, go to **Settings → Secrets and variables → Actions → New repository secret** and add:
   - `DISCORD_TOKEN`: the bot token
   - `DISCORD_CHANNEL_ID`: the channel ID
   - `NEWSAPI_KEY` (optional)
2. To test, go to **Actions → daily digest → Run workflow**. It posts immediately.

How it behaves:
- **Timing:** GitHub cron runs in UTC and often starts late, so the workflow fires at 05:03, 06:03 and 07:03 UTC. It posts on the first run at or after 08:00 Vilnius time (summer or winter) and skips the rest. Expect the post between about 08:03 and 08:30. If you change `DIGEST_HOUR` or `TIMEZONE`, edit the cron hours too.
- **Memory between days:** posted URLs are kept in the Actions cache. If GitHub evicts the cache, the worst case is that the day's stories get reposted once.
- **Slash commands don't work** in this mode, because nothing is running between posts. Add extra feeds in `DEFAULT_RSS_FEEDS` in `config.py`.
- **Inactivity:** GitHub turns off scheduled workflows in public repos after 60 days with no commits. It emails you first, and **Actions → daily digest → Enable workflow** turns it back on.
- **Failures:** a failed run shows red in the Actions tab, and GitHub emails you.

### Deploy on a Linux server (recommended)

On a fresh Ubuntu/Debian VPS (the cheapest plan is plenty: the bot uses roughly 100 MB of RAM):

```bash
git clone <this repo> && cd <repo>/discord-digest-bot
./deploy.sh      # 1st run: installs Docker if needed, creates .env, then stops
nano .env        # fill in DISCORD_TOKEN, DISCORD_CHANNEL_ID, NEWSAPI_KEY
./deploy.sh      # builds, runs check.py, and starts the bot only if the check passes
```

To update later, run `git pull && ./deploy.sh`. To follow the logs, run `docker compose logs -f`.

The SQLite database lives on the `digest-data` Docker volume, so rebuilds keep your seen articles and custom feeds. The container restarts automatically after a crash or reboot.

If the repository is private, the server needs access to clone it (a GitHub deploy key or a personal access token).

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests run offline: feeds, NewsAPI and the Discord REST API are simulated by local test servers. They cover parsing, dedup, categorisation, timeouts and failing sources, the SQLite store, Discord embed size limits, and every `check.py` failure mode. GitHub Actions runs them on Python 3.10 and 3.12 for every push that touches this folder.

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
check.py      preflight check + digest preview (posts nothing)
run_once.py   post once and exit (used by the GitHub Actions schedule)
deploy.sh     Docker install/build/check/start for a Linux server
```

## Tuning

- **Categories** are assigned by keyword scoring (`CATEGORY_KEYWORDS` in `config.py`). Title matches count double. If no keywords match, the source's default category is used. Expect some stories to land in the wrong category, and edit the keyword lists to suit you.
- **Ranking** is newest first, with at most 2 stories per source per category, so one feed can't take over a section.
- **Built-in feeds** are listed in `DEFAULT_RSS_FEEDS` in `config.py`.
