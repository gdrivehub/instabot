import os
import re
import logging
import asyncio
import tempfile
import shutil
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from telegram import Update, InputMediaPhoto, InputMediaVideo
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode
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

# Optional: path to a Netscape-format cookies file exported from your browser
# while logged in to Instagram. Set IG_COOKIES_FILE env var on Koyeb.
IG_COOKIES_FILE = os.getenv("IG_COOKIES_FILE", "")

# ── Tiny HTTP health-check server (Koyeb requires this) ───────────────────
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *_):
        pass  # suppress access log spam


def _start_health_server():
    server = HTTPServer(("0.0.0.0", HEALTH_PORT), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health-check server listening on port %s", HEALTH_PORT)


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


# ── yt-dlp download ────────────────────────────────────────────────────────

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
        "noplaylist": False,   # allow carousels (playlists)
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        },
    }
    if IG_COOKIES_FILE and os.path.isfile(IG_COOKIES_FILE):
        opts["cookiefile"] = IG_COOKIES_FILE
        logger.info("Using Instagram cookies from %s", IG_COOKIES_FILE)
    return opts


def _download_sync(url: str, tmpdir: str) -> tuple[list[Path], str]:
    """Blocking download — called via asyncio.to_thread."""
    opts = _build_ydl_opts(tmpdir)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    if info is None:
        raise RuntimeError("yt-dlp returned no info for this URL.")

    # Grab caption from info dict
    caption = ""
    entries = info.get("entries") or [info]
    for entry in entries:
        if entry and entry.get("description"):
            caption = entry["description"].strip()
            break

    # Fallback: read .description files written by yt-dlp
    if not caption:
        for desc_file in sorted(Path(tmpdir).glob("*.description")):
            text = desc_file.read_text(encoding="utf-8", errors="ignore").strip()
            if text:
                caption = text
                break

    # Collect media files (skip .description and other text files)
    media_exts = {".mp4", ".mov", ".webm", ".mkv", ".jpg", ".jpeg", ".png", ".webp"}
    media_files: list[Path] = sorted(
        p for p in Path(tmpdir).iterdir()
        if p.suffix.lower() in media_exts
    )

    return media_files, caption


async def download_instagram(url: str, tmpdir: str) -> tuple[list[Path], str]:
    return await asyncio.to_thread(_download_sync, url, tmpdir)


# ── Caption helper ─────────────────────────────────────────────────────────

def trim_caption(caption: str, max_len: int = 1024) -> str:
    caption = caption.strip()
    if len(caption) > max_len:
        caption = caption[: max_len - 1] + "…"
    return caption


# ── Send media to Telegram ─────────────────────────────────────────────────
MAX_GROUP = 10
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv"}


async def send_media(update: Update, media_files: list[Path], caption: str) -> None:
    if not media_files:
        await update.message.reply_text("⚠️ No media found in this post.")
        return

    cap = trim_caption(caption) if caption else None

    # Single file
    if len(media_files) == 1:
        path = media_files[0]
        with open(path, "rb") as f:
            if path.suffix.lower() in VIDEO_EXTS:
                await update.message.reply_video(video=f, caption=cap, supports_streaming=True)
            else:
                await update.message.reply_photo(photo=f, caption=cap)
        return

    # Multiple files → send as media group(s) of up to 10
    chunks = [media_files[i: i + MAX_GROUP] for i in range(0, len(media_files), MAX_GROUP)]
    for chunk_idx, chunk in enumerate(chunks):
        media_group = []
        handles = []
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
    "👋 *Welcome to InstaGrab Bot!*\n\n"
    "Send me any Instagram link and I'll download it for you:\n\n"
    "• 📸 Posts \\(photos & carousels\\)\n"
    "• 🎬 Reels\n"
    "• 📺 IGTV\n"
    "• 📖 Stories\n\n"
    "Just paste the URL and hit send\\!"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(START_MSG, parse_mode=ParseMode.MARKDOWN_V2)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()

    if not is_instagram_url(text):
        await update.message.reply_text(
            "🔗 Please send a valid Instagram URL (post, reel, story, or IGTV)."
        )
        return

    url = extract_url(text)
    status = await update.message.reply_text("⏳ Downloading… please wait.")

    tmpdir = tempfile.mkdtemp(prefix="instabot_")
    try:
        media_files, caption = await download_instagram(url, tmpdir)
        await status.delete()
        await send_media(update, media_files, caption)

    except yt_dlp.utils.ExtractorError as exc:
        msg = str(exc)
        logger.error("ExtractorError for %s: %s", url, msg)
        if "login" in msg.lower() or "private" in msg.lower():
            await status.edit_text(
                "🔒 This content is private or requires login.\n"
                "Ask the bot admin to configure Instagram cookies."
            )
        else:
            await status.edit_text(
                "❌ Could not extract this post. It may have been deleted or is unavailable."
            )

    except yt_dlp.utils.DownloadError as exc:
        logger.error("DownloadError for %s: %s", url, exc)
        await status.edit_text(
            "❌ Download failed. Instagram may be rate-limiting. "
            "Please try again in a few minutes."
        )

    except Exception:
        logger.exception("Unexpected error for URL %s", url)
        await status.edit_text("❌ Something went wrong. Please try again later.")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    _start_health_server()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot is running…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
