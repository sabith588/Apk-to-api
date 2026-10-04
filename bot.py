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

# Default Token (Can be updated dynamically via Telegram)
CURRENT_AUTH_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjYXRlZ29yeSI6ImFjY2VzcyIsImRldmljZV9pZCI6Im1vYmlsZS13ZWIiLCJleHBpcnkiOjE3OTEyNTAxMzYsImlhdCI6MTc5MTA3NzMzNiwibG9jYWxlIjoiIiwicGxhdGZvcm0iOiJ3ZWIiLCJyb2xlIjoiTGlzdGVuZXIiLCJ0ZW5hbnQiOiJwb2NrZXRfZm0iLCJ1aWQiOiIiLCJ2ZXJzaW9uIjoidjIifQ.esfnEEDVJFaVzhW7qMZV3YOiE-ATSot2P4TunXuwMvA"

DOWNLOAD_DIR = Path("./downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)


def get_headers() -> dict:
    """Returns headers with the currently active auth-token."""
    return {
        "User-Agent": "Mozilla/5.0 (Linux; Android 11; SM-A505F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
        "Referer": "https://pocketfm.com/",
        "Origin": "https://pocketfm.com",
        "Cookie": f"auth-token={CURRENT_AUTH_TOKEN}",
        "Authorization": f"Bearer {CURRENT_AUTH_TOKEN}",
        "auth-token": CURRENT_AUTH_TOKEN
    }


def extract_show_id(text: str) -> str:
    """Extracts a 32-character show_id from a raw ID or Pocket FM URL."""
    match = re.search(r'([a-f0-9]{32,40})', text, re.IGNORECASE)
    return match.group(1) if match else text.strip()


async def fetch_show_episodes(show_id: str) -> list:
    """Queries Pocket FM API using current auth-token to fetch all episodes."""
    url = f"https://api.pocketfm.com/v2/content_api/show.get_details?show_id={show_id}&info_level=max"
    
    async with httpx.AsyncClient(headers=get_headers(), timeout=15.0) as client:
        response = await client.get(url)
        if response.status_code != 200:
            logger.error(f"API Error {response.status_code}: {response.text}")
            return []
        
        data = response.json()
        logger.info(f"API Response: {data}")

        stories = (
            data.get("result", {}).get("stories") or 
            data.get("show", {}).get("stories") or 
            data.get("stories") or 
            data.get("episodes") or 
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
                ep.get("link")
            )
            
            if ep_id:
                episodes.append({
                    "id": ep_id,
                    "title": title,
                    "stream_url": stream_url
                })
        return episodes


async def download_file(url: str, output_path: Path) -> bool:
    """Downloads stream fragment via multi-threaded yt-dlp."""
    cmd = [
        "yt-dlp",
        "--concurrent-fragments", "16",
        "--add-header", f"Cookie: auth-token={CURRENT_AUTH_TOKEN}",
        "--add-header", f"Authorization: Bearer {CURRENT_AUTH_TOKEN}",
        "--user-agent", get_headers()["User-Agent"],
        "--referer", get_headers()["Referer"],
        "-o", str(output_path),
        url
    ]

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
    """Displays main menu with action buttons."""
    keyboard = [
        [InlineKeyboardButton("🔑 Set New Auth Token", callback_data="btn_set_token")],
        [InlineKeyboardButton("👀 View Active Token Status", callback_data="btn_view_token")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    msg = (
        "🤖 **Pocket FM Downloader Bot**\n\n"
        "Send me any **Show ID** or **Pocket FM Show URL** to extract episodes.\n\n"
        "Use the buttons below to set or update your `auth-token` whenever it expires."
    )
    await update.message.reply_text(msg, reply_markup=reply_markup, parse_mode="Markdown")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles inline keyboard button interactions."""
    query = update.callback_query
    await query.answer()

    if query.data == "btn_set_token":
        context.user_data["waiting_for_token"] = True
        await query.message.reply_text(
            "🔑 **Send your new `auth-token`**\n\n"
            "Paste the extracted `eyJhbGci...` string in your next message.",
            parse_mode="Markdown"
        )
    elif query.data == "btn_view_token":
        masked_token = f"{CURRENT_AUTH_TOKEN[:15]}...{CURRENT_AUTH_TOKEN[-15:]}" if len(CURRENT_AUTH_TOKEN) > 30 else CURRENT_AUTH_TOKEN
        await query.message.reply_text(
            f"ℹ️️ **Current Auth Token:**\n`{masked_token}`",
            parse_mode="Markdown"
        )


async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles text messages (either token update or show request)."""
    global CURRENT_AUTH_TOKEN
    text = update.message.text.strip()

    # Case 1: User is updating token
    if context.user_data.get("waiting_for_token") or text.startswith("eyJ"):
        CURRENT_AUTH_TOKEN = text
        context.user_data["waiting_for_token"] = False
        await update.message.reply_text(
            "✅ **Auth Token Updated Successfully!**\n\nNow send any Show ID or Pocket FM URL to start downloading.",
            parse_mode="Markdown"
        )
        return

    # Case 2: Process Show Download
    show_id = extract_show_id(text)
    status_msg = await update.message.reply_text(f"🔍 Fetching episodes for Show ID: `{show_id}`...", parse_mode="Markdown")

    episodes = await fetch_show_episodes(show_id)

    if not episodes:
        keyboard = [[InlineKeyboardButton("🔑 Update Auth Token", callback_data="btn_set_token")]]
        await status_msg.edit_text(
            "❌ Could not fetch episodes. Check if the Show ID is valid or if your `auth-token` has expired.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    await status_msg.edit_text(f"📦 Found **{len(episodes)}** episodes! Starting sequential download & upload...", parse_mode="Markdown")

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
                await status_msg.edit_text(f"📤 Uploading ({idx}/{len(episodes)}): **{ep_title}** to channel...", parse_mode="Markdown")
                
                with open(file_path, "rb") as video_file:
                    await context.bot.send_video(
                        chat_id=TARGET_CHANNEL_ID,
                        video=video_file,
                        caption=f"🎧 **{ep_title}**\n🆔 Episode ID: `{ep_id}`\n📺 Show ID: `{show_id}`",
                        parse_mode="Markdown"
                    )
                success_count += 1
            else:
                logger.warning(f"Failed to download episode {ep_id}")

        except Exception as e:
            logger.error(f"Error processing episode {ep_id}: {e}")

        finally:
            if file_path.exists():
                file_path.unlink()

    await status_msg.edit_text(f"🎉 Process Complete! Uploaded **{success_count}/{len(episodes)}** episodes to your channel.")


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))

    logger.info("Bot starting up...")
    app.run_polling()


if __name__ == "__main__":
    main()
