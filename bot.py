import re
import logging
import asyncio
import subprocess
from pathlib import Path
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Logging Setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# CONFIGURATION
BOT_TOKEN = "8827979888:AAGXJJsYhKHcVEGK-aCgJH0RqQxVtJb8Us8"
TARGET_CHANNEL_ID = "-1004291729847"

# Default Cookie / Auth Token Storage
CURRENT_COOKIE = ""

DOWNLOAD_DIR = Path("./downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)


def extract_auth_token(cookie_str: str) -> str:
    """Extracts the auth-token JWT string from a raw cookie string if present."""
    match = re.search(r'auth-token=([^;]+)', cookie_str)
    if match:
        return match.group(1).strip()
    # If the user pasted just the token string itself
    if cookie_str.startswith("eyJ"):
        return cookie_str.strip()
    return ""


def get_headers() -> dict:
    """Generates standard browser headers with session cookies and tokens."""
    cookie = CURRENT_COOKIE.strip()
    token = extract_auth_token(cookie)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://pocketfm.com/",
        "Origin": "https://pocketfm.com",
        "client-version": "100.0.0",
        "platform": "web"
    }

    if cookie:
        headers["Cookie"] = cookie
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["auth-token"] = token

    return headers


def extract_show_id(text: str) -> str:
    """Extracts a 32-to-40-character show_id from a raw ID or Pocket FM URL."""
    match = re.search(r'([a-f0-9]{32,40})', text, re.IGNORECASE)
    return match.group(1) if match else text.strip()


async def fetch_show_episodes(show_id: str) -> list:
    """Queries Pocket FM API using current session headers to retrieve episodes."""
    url = f"https://api.pocketfm.com/v2/content_api/show.get_details?show_id={show_id}&info_level=max"
    
    async with httpx.AsyncClient(headers=get_headers(), timeout=20.0, follow_redirects=True) as client:
        try:
            response = await client.get(url)
            logger.info(f"API Response Code: {response.status_code}")
            
            if response.status_code != 200:
                logger.error(f"API Failed: {response.text}")
                return []

            data = response.json()
            
            # Look through possible JSON keys where Pocket FM stores episode details
            result = data.get("result", {}) or data.get("data", {}) or data
            stories = (
                result.get("stories") or 
                result.get("episodes") or 
                result.get("show", {}).get("stories") or 
                []
            )

            episodes = []
            for ep in stories:
                ep_id = ep.get("id") or ep.get("story_id") or ep.get("episode_id")
                title = ep.get("title") or ep.get("name") or f"Episode {ep_id}"
                stream_url = (
                    ep.get("media_url") or 
                    ep.get("stream_url") or 
                    ep.get("download_url") or 
                    ep.get("link") or
                    ep.get("audio_url")
                )

                if ep_id:
                    episodes.append({
                        "id": ep_id,
                        "title": title,
                        "stream_url": stream_url
                    })

            return episodes
        except Exception as e:
            logger.error(f"Exception during API fetch: {e}")
            return []


async def download_file(url: str, output_path: Path) -> bool:
    """Downloads audio/video stream passing cookies to yt-dlp."""
    headers = get_headers()
    cmd = [
        "yt-dlp",
        "--concurrent-fragments", "16",
        "--user-agent", headers["User-Agent"],
        "--referer", "https://pocketfm.com/",
        "-o", str(output_path),
        url
    ]

    if CURRENT_COOKIE:
        cmd.extend(["--add-header", f"Cookie: {CURRENT_COOKIE.strip()}"])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    
    if proc.returncode == 0:
        return True
    logger.error(f"yt-dlp error: {stderr.decode()}")
    return False


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays main menu with inline buttons."""
    keyboard = [
        [InlineKeyboardButton("🔑 Set New Cookie / Token", callback_data="btn_set_cookie")],
        [InlineKeyboardButton("👀 View Active Session", callback_data="btn_view_cookie")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    msg = (
        "🤖 **Pocket FM Downloader Bot**\n\n"
        "Send me any **Show ID** or **Pocket FM Show URL** to fetch episodes.\n\n"
        "Use the buttons below to update your session token or cookie."
    )
    await update.message.reply_text(msg, reply_markup=reply_markup, parse_mode="Markdown")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles inline button clicks."""
    query = update.callback_query
    await query.answer()

    if query.data == "btn_set_cookie":
        context.user_data["waiting_for_cookie"] = True
        await query.message.reply_text(
            "🔑 **Send your fresh Cookie or Auth-Token:**\n\n"
            "Paste your complete raw cookie string or `eyJ...` token in your next reply.",
            parse_mode="Markdown"
        )
    elif query.data == "btn_view_cookie":
        if not CURRENT_COOKIE:
            await query.message.reply_text("⚠️ No active cookie set yet.")
            return
        token = extract_auth_token(CURRENT_COOKIE)
        masked = f"{token[:12]}...{token[-12:]}" if len(token) > 24 else token
        await query.message.reply_text(
            f"ℹ️ **Current Auth Token:**\n`{masked}`",
            parse_mode="Markdown"
        )


async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes messages for cookie updates, episode lists, or show URLs."""
    global CURRENT_COOKIE
    text = update.message.text.strip()

    # 1. Update Cookie or Auth Token
    if context.user_data.get("waiting_for_cookie") or text.startswith("eyJ") or "auth-token=" in text:
        CURRENT_COOKIE = text
        context.user_data["waiting_for_cookie"] = False
        await update.message.reply_text(
            "✅ **Session Cookie / Auth Token Updated Successfully!**\n\nNow send your Show ID or URL.",
            parse_mode="Markdown"
        )
        return

    # 2. Check for bulk episode hashes list (if text contains [EPISODE])
    episode_matches = re.findall(r'([a-f0-9]{32,40})', text, re.IGNORECASE)
    if len(episode_matches) > 1 and "[EPISODE]" in text:
        status_msg = await update.message.reply_text(f"🔍 Found **{len(episode_matches)}** episode hashes. Starting download sequence...", parse_mode="Markdown")
        
        success_count = 0
        for idx, ep_id in enumerate(episode_matches, start=1):
            stream_url = f"https://pocketfm.com/episode/{ep_id}"
            file_path = DOWNLOAD_DIR / f"{ep_id}.mp4"

            try:
                await status_msg.edit_text(f"⏳ Downloading ({idx}/{len(episode_matches)}): Episode `{ep_id[:8]}...`", parse_mode="Markdown")
                downloaded = await download_file(stream_url, file_path)

                if downloaded and file_path.exists():
                    await status_msg.edit_text(f"📤 Uploading ({idx}/{len(episode_matches)})...", parse_mode="Markdown")
                    with open(file_path, "rb") as video_file:
                        await context.bot.send_video(
                            chat_id=TARGET_CHANNEL_ID,
                            video=video_file,
                            caption=f"🎧 **Episode**\n🆔 Episode ID: `{ep_id}`",
                            parse_mode="Markdown"
                        )
                    success_count += 1
            except Exception as e:
                logger.error(f"Error processing {ep_id}: {e}")
            finally:
                if file_path.exists():
                    file_path.unlink()

        await status_msg.edit_text(f"🎉 Complete! Uploaded **{success_count}/{len(episode_matches)}** episodes.")
        return

    # 3. Handle Show ID or Pocket FM URL
    show_id = extract_show_id(text)
    status_msg = await update.message.reply_text(f"🔍 Fetching episodes for Show ID: `{show_id}`...", parse_mode="Markdown")

    episodes = await fetch_show_episodes(show_id)

    if not episodes:
        keyboard = [[InlineKeyboardButton("🔑 Update Auth Token", callback_data="btn_set_cookie")]]
        await status_msg.edit_text(
            "❌ **Could not fetch episodes.**\n\n"
            "Check if the Show ID is valid or if your `auth-token` has expired.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    await status_msg.edit_text(f"📦 Found **{len(episodes)}** episodes! Starting download & upload process...", parse_mode="Markdown")

    success_count = 0
    for idx, ep in enumerate(episodes, start=1):
        ep_id = ep["id"]
        ep_title = ep["title"]
        stream_url = ep.get("stream_url") or f"https://pocketfm.com/episode/{ep_id}"
        file_path = DOWNLOAD_DIR / f"{show_id}_{ep_id}.mp4"

        try:
            await status_msg.edit_text(f"⏳ Downloading ({idx}/{len(episodes)}): **{ep_title}**...", parse_mode="Markdown")
            
            downloaded = await download_file(stream_url, file_path)

            if downloaded and file_path.exists():
                await status_msg.edit_text(f"📤 Uploading ({idx}/{len(episodes)}): **{ep_title}**...", parse_mode="Markdown")
                
                with open(file_path, "rb") as video_file:
                    await context.bot.send_video(
                        chat_id=TARGET_CHANNEL_ID,
                        video=video_file,
                        caption=f"🎧 **{ep_title}**\n🆔 Episode ID: `{ep_id}`\n📺 Show ID: `{show_id}`",
                        parse_mode="Markdown"
                    )
                success_count += 1
            else:
                logger.warning(f"Download failed for episode {ep_id}")

        except Exception as e:
            logger.error(f"Error processing episode {ep_id}: {e}")

        finally:
            if file_path.exists():
                file_path.unlink()

    await status_msg.edit_text(f"🎉 Complete! Uploaded **{success_count}/{len(episodes)}** episodes to target channel.")


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))

    logger.info("Bot starting up...")
    app.run_polling()


if __name__ == "__main__":
    main()
