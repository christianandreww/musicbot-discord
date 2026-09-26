# Personal Music Bot

A private Discord music bot for one server: hop in a voice channel with friends and play songs, podcasts, or radio streams. No premium tiers, no database, no web dashboard.

**Commands:** `/play` `/pause` `/skip` `/seek` `/nowplaying` `/queue` `/shuffle` `/volume` `/bassboost` `/stay` `/stop` `/lyrics`

Everything is open to everyone in the server.

## What you're running

```
   Discord  ◂──gateway──▸  bot.py (Python)  ◂──HTTP/WS──▸  Lavalink (Java, Docker)
      ▴                                                          │
      └──────────────────── voice audio ◂────────────────────────┘
```

Lavalink does the actual audio work; the bot tells it what to play. Lavalink needs ~512 MB RAM, the bot about 100 MB.

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
- **`lavalink/application.yml`** — optional Spotify credentials and the YouTube sign-in; see the sections below. It works as-is for SoundCloud and direct links.

Both files hold secrets and are gitignored. Never commit them.

### 3. Install

Requires Python 3.11+ and [Docker Desktop](https://docs.docker.com/get-started/get-docker/).

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 4. Run

**Windows:** double-click **`start-bot.bat`**. It waits for Docker, (re)starts Lavalink so config changes apply, then starts the bot. Keep the window open; closing it stops the bot. Put a shortcut in `shell:startup` to launch it at login.

**Manually / other systems:**

```bash
docker compose up -d
python bot.py
```

Wait for `Lavalink node ready` and `Logged in as …`, join a voice channel, and `/play` something.

## Audio sources

| Source | Status | Notes |
|---|---|---|
| YouTube | ⚠️ Unreliable | See below. Needs a burner-account sign-in. |
| SoundCloud | ✅ | Pick **SoundCloud** in `/play`'s `source` option, or paste a link. |
| Spotify tracks | ✅ | Needs Spotify credentials. Plays a matching YouTube/SoundCloud copy. |
| Spotify playlists | ❌ | Spotify blocks playlist contents for personal (Development Mode) apps. Copy the playlist to YouTube (e.g. with TuneMyMusic) and paste that link instead. |
| Podcasts via RSS | ✅ | Open the feed, copy an episode's `<enclosure url="…">`, paste it into `/play`. |
| Internet radio / direct `.mp3` | ✅ | Paste the stream URL. |

**Automatic fallbacks.** If YouTube refuses to stream a song, the bot finds the same song on SoundCloud and plays that instead (🔁). If a SoundCloud upload turns out to be a 30-second SoundCloud Go+ preview, it switches to another upload and continues from 0:30 (✂️).

### YouTube

YouTube actively blocks self-hosted players. As of September 2026, playback often fails with *"Sign in to confirm you're not a bot"* or *"No supported audio streams"*, frequently working for the first song and then refusing the rest. The SoundCloud fallback covers this.

To give it the best chance:

1. In `lavalink/application.yml`, keep `oauth: enabled: true` with the `refreshToken` line commented out, and start Lavalink.
2. Run `docker compose logs -f`, open the `google.com/device` link it prints, and sign in with a **throwaway Google account — never your main one**; accounts used this way can get flagged. Sign into youtube.com with it in a browser first and watch something, since brand-new accounts are often refused.
3. Copy the refresh token from the log into `refreshToken: "…"`, uncomment the line, and restart.

**When YouTube breaks:** check the [youtube-source releases](https://github.com/lavalink-devs/youtube-source/releases). If there's a version newer than the one in `application.yml`, bump it and restart — that is usually the actual fix. Also check the [client list](https://github.com/lavalink-devs/youtube-source#available-clients) for newly added clients.

### Spotify (optional)

Create a free app at the [Spotify dashboard](https://developer.spotify.com/dashboard). The redirect URI is required but unused — enter `http://127.0.0.1:8080` (Spotify rejects `localhost`). Paste the client ID and secret into `plugins.lavasrc.spotify` in `application.yml` and restart Lavalink.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Commands missing or duplicated | Check `GUILD_ID` in `.env`, restart, then `Ctrl+R` in Discord. |
| "Lavalink is offline" | `docker compose ps` — if it's restarting, `docker compose logs --tail 40` shows why (usually a YAML mistake or a placeholder value). |
| Lavalink crash: `Invalid status code for oauth2 token fetch: 400` | A placeholder or expired `refreshToken`. Comment the line out and redo the sign-in. |
| YouTube songs fail | Expected at the moment — see **YouTube** above. The SoundCloud fallback should kick in. |
| A song goes silent ~30 s in | A SoundCloud preview. The bot should auto-switch; if every upload is a preview, the song is skipped. |
| Spotify link fails | Missing or wrong credentials, or it's a playlist (not supported). |
| Bot leaves during a long pause | `/stay`, or raise `INACTIVE_TIMEOUT` in `.env`. |
| Audio stutters | Raise Lavalink's memory: `_JAVA_OPTIONS: -Xmx1G` in `docker-compose.yml`. |

## Note on legality

Streaming from YouTube this way is against YouTube's terms of service — it's what got large public music bots like Groovy and Rythm shut down. A private bot for a handful of friends is a very different situation, but keep it private and don't monetize it.
