# Lavalink + yt-dlp + Deno, for LavaSrc's yt-dlp YouTube source.
#
# yt-dlp is the most actively maintained YouTube downloader; YouTube breaks
# things often and yt-dlp's fixes usually land within days. start-bot.bat
# rebuilds this image once a day (via YTDLP_REFRESH) to pick up new releases.
#
# Deno is the JavaScript runtime yt-dlp needs for full YouTube support.

# --- Stage 1: download and unpack the tools --------------------------------
FROM debian:bookworm-slim AS tools

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl unzip \
 && rm -rf /var/lib/apt/lists/*

# Changing this value (start-bot.bat passes today's date) forces a fresh
# download of the latest yt-dlp and Deno; everything above stays cached.
ARG YTDLP_REFRESH=manual
RUN echo "yt-dlp refresh key: ${YTDLP_REFRESH}" \
 && mkdir -p /out/yt-dlp \
 && curl -fsSL -o /tmp/yt-dlp.zip https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux.zip \
 && unzip -q /tmp/yt-dlp.zip -d /out/yt-dlp \
 && chmod 755 /out/yt-dlp/yt-dlp_linux \
 && curl -fsSL -o /tmp/deno.zip https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip \
 && unzip -q /tmp/deno.zip -d /out \
 && chmod 755 /out/deno

# --- Stage 2: the normal Lavalink image, plus the tools -----------------------
FROM ghcr.io/lavalink-devs/lavalink:4

# yt-dlp's "onedir" build: starts faster than the single-file build and
# already includes yt-dlp's JavaScript challenge solver (yt-dlp-ejs).
COPY --from=tools /out/yt-dlp /opt/yt-dlp
COPY --from=tools /out/deno /usr/local/bin/deno

# The container user has no home folder, so point yt-dlp's and Deno's caches
# somewhere writable. Caching YouTube's player script makes songs start faster.
ENV XDG_CACHE_HOME=/tmp/cache \
    DENO_DIR=/tmp/deno
