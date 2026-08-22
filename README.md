# FC Vino Discord bot

One Python bot for a nine-person server about football and wine.

- 🍷 **Cellar** — log bottles, rate them out of 100, keep tasting notes, see group averages and best-value finds
- ⚽ **Football** — Premier League and Champions League fixtures, results and tables, plus a nudge before kickoff
- 🎯 **Predictions** — exact-score picks across a Premier League matchweek, scored automatically, season leaderboard

Everything is slash commands, and everything lives in one process. Adding a feature means adding a cog, not another bot.

---

## 1. Create the bot on Discord

Only you can do this part — it needs your Discord account and Manage Server on **FC Vino**.

1. Go to <https://discord.com/developers/applications> and click **New Application**. Name it `FC Vino`.
2. Open the **Bot** tab → **Reset Token** → **Copy**. This is the password to your bot. It goes in `.env` in step 6 and never into git.
3. Still on the **Bot** tab:
   - Leave all three **Privileged Gateway Intents** *off*. Slash commands don't need Message Content or Server Members, and leaving them off means one less thing to go wrong.
   - Turn **Public Bot** *off*, so nobody else can invite it anywhere.
4. Open **OAuth2 → URL Generator**:
   - Scopes: **`bot`** and **`applications.commands`** (you need both — `bot` gets it into the server, `applications.commands` lets it register slash commands).
   - Bot permissions: **Send Messages**, **Embed Links**, **Read Message History**, **Add Reactions**.
   - Copy the generated URL at the bottom, open it in a browser, pick **FC Vino**, and authorise.
5. Get your server's ID: in the Discord app, **Settings → Advanced → Developer Mode** on, then right-click the **FC Vino** server name → **Copy Server ID**.
6. Get a free football key at <https://www.football-data.org/client/register>. It arrives by email, usually within a minute.

## 2. Run it

Pick either environment — conda is what this machine uses.

**conda**

```bash
conda create -n fc_vino python=3.13     # skip if the env already exists
conda activate fc_vino
pip install -r requirements.txt
```

Create the env *with* `python=3.13`. `conda create -n fc_vino` on its own makes an
empty env with no interpreter at all, which shows up as a missing `pip`. If you have
already done that, fix it in place with `conda install -n fc_vino python=3.13 pip`.
Using pip inside conda is fine here — every dependency is pure Python.

**venv**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Then, either way:

```bash
cp .env.example .env      # then fill in the three values from step 1
python -m bot
```

You should see:

```
INFO     database ready at .../data/fcvino.sqlite3
INFO     loaded bot.cogs.core
INFO     synced 20 commands to guild 1234567890
INFO     logged in as FC Vino#1234 (id ...)
```

Now type `/ping` in any channel of the server.

Two things worth doing right away:

```
/fcvino-setup kind:predictions      # where matchweek fixtures and the leaderboard go
/fcvino-setup kind:football         # where kickoff reminders go
```

Without those, the bot still answers commands — it just has nowhere to post on its own.

### Before you touch Discord at all

```bash
python -m bot --check
```

Loads the config, opens the database, imports every cog, and tells you which credentials are still missing. No network, no token needed. Run it after any edit.

```bash
python scripts/check_football_api.py          # next 10 days of the prediction competition
python scripts/check_football_api.py CL 30    # any free-tier code, any window
```

Proves the football key works on its own, so if `/football fixtures` misbehaves you know which half to blame.

## 3. When it doesn't work

| What you see | What it means |
|---|---|
| `Improper token has been passed` / "Discord rejected that token" | The token in `.env` is stale or has stray quotes. Reset Token again, paste the raw value. |
| Bot shows online, but no slash commands appear | `DISCORD_GUILD_ID` is wrong, or the invite lacked the `applications.commands` scope. Fix and restart — guild-scoped commands appear within seconds. |
| `I am not a member of guild …` in the log | The invite URL was never opened, or it added the bot to a different server. Redo step 4. |
| Commands appear but the bot can't reply | Missing **Send Messages** or **Embed Links** in that channel. `/fcvino-setup` checks this for you and says which one is missing. |
| `/football` and `/predict` are missing entirely | No `FOOTBALL_DATA_TOKEN` in `.env`. The bot logs a warning and skips those cogs. |
| `pip: command not found` in the conda env | The env was created without an interpreter. `conda install -n fc_vino python=3.13 pip`. |
| `ModuleNotFoundError: No module named 'discord'` | Wrong environment active. `conda activate fc_vino` (or `source .venv/bin/activate`) and check `which python`. |
| "That competition isn't included in the free tier" | Free tier covers PL, ELC, CL, BL1, SA, PD, FL1, DED, PPL, BSA, EC, WC only. |

## Commands

### 🍷 Wine

| Command | What it does |
|---|---|
| `/wine add` | Log a bottle: name, producer, vintage, country, region, grape, price in kr, where you bought it |
| `/wine rate` | Your score out of 100 plus tasting notes. Rate again to change your mind |
| `/wine show` | One bottle: group average, value for money, everyone's score and notes |
| `/wine top` | Highest group average (needs 2+ ratings to qualify) |
| `/wine value` | Best rating points per 100 kr |
| `/wine search` | Match on name, producer, country, region or grape |
| `/wine mine` | Your own tasting book |
| `/wine remove` | Delete a bottle you added (admins can delete any) |

Bottle names autocomplete, so nobody types database ids.

### ✈️ Trips

The away-trip archive: one place a year since 2010, and every match we saw while there.

| Command | What it does |
|---|---|
| `/trips add` | Record a year's trip: country, city, dates, notes. One trip per year, so re-running it amends that year — supplying a city won't wipe the notes |
| `/trips add-match` | A match we saw: `year:2014`, teams, score, competition, ground, crowd, and a `city` if that game was somewhere else |
| `/trips add-goals` | Scorers for one match in a single field: `23 Robben A, 45 Mueller A` — minute, scorer, then H or A |
| `/trips list` | Every trip in order, flagging the ones with more than one match |
| `/trips show` | One year in full: every match, its ground and city, and who scored |
| `/trips stats` | Countries, cities, grounds, goals, results, clubs, streaks — "how many countries have we seen" |
| `/trips countries` | Each country with visit count and years |
| `/trips teams` | Every club seen, repeats flagged, competitions |
| `/trips search` | By team, country, city or ground |
| `/trips random` | One trip at random |
| `/trips missing` | What still needs filling in |
| `/trips remove-match` | Delete one match, keeping the trip and its other matches |
| `/trips remove` | Delete a whole trip and every match on it |

**One place a year, sometimes more than one match.** The year identifies a trip — that's
why the commands take `year:2014` rather than a name to disambiguate. A trip holds as many
matches as you saw, so `/trips add-match year:2014` twice gives you both, and each match
can carry its own `city` for a Ruhr-style trip taking in Dortmund one day and
Gelsenkirchen the next. Leave `city` blank and the match inherits the trip's, so a London
double-header needs no repetition.

**Why this is typed in rather than fetched.** The football-data.org free tier has no
match data before the 2023/24 season, covers only 12 competitions, and returns no venue,
no attendance and an empty goalscorer list even where it does work. Every one of those was
checked against a live key rather than assumed. So the archive stands on its own.

**Filling in the detail.** Enter the trips through the commands above, then the researched
detail — exact dates, competitions, grounds, crowds, scorers — goes into
`seeds/trip_details.json` and is applied with:

```bash
python scripts/apply_trip_details.py --dry-run     # show what would change
python scripts/apply_trip_details.py               # fill empty columns only
python scripts/apply_trip_details.py --create      # also insert missing trips
```

It only fills columns that are empty, so it can never overwrite something you typed
(pass `--overwrite` when you want the file to win), and re-running it changes nothing.
That file is in git on purpose: public football results are reference data, so unlike
`data/fcvino.sqlite3` they belong in the repo — which means the archive survives a lost
database.

### ⚽ Football

| Command | What it does |
|---|---|
| `/football fixtures` | Next 7 days. No competition given → the ones we follow, plus your club |
| `/football results` | Recent scores, same defaults |
| `/football standings` | League table |
| `/football myteam` | Register your club, from an autocompleted list. No argument shows your current pick |
| `/football forget-team` | Stop following it |

**A channel that shows the league table.** A Discord channel can't invoke a slash command — commands only fire when a person types one. So for a dedicated `#premier-league-table` channel, the bot maintains it instead:

```
/fcvino-setup kind:standings channel:#premier-league-table
```

It posts the table once, pins it, then **edits that same message** every 30 minutes, so the channel holds exactly one message that's always current rather than a season's worth of stale tables. It only edits when the table has actually moved — a fingerprint of positions, games played, goal difference and points is compared first — so there's no `(edited)` churn on a quiet Tuesday and no wasted API calls. The message id is stored, so a restart carries on editing the same message; if someone deletes it, the next pass posts a fresh one.

Pinning needs **Manage Messages**, which isn't in the invite permissions above. Without it the table still works, just unpinned — add the permission to the bot's role if you want it pinned.

Once a football channel is set, the bot posts a reminder 60 minutes before kickoff and tags whoever follows one of the teams playing. Change the lead time with `REMINDER_LEAD_MINUTES`, and the competitions with `REMINDER_COMPETITIONS` (set it to just `CL` if a full Premier League slate is too much).

### 🎯 Predictions

| Command | What it does |
|---|---|
| `/predict score` | Exact score for a fixture. **3 pts** spot on, **1 pt** right result |
| `/predict fixtures` | The matchweek with your picks beside each fixture |
| `/predict mine` | What you've submitted and what's still open |
| `/predict table` | Season leaderboard, or one matchweek |

Your picks are private until kickoff: confirmations are ephemeral, `/predict fixtures` only ever shows your own, and everyone's guesses are revealed in the matchweek wrap-up. Each fixture locks at **its own** kickoff, so a Sunday game stays open after Saturday's early match. The bot posts each new matchweek when it comes into view, and a results wrap-up plus updated table once the games are played.

## How it fits together

```
bot/__main__.py       entrypoint; builds the bot, loads cogs, syncs commands to your guild
bot/config.py         .env -> a validated Config, with error messages you can act on
bot/db.py             SQLite schema and helpers; the idempotency guards live here
bot/football_api.py   the only thing that talks to football-data.org: rate limit + cache
bot/mirror.py         keeps the local `matches` table in step with the API
bot/trip_stats.py     pure statistics over the trips archive
bot/formatting.py     embeds, colours, kroner, Discord timestamps
bot/cogs/core.py      /ping, /fcvino-help, /fcvino-setup
bot/cogs/wine.py      the cellar
bot/cogs/trips.py     the away-trip archive
bot/cogs/football.py  fixtures, results, tables, kickoff reminders
bot/cogs/predictions.py  the prediction game and its scoring loop
```

Two decisions worth knowing about:

**The free tier allows 10 requests a minute.** Every call goes through `bot/football_api.py`, which caps itself at 8/minute and caches responses, so nine people asking for the same fixture list is one upstream request. Nothing else in the codebase is allowed to call the API directly.

**Background jobs read the local mirror, not the API.** `matches` is a copy of the fixtures and results we care about. A refresh failing delays the copy; it doesn't break a command or lose a prediction. Reminders and scores are claimed in `reminders_sent` / `prediction_scores` before anything is posted, so restarting the bot mid-matchweek can't double-ping or double-score.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
ruff check bot scripts tests
```

No network and no token: the football API and Discord are both stubbed. Alongside the unit tests for scoring, wine maths and config validation, `tests/test_loops.py` drives the real background-loop bodies through a whole matchweek — fixtures mirrored, reminder posted once, predictions scored, wrap-up published — which is the cheapest way to catch a regression in the parts that only ever run unattended.

## Notes

- **The bot is only online while `python -m bot` is running.** Close the terminal or let the Mac sleep and it goes offline; reminders in that window are missed rather than fired late. When that gets annoying, the same code moves to a Raspberry Pi or a small VPS — the only change is where `.env` lives.
- `.env` and `data/` are gitignored. If a token ever does land in a commit, hit **Reset Token** in the Developer Portal. Rotating the token is the fix; rewriting history is not.
- Command names are English. If you'd rather have `/vin`, `/fotball` and `/tips`, it's a rename in one place per cog.
