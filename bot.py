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
    ConversationHandler,
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

# ── Proxy state ────────────────────────────────────────────────────────────
# In-memory proxy list — loaded at runtime via /proxy command
_proxies: list[str] = []

# Conversation state for /proxy file upload
WAITING_FOR_PROXY_FILE = 1

MIN_DELAY = float(os.getenv("MIN_DELAY", "4"))
MAX_DELAY = float(os.getenv("MAX_DELAY", "8"))
_last_download_time: float = 0.0
_download_lock = asyncio.Lock()

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


# ── Proxy helpers ──────────────────────────────────────────────────────────

def _parse_proxy_file(content: str) -> list[str]:
    """Parse proxy list from file content. One proxy per line."""
    proxies = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Auto-prefix with http:// if no scheme given
        if not re.match(r"^(http|https|socks4|socks5)://", line, re.IGNORECASE):
            line = "http://" + line
        proxies.append(line)
    return proxies


def _get_random_proxy() -> str | None:
    """Return a random proxy from the loaded list, or None if empty."""
    return random.choice(_proxies) if _proxies else None


# ── yt-dlp options ─────────────────────────────────────────────────────────

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]


def _build_ydl_opts(tmpdir: str, proxy: str | None = None) -> dict:
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
        "retries": 3,
        "fragment_retries": 3,
        "retry_sleep_functions": {"http": lambda n: 2 ** n},
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
    if proxy:
        opts["proxy"] = proxy
        logger.info("Using proxy: %s", proxy)
    return opts


def _download_sync(url: str, tmpdir: str, proxy: str | None) -> tuple[list[Path], str]:
    """Blocking yt-dlp download. Run via asyncio.to_thread."""
    with yt_dlp.YoutubeDL(_build_ydl_opts(tmpdir, proxy)) as ydl:
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
    global _last_download_time

    async with _download_lock:
        elapsed = time.monotonic() - _last_download_time
        delay = random.uniform(MIN_DELAY, MAX_DELAY)
        wait = max(0.0, delay - elapsed)
        if wait > 0:
            logger.info("Waiting %.1fs before download (rate-limit protection)", wait)
            await asyncio.sleep(wait)

        proxy = _get_random_proxy()
        result = await asyncio.to_thread(_download_sync, url, tmpdir, proxy)
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


# ── /proxy conversation handler ────────────────────────────────────────────

async def cmd_proxy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for /proxy — ask user to send the file."""
    proxy_status = (
        f"✅ <b>{len(_proxies)} proxies</b> currently loaded."
        if _proxies
        else "⚠️ No proxies loaded yet."
    )
    await update.message.reply_text(
        f"{proxy_status}\n\n"
        "📄 Send me a <b>.txt file</b> with one proxy per line to load/replace them.\n\n"
        "<b>Supported formats:</b>\n"
        "<code>ip:port</code>\n"
        "<code>http://ip:port</code>\n"
        "<code>http://user:pass@ip:port</code>\n"
        "<code>socks5://ip:port</code>\n\n"
        "Lines starting with <code>#</code> are treated as comments.\n\n"
        "Send /cancel to abort.",
        parse_mode="HTML",
    )
    return WAITING_FOR_PROXY_FILE


async def receive_proxy_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the .txt file and load proxies from it."""
    global _proxies

    doc = update.message.document
    if not doc:
        await update.message.reply_text("❌ Please send a .txt file, not a message.")
        return WAITING_FOR_PROXY_FILE

    if not doc.file_name.lower().endswith(".txt"):
        await update.message.reply_text(
            "❌ File must be a <b>.txt</b> file. Please try again or /cancel.",
            parse_mode="HTML",
        )
        return WAITING_FOR_PROXY_FILE

    # Download file content
    status = await update.message.reply_text("⏳ Loading proxies…")
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        content_bytes = await tg_file.download_as_bytearray()
        content = content_bytes.decode("utf-8", errors="ignore")
    except Exception as exc:
        logger.exception("Failed to download proxy file")
        await status.edit_text(f"❌ Could not read the file: {exc}")
        return ConversationHandler.END

    parsed = _parse_proxy_file(content)

    if not parsed:
        await status.edit_text(
            "❌ No valid proxies found in the file.\n"
            "Make sure each line contains a proxy in <code>ip:port</code> or full URL format.",
            parse_mode="HTML",
        )
        return WAITING_FOR_PROXY_FILE

    _proxies = parsed
    logger.info("Loaded %d proxies from uploaded file", len(_proxies))

    # Show a preview of loaded proxies (first 5)
    preview = "\n".join(f"• <code>{p}</code>" for p in _proxies[:5])
    more = f"\n<i>…and {len(_proxies) - 5} more</i>" if len(_proxies) > 5 else ""

    await status.edit_text(
        f"✅ <b>{len(_proxies)} proxies loaded successfully!</b>\n\n"
        f"{preview}{more}\n\n"
        "Proxies will be picked randomly for each download.",
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


async def cmd_proxystatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current proxy list."""
    if not _proxies:
        await update.message.reply_text("⚠️ No proxies loaded. Use /proxy to upload a list.")
        return

    preview = "\n".join(f"• <code>{p}</code>" for p in _proxies[:10])
    more = f"\n<i>…and {len(_proxies) - 10} more</i>" if len(_proxies) > 10 else ""
    await update.message.reply_text(
        f"🌐 <b>{len(_proxies)} proxies loaded:</b>\n\n{preview}{more}",
        parse_mode="HTML",
    )


async def cmd_clearproxy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear all proxies."""
    global _proxies
    _proxies = []
    await update.message.reply_text("🗑️ All proxies cleared. Bot will use direct connection.")


# ── /start ─────────────────────────────────────────────────────────────────

START_MSG = (
    "<b>👋 Welcome to InstaGrab Bot!</b>\n\n"
    "Send me any Instagram link and I'll download it for you:\n\n"
    "• 📸 Posts (photos &amp; carousels)\n"
    "• 🎬 Reels\n"
    "• 📺 IGTV\n"
    "• 📖 Stories\n\n"
    "<b>Commands:</b>\n"
    "/proxy — Upload a proxy list (.txt file)\n"
    "/proxystatus — View loaded proxies\n"
    "/clearproxy — Remove all proxies\n\n"
    "Just paste an Instagram URL to download!"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(START_MSG, parse_mode="HTML")


# ── URL message handler ────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()

    if not is_instagram_url(text):
        await update.message.reply_text(
            "🔗 Please send a valid Instagram URL (post, reel, story, or IGTV)."
        )
        return

    url = extract_url(text)

    proxy_info = f" via proxy" if _proxies else ""
    elapsed = time.monotonic() - _last_download_time
    wait_est = max(0.0, MIN_DELAY - elapsed)
    if wait_est > 1:
        status = await update.message.reply_text(
            f"⏳ Downloading{proxy_info}… (~{int(wait_est)+1}s delay for rate-limit protection)"
        )
    else:
        status = await update.message.reply_text(f"⏳ Downloading{proxy_info}… please wait.")

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
                "Try:\n"
                "• Uploading fresh proxies with /proxy\n"
                "• Updating your cookies in Koyeb env vars\n"
                "• Waiting a few minutes and retrying",
                parse_mode="HTML",
            )
        else:
            await status.edit_text("❌ Could not extract this post. It may be deleted or private.")

    except yt_dlp.utils.DownloadError as exc:
        msg = str(exc)
        logger.error("DownloadError for %s: %s", url, msg)
        if "rate" in msg.lower() or "login" in msg.lower() or "not available" in msg.lower():
            await status.edit_text(
                "⚠️ <b>Instagram rate-limit hit.</b>\n\n"
                "Try uploading proxies with /proxy or wait a few minutes.",
                parse_mode="HTML",
            )
        else:
            await status.edit_text("❌ Download failed. Please try again in a few minutes.")

    except Exception:
        logger.exception("Unexpected error for URL %s", url)
        await status.edit_text("❌ Something went wrong. Please try again later.")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── Health check server ────────────────────────────────────────────────────

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

    # /proxy conversation: command → wait for file → done
    proxy_conv = ConversationHandler(
        entry_points=[CommandHandler("proxy", cmd_proxy)],
        states={
            WAITING_FOR_PROXY_FILE: [
                MessageHandler(filters.Document.TXT, receive_proxy_file),
                CommandHandler("cancel", cmd_cancel),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    app.add_handler(proxy_conv)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("proxystatus", cmd_proxystatus))
    app.add_handler(CommandHandler("clearproxy", cmd_clearproxy))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def post_init(application: Application) -> None:
        await _start_health_server()
        # Register commands in Telegram menu
        await application.bot.set_my_commands([
            ("start", "Welcome message & instructions"),
            ("proxy", "Upload a proxy list (.txt file)"),
            ("proxystatus", "View currently loaded proxies"),
            ("clearproxy", "Remove all proxies"),
        ])

    app.post_init = post_init

    logger.info("Bot is running…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
