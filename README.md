# Personal Music Bot

A private Discord music bot for one server: hop in a voice channel with friends and play songs, podcasts, or radio streams. No premium tiers, no database, no web dashboard.

## Commands

Everything is open to everyone in the server. Commands that change playback need you to be in the bot's voice channel.

| | |
|---|---|
| **Playing** | `/play` add to the end of the queue · `/playnext` add right after the current song · `/pause` (again to resume) · `/resume` · `/skip` · `/back` previous song · `/seek` jump to a time · `/nowplaying` bring the player card back down · `/stop` stop and leave |
| **Queue** | `/queue` (with page buttons) · `/remove` · `/move` · `/skipto` · `/shuffle` · `/clear` |
| **Modes** | `/loop` off / current song / queue · `/autoplay` keep playing similar songs when the queue runs out · `/stay` don't leave when idle |
| **Sound** | `/volume` · `/bassboost` · `/filter` nightcore / vaporwave / 8D |
| **Other** | `/lyrics` |

`/remove`, `/move` and `/skipto` suggest the queued songs as you type — pick one by name instead of counting positions.

**The player card.** Each song posts a *Now playing* card — artwork, title, a progress bar, what's up next — with buttons like a phone's music controls:

| ⏮️ back | ⏯️ pause / resume | ⏭️ skip | ⏹️ stop | 📜 queue |
|---|---|---|---|---|
| 🔀 **shuffle** | 🔁 **loop** (off → queue → song) | 🔉 **volume −10** | 🔊 **volume +10** | 📻 **autoplay** |

Only one card is shown at a time (the newest, at the bottom of the chat), and it updates itself every few seconds. Anyone in the bot's voice channel can use the buttons; 📜 works for everyone. Cards from before a restart stop responding — `/nowplaying` brings up a fresh one.

## What you're running

```
   Discord  ◂──gateway──▸  bot.py (Python)  ◂──HTTP/WS──▸  Lavalink (Java, Docker)
      ▴                                                          │  └─ yt-dlp + Deno
      └──────────────────── voice audio ◂────────────────────────┘
```

Lavalink does the actual audio work; the bot tells it what to play. YouTube goes through **yt-dlp**, which is baked into a custom Lavalink image built from `lavalink/Dockerfile`. Lavalink needs ~512 MB RAM, the bot about 100 MB.

## Setup

### 1. Create the Discord application

1. Go to the [Developer Portal](https://discord.com/developers/applications) → **New Application**.
2. **Bot** tab → **Reset Token** → copy it. That's your `DISCORD_TOKEN`.
3. Invite the bot with this URL, using your **Application ID** from the General Information page:

   ```
   https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot%20applications.commands&permissions=3165184
   ```

No Privileged Gateway Intents are needed — the bot is slash-command only.

### 2. Configure

```powershell
copy .env.example .env
copy lavalink\application.example.yml lavalink\application.yml
```

- **`.env`** — fill in `DISCORD_TOKEN` and `GUILD_ID` (right-click your server icon → Copy Server ID; needs Developer Mode under Settings → Advanced). With `GUILD_ID` set, slash commands appear instantly.
- **`lavalink/application.yml`** — optional Spotify credentials (see below). YouTube, SoundCloud and direct links work as-is.

Both files hold secrets and are gitignored. Never commit them.

### 3. Install

Requires Python 3.11+ and [Docker Desktop](https://docs.docker.com/get-started/get-docker/).

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 4. Run

**Windows:** double-click **`start-bot.bat`**. It waits for Docker, builds/refreshes the Lavalink image (once a day, to pick up the newest yt-dlp), restarts Lavalink so config changes apply, then starts the bot. Keep the window open; closing it stops the bot. Put a shortcut in `shell:startup` to launch it at login.

The very first launch builds the image from scratch — expect a minute or two.

**Manually / other systems:**

```bash
docker compose build          # first time, and whenever you want a newer yt-dlp
docker compose up -d
python bot.py
```

Wait for `Lavalink node ready` and `Logged in as …`, join a voice channel, and `/play` something.

## Audio sources

| Source | Status | Notes |
|---|---|---|
| YouTube | ✅ Mostly | Via yt-dlp. Songs take 1–3 s longer to start than other sources. |
| SoundCloud | ✅ | Pick **SoundCloud** in `/play`'s `source` option, or paste a link. |
| Spotify tracks | ✅ | Needs Spotify credentials. Plays a matching YouTube copy. |
| Spotify playlists | ❌ | Spotify blocks playlist contents for personal (Development Mode) apps. Copy the playlist to YouTube (e.g. with TuneMyMusic) and paste that link instead. |
| Podcasts via RSS | ✅ | Open the feed, copy an episode's `<enclosure url="…">`, paste it into `/play`. |
| Internet radio / direct `.mp3` | ✅ | Paste the stream URL. |

**Automatic fallbacks.** If a YouTube song fails, the bot retries it once (a fresh yt-dlp link often works). If it fails again, it finds the same song on SoundCloud and plays that instead (🔁). If a SoundCloud upload turns out to be a 30-second SoundCloud Go+ preview, it switches to another upload and continues from 0:30 (✂️).

### YouTube (yt-dlp)

YouTube actively fights self-hosted players and changes things often. yt-dlp is the most actively maintained tool for keeping up, which is why this bot uses it through [LavaSrc's yt-dlp source](https://github.com/topi314/LavaSrc) instead of the usual Lavalink YouTube plugin.

- **Staying current:** `start-bot.bat` downloads the latest yt-dlp on the first launch of each day. If YouTube starts failing, restarting the bot the next day is often the whole fix. To force it immediately: `docker compose build --no-cache` then restart.
- **If YouTube blocks your connection** ("Sign in to confirm you're not a bot"), yt-dlp can use a burner Google account's cookies, or a PO token provider. Both need extra setup — see yt-dlp's [PO token guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide) and [FAQ](https://github.com/yt-dlp/yt-dlp/wiki/FAQ) ("How do I pass cookies"). Never use your main account.
- **Switching back to the youtube-source plugin:** in `lavalink/application.yml`, uncomment the `youtube-plugin` dependency and set `ytdlp: false`. Don't enable both — they register under the same name and clash.

### Spotify (optional)

Create a free app at the [Spotify dashboard](https://developer.spotify.com/dashboard). The redirect URI is required but unused — enter `http://127.0.0.1:8080` (Spotify rejects `localhost`). Paste the client ID and secret into `plugins.lavasrc.spotify` in `application.yml` and restart.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Commands missing or duplicated | Check `GUILD_ID` in `.env`, restart, then `Ctrl+R` in Discord. |
| "Lavalink is offline" | `docker compose ps` — if it's restarting, `docker compose logs --tail 40` shows why (usually a YAML mistake or a placeholder value). |
| Image build fails | Needs internet access to GitHub. If it fails, the bot keeps using the last image that built. |
| YouTube songs fail | Check `docker compose logs --tail 60` for the yt-dlp error. Try again tomorrow (auto-update) or force an update — see **YouTube** above. The SoundCloud fallback covers you meanwhile. |
| A song goes silent ~30 s in | A SoundCloud preview. The bot should auto-switch; if every upload is a preview, the song is skipped. |
| Spotify link fails | Missing or wrong credentials, or it's a playlist (not supported). |
| Bot leaves during a long pause | `/stay`, or raise `INACTIVE_TIMEOUT` in `.env`. |
| Audio stutters | Raise Lavalink's memory: `_JAVA_OPTIONS: -Xmx1G` in `docker-compose.yml`. |

## Note on legality

Streaming from YouTube this way is against YouTube's terms of service — it's what got large public music bots like Groovy and Rythm shut down. A private bot for a handful of friends is a very different situation, but keep it private and don't monetize it.
