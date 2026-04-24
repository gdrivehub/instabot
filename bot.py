import os
import re
import time
import random
import logging
import asyncio
import tempfile
import shutil
from pathlib import Path
from aiohttp import web

from telegram import Update, InputMediaPhoto, InputMediaVideo
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
import yt_dlp

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
HEALTH_PORT = int(os.getenv("PORT", "8000"))
IG_COOKIES_FILE = os.getenv("IG_COOKIES_FILE", "")

# Write cookies from env var to disk at startup (for Koyeb)
_IG_COOKIES_CONTENT = os.getenv("IG_COOKIES_CONTENT", "")
if _IG_COOKIES_CONTENT and not IG_COOKIES_FILE:
    _cookie_path = "/tmp/cookies.txt"
    with open(_cookie_path, "w") as _f:
        _f.write(_IG_COOKIES_CONTENT)
    IG_COOKIES_FILE = _cookie_path
    logger.info("Cookies written to %s", _cookie_path)

# ── Rate-limit state ───────────────────────────────────────────────────────
# Track the last download time to enforce a minimum gap between requests
_last_download_time: float = 0.0
_download_lock = asyncio.Lock()

# Minimum seconds to wait between downloads (randomised to look human)
MIN_DELAY = float(os.getenv("MIN_DELAY", "4"))
MAX_DELAY = float(os.getenv("MAX_DELAY", "8"))

# ── Instagram URL helpers ──────────────────────────────────────────────────
INSTAGRAM_URL_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/"
    r"(?:p|reel|reels|stories|s|tv)/[\w\-]+/?",
    re.IGNORECASE,
)


def is_instagram_url(text: str) -> bool:
    return bool(INSTAGRAM_URL_RE.search(text))


def extract_url(text: str) -> str:
    m = INSTAGRAM_URL_RE.search(text)
    return m.group(0) if m else ""


# ── yt-dlp options ─────────────────────────────────────────────────────────

# Rotate through realistic User-Agents
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]


def _build_ydl_opts(tmpdir: str) -> dict:
    opts = {
        "outtmpl": os.path.join(tmpdir, "%(playlist_index)s%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        "format": "bestvideo+bestaudio/best",
        "writedescription": True,
        "writethumbnail": False,
        "writeinfojson": False,
        "noplaylist": False,
        # Retry settings
        "retries": 3,
        "fragment_retries": 3,
        "retry_sleep_functions": {"http": lambda n: 2 ** n},
        # Socket / connection settings
        "socket_timeout": 30,
        "http_headers": {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Sec-Fetch-Mode": "navigate",
        },
    }
    if IG_COOKIES_FILE and os.path.isfile(IG_COOKIES_FILE):
        opts["cookiefile"] = IG_COOKIES_FILE
        logger.info("Using cookies from %s", IG_COOKIES_FILE)
    else:
        logger.warning("No cookies file found — anonymous access (high rate-limit risk)")
    return opts


def _download_sync(url: str, tmpdir: str) -> tuple[list[Path], str]:
    """Blocking yt-dlp download. Run via asyncio.to_thread."""
    with yt_dlp.YoutubeDL(_build_ydl_opts(tmpdir)) as ydl:
        info = ydl.extract_info(url, download=True)

    if info is None:
        raise RuntimeError("yt-dlp returned no info for this URL.")

    caption = ""
    for entry in (info.get("entries") or [info]):
        if entry and entry.get("description"):
            caption = entry["description"].strip()
            break

    if not caption:
        for desc_file in sorted(Path(tmpdir).glob("*.description")):
            text = desc_file.read_text(encoding="utf-8", errors="ignore").strip()
            if text:
                caption = text
                break

    media_exts = {".mp4", ".mov", ".webm", ".mkv", ".jpg", ".jpeg", ".png", ".webp"}
    media_files = sorted(
        p for p in Path(tmpdir).iterdir()
        if p.suffix.lower() in media_exts
    )
    return media_files, caption


async def download_instagram(url: str, tmpdir: str) -> tuple[list[Path], str]:
    """
    Download with a per-process lock and a human-like delay between requests
    to avoid Instagram rate-limiting.
    """
    global _last_download_time

    async with _download_lock:
        # Enforce minimum gap since last download
        elapsed = time.monotonic() - _last_download_time
        delay = random.uniform(MIN_DELAY, MAX_DELAY)
        wait = max(0.0, delay - elapsed)
        if wait > 0:
            logger.info("Waiting %.1fs before download (rate-limit protection)", wait)
            await asyncio.sleep(wait)

        result = await asyncio.to_thread(_download_sync, url, tmpdir)
        _last_download_time = time.monotonic()
        return result


# ── Caption helper ─────────────────────────────────────────────────────────

def trim_caption(caption: str, max_len: int = 1024) -> str:
    caption = caption.strip()
    return caption[: max_len - 1] + "…" if len(caption) > max_len else caption


# ── Send media ─────────────────────────────────────────────────────────────
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv"}
MAX_GROUP = 10


async def send_media(update: Update, media_files: list[Path], caption: str) -> None:
    if not media_files:
        await update.message.reply_text("⚠️ No media found in this post.")
        return

    cap = trim_caption(caption) if caption else None

    if len(media_files) == 1:
        path = media_files[0]
        with open(path, "rb") as f:
            if path.suffix.lower() in VIDEO_EXTS:
                await update.message.reply_video(video=f, caption=cap, supports_streaming=True)
            else:
                await update.message.reply_photo(photo=f, caption=cap)
        return

    chunks = [media_files[i: i + MAX_GROUP] for i in range(0, len(media_files), MAX_GROUP)]
    for chunk_idx, chunk in enumerate(chunks):
        media_group, handles = [], []
        for item_idx, path in enumerate(chunk):
            fh = open(path, "rb")
            handles.append(fh)
            item_cap = cap if (chunk_idx == 0 and item_idx == 0) else None
            if path.suffix.lower() in VIDEO_EXTS:
                media_group.append(InputMediaVideo(media=fh, caption=item_cap))
            else:
                media_group.append(InputMediaPhoto(media=fh, caption=item_cap))
        try:
            await update.message.reply_media_group(media=media_group)
        finally:
            for fh in handles:
                fh.close()


# ── Telegram handlers ──────────────────────────────────────────────────────

START_MSG = (
    "<b>👋 Welcome to InstaGrab Bot!</b>\n\n"
    "Send me any Instagram link and I'll download it for you:\n\n"
    "• 📸 Posts (photos &amp; carousels)\n"
    "• 🎬 Reels\n"
    "• 📺 IGTV\n"
    "• 📖 Stories\n\n"
    "Just paste the URL and hit send!"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(START_MSG, parse_mode="HTML")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()

    if not is_instagram_url(text):
        await update.message.reply_text(
            "🔗 Please send a valid Instagram URL (post, reel, story, or IGTV)."
        )
        return

    url = extract_url(text)

    # Tell user if they'll need to wait due to rate-limit delay
    elapsed = time.monotonic() - _last_download_time
    wait_est = max(0.0, MIN_DELAY - elapsed)
    if wait_est > 1:
        status = await update.message.reply_text(
            f"⏳ Downloading… (brief delay to avoid Instagram rate-limits, ~{int(wait_est)+1}s)"
        )
    else:
        status = await update.message.reply_text("⏳ Downloading… please wait.")

    tmpdir = tempfile.mkdtemp(prefix="instabot_")
    try:
        media_files, caption = await download_instagram(url, tmpdir)
        await status.delete()
        await send_media(update, media_files, caption)

    except yt_dlp.utils.ExtractorError as exc:
        msg = str(exc)
        logger.error("ExtractorError for %s: %s", url, msg)
        if "login" in msg.lower() or "private" in msg.lower() or "rate" in msg.lower():
            await status.edit_text(
                "🔒 Instagram blocked this request.\n\n"
                "This usually means:\n"
                "• Your cookies have expired — re-export and update <code>IG_COOKIES_CONTENT</code>\n"
                "• The post is private\n"
                "• Instagram is temporarily blocking this server's IP\n\n"
                "Try again in a few minutes.",
                parse_mode="HTML",
            )
        else:
            await status.edit_text(
                "❌ Could not extract this post. It may have been deleted or is unavailable."
            )

    except yt_dlp.utils.DownloadError as exc:
        msg = str(exc)
        logger.error("DownloadError for %s: %s", url, msg)
        if "rate" in msg.lower() or "login" in msg.lower() or "not available" in msg.lower():
            await status.edit_text(
                "⚠️ <b>Instagram rate-limit hit.</b>\n\n"
                "Your cookies may have expired. To fix:\n"
                "1. Export fresh cookies from your browser\n"
                "2. Update <code>IG_COOKIES_CONTENT</code> in Koyeb env vars\n"
                "3. Redeploy\n\n"
                "Or just wait a few minutes and try again.",
                parse_mode="HTML",
            )
        else:
            await status.edit_text(
                "❌ Download failed. Please try again in a few minutes."
            )

    except Exception:
        logger.exception("Unexpected error for URL %s", url)
        await status.edit_text("❌ Something went wrong. Please try again later.")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── Async health-check server ──────────────────────────────────────────────

async def _health(_request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def _start_health_server() -> None:
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HEALTH_PORT)
    await site.start()
    logger.info("Health-check server on port %s", HEALTH_PORT)


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def post_init(application: Application) -> None:
        await _start_health_server()

    app.post_init = post_init

    logger.info("Bot is running…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
