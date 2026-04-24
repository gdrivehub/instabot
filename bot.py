import os
import re
import logging
import asyncio
import tempfile
import shutil
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
import instaloader

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]

# Optional: provide Instagram credentials to access private content / avoid
# rate-limits.  Leave blank to use anonymous access.
IG_USERNAME = os.getenv("IG_USERNAME", "")
IG_PASSWORD = os.getenv("IG_PASSWORD", "")

# ── Instaloader setup ──────────────────────────────────────────────────────
L = instaloader.Instaloader(
    download_videos=True,
    download_video_thumbnails=False,
    download_geotags=False,
    download_comments=False,
    save_metadata=False,
    compress_json=False,
    post_metadata_txt_pattern="",   # don't write caption files
    filename_pattern="{shortcode}_{typename}",
    quiet=True,
)

if IG_USERNAME and IG_PASSWORD:
    try:
        L.login(IG_USERNAME, IG_PASSWORD)
        logger.info("Logged in to Instagram as %s", IG_USERNAME)
    except Exception as exc:
        logger.warning("Instagram login failed: %s", exc)

# ── Helpers ─────────────────────────────────────────────────────────────────

INSTAGRAM_URL_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/"
    r"(?:p|reel|reels|stories|s)/[\w\-]+/?",
    re.IGNORECASE,
)


def extract_shortcode(url: str) -> str | None:
    """Pull the shortcode from a post / reel URL."""
    m = re.search(r"/(?:p|reel|reels)/([\w\-]+)", url)
    return m.group(1) if m else None


def extract_story_info(url: str) -> tuple[str, str] | None:
    """Return (username, story_id) from a stories URL."""
    m = re.search(r"/stories/([\w\.]+)/(\d+)", url)
    return (m.group(1), m.group(2)) if m else None


def is_instagram_url(text: str) -> bool:
    return bool(INSTAGRAM_URL_RE.search(text))


def caption_text(caption: str | None, max_len: int = 1024) -> str:
    if not caption:
        return ""
    caption = caption.strip()
    if len(caption) > max_len:
        caption = caption[: max_len - 3] + "…"
    return caption


# ── Download logic ──────────────────────────────────────────────────────────

async def download_post(url: str, tmpdir: str) -> tuple[list[Path], str]:
    """
    Download a post or reel.
    Returns (list_of_media_paths, caption).
    """
    shortcode = extract_shortcode(url)
    if not shortcode:
        raise ValueError("Could not extract shortcode from URL.")

    post = await asyncio.to_thread(instaloader.Post.from_shortcode, L.context, shortcode)
    await asyncio.to_thread(L.download_post, post, target=tmpdir)

    # Collect downloaded media
    media_files: list[Path] = sorted(
        [
            p
            for p in Path(tmpdir).iterdir()
            if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".mp4", ".mov"}
        ]
    )
    cap = caption_text(post.caption)
    return media_files, cap


async def download_story(url: str, tmpdir: str) -> tuple[list[Path], str]:
    """
    Download a single story item.
    Returns (list_of_media_paths, caption).
    """
    info = extract_story_info(url)
    if not info:
        raise ValueError("Could not parse story URL.")

    username, story_id_str = info
    story_id = int(story_id_str)

    profile = await asyncio.to_thread(instaloader.Profile.from_username, L.context, username)
    stories = await asyncio.to_thread(L.get_stories, [profile.userid])

    found_item = None
    async for story in _async_iter(stories):
        for item in story.get_items():
            if item.mediaid == story_id:
                found_item = item
                break
        if found_item:
            break

    if not found_item:
        raise ValueError("Story not found — it may have expired or be private.")

    await asyncio.to_thread(L.download_storyitem, found_item, target=tmpdir)

    media_files: list[Path] = sorted(
        [
            p
            for p in Path(tmpdir).iterdir()
            if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".mp4", ".mov"}
        ]
    )
    return media_files, ""   # stories don't have captions


async def _async_iter(sync_iterable):
    """Wrap a sync iterable for async for."""
    for item in sync_iterable:
        yield item


# ── Send helpers ────────────────────────────────────────────────────────────

MAX_MEDIA_GROUP = 10   # Telegram limit


async def send_media(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    media_files: list[Path],
    caption: str,
) -> None:
    if not media_files:
        await update.message.reply_text("⚠️ No media found in this post.")
        return

    # Single file
    if len(media_files) == 1:
        path = media_files[0]
        with open(path, "rb") as f:
            if path.suffix.lower() in {".mp4", ".mov"}:
                await update.message.reply_video(
                    video=f,
                    caption=caption or None,
                    supports_streaming=True,
                )
            else:
                await update.message.reply_photo(photo=f, caption=caption or None)
        return

    # Multiple files → media group(s)
    chunks = [
        media_files[i : i + MAX_MEDIA_GROUP]
        for i in range(0, len(media_files), MAX_MEDIA_GROUP)
    ]
    for idx, chunk in enumerate(chunks):
        media_group = []
        file_handles = []
        for i, path in enumerate(chunk):
            fh = open(path, "rb")
            file_handles.append(fh)
            cap = (caption if idx == 0 and i == 0 else None) or None
            if path.suffix.lower() in {".mp4", ".mov"}:
                media_group.append(InputMediaVideo(media=fh, caption=cap))
            else:
                media_group.append(InputMediaPhoto(media=fh, caption=cap))

        try:
            await update.message.reply_media_group(media=media_group)
        finally:
            for fh in file_handles:
                fh.close()


# ── Command & message handlers ──────────────────────────────────────────────

START_MSG = """
👋 *Welcome to InstaGrab Bot!*

Send me any Instagram link and I'll download it for you:

• 📸 Posts (photos & carousels)
• 🎬 Reels
• 📖 Stories

Just paste the URL and hit send!
"""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(START_MSG, parse_mode=ParseMode.MARKDOWN)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""

    if not is_instagram_url(text):
        await update.message.reply_text(
            "🔗 Please send a valid Instagram URL (post, reel, or story)."
        )
        return

    url = INSTAGRAM_URL_RE.search(text).group(0)
    status_msg = await update.message.reply_text("⏳ Downloading… please wait.")

    tmpdir = tempfile.mkdtemp(prefix="instabot_")
    try:
        is_story = "/stories/" in url or "/s/" in url

        if is_story:
            media_files, cap = await download_story(url, tmpdir)
        else:
            media_files, cap = await download_post(url, tmpdir)

        await status_msg.delete()
        await send_media(update, context, media_files, cap)

    except instaloader.exceptions.LoginRequiredException:
        await status_msg.edit_text(
            "🔒 This content is private. The bot needs Instagram credentials to access it."
        )
    except instaloader.exceptions.BadResponseException as exc:
        logger.error("Bad response from Instagram: %s", exc)
        await status_msg.edit_text(
            "❌ Instagram returned an error. The post may have been deleted or is unavailable."
        )
    except ValueError as exc:
        await status_msg.edit_text(f"❌ {exc}")
    except Exception as exc:
        logger.exception("Unexpected error for URL %s", url)
        await status_msg.edit_text(
            "❌ Something went wrong while downloading. Please try again later."
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot is running…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
