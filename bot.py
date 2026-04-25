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
BOT_TOKEN        = os.environ["BOT_TOKEN"]
HEALTH_PORT      = int(os.getenv("PORT", "8000"))
SESSION_FILE     = "/tmp/ig_session.json"

# IG_SESSION_JSON env var holds the full contents of session.json
IG_SESSION_JSON  = os.getenv("IG_SESSION_JSON", "")

# Optional fallback: plain sessionid cookie value
IG_SESSION_ID    = os.getenv("IG_SESSION_ID", "")
IG_USERNAME      = os.getenv("IG_USERNAME", "")

# ── Write session to disk at startup ──────────────────────────────────────
if IG_SESSION_JSON:
    try:
        # Validate it's proper JSON before writing
        parsed = json.loads(IG_SESSION_JSON)
        with open(SESSION_FILE, "w") as f:
            json.dump(parsed, f)
        logger.info("Session JSON written to %s", SESSION_FILE)
    except json.JSONDecodeError as e:
        logger.error("IG_SESSION_JSON is not valid JSON: %s", e)
        raise RuntimeError("IG_SESSION_JSON env var contains invalid JSON. Check the value in Koyeb.") from e

# ── Instagram client ───────────────────────────────────────────────────────
_cl: Client | None = None
_cl_lock = asyncio.Lock()
_last_request_time: float = 0.0
MIN_DELAY = 3.0


def _build_client() -> Client:
    cl = Client()
    cl.delay_range = [2, 5]
    return cl


def _init_client_sync() -> Client:
    """Load session from file or sessionid. Raises on failure."""
    cl = _build_client()

    if os.path.isfile(SESSION_FILE):
        try:
            cl.load_settings(SESSION_FILE)
            # Verify session is still alive with a lightweight call
            cl.get_timeline_feed()
            logger.info("Instagram session loaded and verified ✅")
            return cl
        except Exception as exc:
            logger.warning("Saved session invalid or expired: %s", exc)

    # Fallback: plain sessionid cookie
    if IG_SESSION_ID:
        try:
            cl2 = _build_client()
            cl2.login_by_sessionid(IG_SESSION_ID)
            cl2.dump_settings(SESSION_FILE)
            logger.info("Logged in via sessionid cookie ✅")
            return cl2
        except Exception as exc:
            logger.error("sessionid login failed: %s", exc)

    raise RuntimeError(
        "Could not authenticate with Instagram.\n"
        "Make sure IG_SESSION_JSON (or IG_SESSION_ID) is set correctly in Koyeb env vars."
    )


async def get_client() -> Client:
    global _cl
    async with _cl_lock:
        if _cl is None:
            _cl = await asyncio.to_thread(_init_client_sync)
        return _cl


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


# ── Throttle ───────────────────────────────────────────────────────────────

def _throttle():
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    wait = max(0.0, MIN_DELAY + random.uniform(0, 2) - elapsed)
    if wait > 0:
        time.sleep(wait)
    _last_request_time = time.monotonic()


# ── Download logic ─────────────────────────────────────────────────────────

def _download_post_sync(cl: Client, url: str, tmpdir: str) -> tuple[list[Path], str]:
    shortcode = extract_shortcode(url)
    if not shortcode:
        raise ValueError("Could not extract shortcode from URL.")

    _throttle()
    pk = cl.media_pk_from_code(shortcode)
    media = cl.media_info(pk)
    caption = media.caption_text or ""

    paths: list[Path] = []

    if media.media_type == 8 and media.resources:
        # Carousel
        for resource in media.resources:
            if resource.media_type == 1:
                p = cl.photo_download(resource.pk, folder=tmpdir)
            else:
                p = cl.video_download(resource.pk, folder=tmpdir)
            if p:
                paths.append(Path(p))
    elif media.media_type == 1:
        # Single photo
        p = cl.photo_download(media.pk, folder=tmpdir)
        if p:
            paths.append(Path(p))
    elif media.media_type == 2:
        # Video / Reel
        p = cl.video_download(media.pk, folder=tmpdir)
        if p:
            paths.append(Path(p))
    else:
        raise ValueError(f"Unsupported media type: {media.media_type}")

    return sorted(paths), caption


def _download_story_sync(cl: Client, url: str, tmpdir: str) -> tuple[list[Path], str]:
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
    cl = await get_client()
    is_story = "/stories/" in url or "/s/" in url
    fn = _download_story_sync if is_story else _download_post_sync

    try:
        return await asyncio.to_thread(fn, cl, url, tmpdir)
    except (LoginRequired, ChallengeRequired):
        # Session expired mid-run — try reloading from env
        logger.warning("Session expired mid-run — reinitialising client")
        async with _cl_lock:
            _cl = None
        cl = await get_client()
        return await asyncio.to_thread(fn, cl, url, tmpdir)


# ── Caption / media helpers ────────────────────────────────────────────────

def trim_caption(caption: str, max_len: int = 1024) -> str:
    caption = caption.strip()
    return caption[: max_len - 1] + "…" if len(caption) > max_len else caption


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
    status = await update.message.reply_text("⏳ Downloading… please wait.")

    tmpdir = tempfile.mkdtemp(prefix="instabot_")
    try:
        media_files, caption = await download_instagram(url, tmpdir)
        await status.delete()
        await send_media(update, media_files, caption)

    except PrivateError:
        await status.edit_text(
            "🔒 This content is private.\n"
            "The bot's Instagram account needs to follow this user to download their content."
        )
    except MediaNotFound:
        await status.edit_text(
            "❌ Post not found — it may have been deleted or the URL is invalid."
        )
    except RateLimitError:
        await status.edit_text(
            "⚠️ Instagram rate-limit hit. Please wait a few minutes and try again."
        )
    except (LoginRequired, ChallengeRequired, BadPassword) as exc:
        logger.error("Auth error: %s", exc)
        await status.edit_text(
            "🔒 Instagram session expired or requires verification.\n\n"
            "Generate a new <code>session.json</code> from your PC and update "
            "<code>IG_SESSION_JSON</code> in Koyeb env vars.",
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
    if not IG_SESSION_JSON and not IG_SESSION_ID:
        raise RuntimeError(
            "No Instagram session configured.\n"
            "Set IG_SESSION_JSON (contents of session.json) "
            "or IG_SESSION_ID (sessionid cookie value) in Koyeb env vars."
        )

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def post_init(application: Application) -> None:
        await _start_health_server()
        logger.info("Verifying Instagram session…")
        try:
            await get_client()
            logger.info("Instagram session OK ✅")
        except Exception as exc:
            logger.error("Instagram session failed: %s", exc)
            raise

        await application.bot.set_my_commands([
            ("start", "Welcome message & instructions"),
        ])

    app.post_init = post_init

    logger.info("Bot is running…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
