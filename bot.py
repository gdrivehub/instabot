import os
import re
import json
import logging
import asyncio
import tempfile
import shutil
import time
import random
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
from instagrapi import Client
from instagrapi.exceptions import (
    LoginRequired,
    ChallengeRequired,
    BadPassword,
    UserNotFound,
    MediaNotFound,
    PrivateError,
    RateLimitError,
)

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────
BOT_TOKEN       = os.environ["BOT_TOKEN"]
HEALTH_PORT     = int(os.getenv("PORT", "8000"))
IG_USERNAME     = os.getenv("IG_USERNAME", "")
IG_PASSWORD     = os.getenv("IG_PASSWORD", "")
SESSION_FILE    = os.getenv("SESSION_FILE", "/tmp/ig_session.json")

# ── Instagram client (singleton) ───────────────────────────────────────────
_cl: Client | None = None
_cl_lock = asyncio.Lock()
_last_request_time: float = 0.0

MIN_DELAY = 3.0   # seconds between Instagram requests


def _build_client() -> Client:
    cl = Client()
    cl.delay_range = [2, 5]   # instagrapi built-in random delay between requests
    return cl


def _load_session(cl: Client) -> bool:
    """Try to load an existing session from disk. Returns True if successful."""
    if os.path.isfile(SESSION_FILE):
        try:
            cl.load_settings(SESSION_FILE)
            cl.login(IG_USERNAME, IG_PASSWORD)   # re-auth with saved session
            logger.info("Session loaded from %s", SESSION_FILE)
            return True
        except Exception as exc:
            logger.warning("Could not reuse saved session: %s", exc)
    return False


def _fresh_login(cl: Client) -> None:
    """Perform a fresh login and save the session."""
    cl.login(IG_USERNAME, IG_PASSWORD)
    cl.dump_settings(SESSION_FILE)
    logger.info("Fresh login successful — session saved to %s", SESSION_FILE)


def _get_client() -> Client:
    """Return the logged-in singleton client (sync, call from thread)."""
    global _cl
    if _cl is not None:
        return _cl
    cl = _build_client()
    if not _load_session(cl):
        _fresh_login(cl)
    _cl = cl
    return _cl


async def get_client() -> Client:
    async with _cl_lock:
        return await asyncio.to_thread(_get_client)


def _relogin(cl: Client) -> None:
    """Force a fresh login (e.g. after session expiry)."""
    global _cl
    logger.info("Session expired — re-logging in")
    cl2 = _build_client()
    _fresh_login(cl2)
    _cl = cl2


# ── URL parsing ────────────────────────────────────────────────────────────
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


def extract_shortcode(url: str) -> str | None:
    m = re.search(r"/(?:p|reel|reels|tv)/([\w\-]+)", url)
    return m.group(1) if m else None


def extract_story_info(url: str) -> tuple[str, int] | None:
    m = re.search(r"/stories/([\w\.]+)/(\d+)", url)
    return (m.group(1), int(m.group(2))) if m else None


# ── Download logic ─────────────────────────────────────────────────────────

def _throttle():
    """Block until MIN_DELAY seconds have passed since last request."""
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    wait = max(0.0, MIN_DELAY + random.uniform(0, 2) - elapsed)
    if wait > 0:
        time.sleep(wait)
    _last_request_time = time.monotonic()


def _download_post_sync(url: str, tmpdir: str) -> tuple[list[Path], str]:
    """Download a post/reel. Returns (media_paths, caption)."""
    cl = _get_client()
    shortcode = extract_shortcode(url)
    if not shortcode:
        raise ValueError("Could not extract shortcode from URL.")

    _throttle()
    media = cl.media_info_by_url(url) if hasattr(cl, 'media_info_by_url') else cl.media_info(cl.media_pk_from_code(shortcode))
    caption = media.caption_text or ""

    tmppath = Path(tmpdir)
    paths: list[Path] = []

    # Carousel (multiple items)
    if media.media_type == 8 and media.resources:
        for i, resource in enumerate(media.resources):
            if resource.media_type == 1:   # photo
                p = cl.photo_download(resource.pk, folder=tmpdir)
            else:                           # video
                p = cl.video_download(resource.pk, folder=tmpdir)
            if p:
                paths.append(Path(p))
    elif media.media_type == 1:   # single photo
        p = cl.photo_download(media.pk, folder=tmpdir)
        if p:
            paths.append(Path(p))
    elif media.media_type == 2:   # video / reel
        p = cl.video_download(media.pk, folder=tmpdir)
        if p:
            paths.append(Path(p))
    else:
        raise ValueError(f"Unsupported media type: {media.media_type}")

    return sorted(paths), caption


def _download_story_sync(url: str, tmpdir: str) -> tuple[list[Path], str]:
    """Download a single story item. Returns (media_paths, caption)."""
    cl = _get_client()
    info = extract_story_info(url)
    if not info:
        raise ValueError("Could not parse story URL.")

    username, story_pk = info
    _throttle()

    user_id = cl.user_id_from_username(username)
    stories = cl.user_stories(user_id)
    item = next((s for s in stories if s.pk == story_pk), None)

    if not item:
        raise MediaNotFound("Story not found — it may have expired.")

    if item.media_type == 1:
        p = cl.photo_download_by_url(str(item.thumbnail_url), filename=str(item.pk), folder=tmpdir)
    else:
        p = cl.video_download_by_url(str(item.video_url), filename=str(item.pk), folder=tmpdir)

    return ([Path(p)] if p else []), ""


async def download_instagram(url: str, tmpdir: str) -> tuple[list[Path], str]:
    global _cl

    is_story = "/stories/" in url or "/s/" in url
    fn = _download_story_sync if is_story else _download_post_sync

    try:
        return await asyncio.to_thread(fn, url, tmpdir)

    except (LoginRequired, ChallengeRequired):
        logger.warning("Session expired — attempting re-login")
        async with _cl_lock:
            await asyncio.to_thread(_relogin, _cl or _build_client())
        # Retry once after re-login
        return await asyncio.to_thread(fn, url, tmpdir)


# ── Caption helper ─────────────────────────────────────────────────────────

def trim_caption(caption: str, max_len: int = 1024) -> str:
    caption = caption.strip()
    return caption[: max_len - 1] + "…" if len(caption) > max_len else caption


# ── Send media ─────────────────────────────────────────────────────────────
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv"}
MAX_GROUP = 10


async def send_media(update: Update, media_files: list[Path], caption: str) -> None:
    if not media_files:
        await update.message.reply_text("⚠️ No media found.")
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


# ── Handlers ───────────────────────────────────────────────────────────────

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
    status = await update.message.reply_text("⏳ Downloading… please wait.")

    tmpdir = tempfile.mkdtemp(prefix="instabot_")
    try:
        media_files, caption = await download_instagram(url, tmpdir)
        await status.delete()
        await send_media(update, media_files, caption)

    except PrivateError:
        await status.edit_text("🔒 This content is private — the bot account must follow this user.")

    except MediaNotFound:
        await status.edit_text("❌ Post not found — it may have been deleted or the URL is invalid.")

    except RateLimitError:
        await status.edit_text(
            "⚠️ Instagram rate-limit hit. Please wait a few minutes and try again."
        )

    except (LoginRequired, ChallengeRequired, BadPassword) as exc:
        logger.error("Auth error: %s", exc)
        await status.edit_text(
            "🔒 Instagram login failed or requires verification.\n"
            "Check that <code>IG_USERNAME</code> and <code>IG_PASSWORD</code> are correct in Koyeb.",
            parse_mode="HTML",
        )

    except ValueError as exc:
        await status.edit_text(f"❌ {exc}")

    except Exception:
        logger.exception("Unexpected error for %s", url)
        await status.edit_text("❌ Something went wrong. Please try again later.")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── Health check ───────────────────────────────────────────────────────────

async def _health(_: web.Request) -> web.Response:
    return web.Response(text="OK")


async def _start_health_server() -> None:
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", HEALTH_PORT).start()
    logger.info("Health-check server on port %s", HEALTH_PORT)


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    if not IG_USERNAME or not IG_PASSWORD:
        raise RuntimeError("IG_USERNAME and IG_PASSWORD environment variables must be set.")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def post_init(application: Application) -> None:
        await _start_health_server()
        # Pre-login at startup so first request is instant
        logger.info("Logging in to Instagram as %s…", IG_USERNAME)
        try:
            await get_client()
            logger.info("Instagram login OK ✅")
        except Exception as exc:
            logger.error("Instagram login FAILED: %s — bot will retry on first request", exc)

        await application.bot.set_my_commands([
            ("start", "Welcome message & instructions"),
        ])

    app.post_init = post_init

    logger.info("Bot is running…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
