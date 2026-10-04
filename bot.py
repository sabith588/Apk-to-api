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

# Global Session State
CURRENT_COOKIE = ""
AUTH_TOKEN = ""

DOWNLOAD_DIR = Path("./downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Pocket FM Web API Endpoints
API_BASE = "https://api.pocketfm.com"


def get_headers() -> dict:
    """Generates standard browser headers with active session tokens."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://pocketfm.com/",
        "Origin": "https://pocketfm.com",
        "client-version": "100.0.0",
        "platform": "web"
    }

    if CURRENT_COOKIE:
        headers["Cookie"] = CURRENT_COOKIE
    if AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {AUTH_TOKEN}"
        headers["auth-token"] = AUTH_TOKEN

    return headers


def extract_show_id(text: str) -> str:
    """Extracts a 32-to-40-character show_id from a raw ID or Pocket FM URL."""
    match = re.search(r'([a-f0-9]{32,40})', text, re.IGNORECASE)
    return match.group(1) if match else text.strip()


async def send_otp_request(mobile_number: str) -> dict:
    """Requests OTP from Pocket FM for the provided mobile number."""
    url = f"{API_BASE}/v2/user_api/send_otp"
    payload = {
        "phone_number": mobile_number,
        "country_code": "+91",  # Change country code if needed
        "platform": "web"
    }
    
    async with httpx.AsyncClient(headers=get_headers(), timeout=15.0) as client:
        try:
            res = await client.post(url, json=payload)
            return res.json()
        except Exception as e:
            logger.error(f"Error requesting OTP: {e}")
            return {"status": "error", "message": str(e)}


async def verify_otp_request(mobile_number: str, otp: str) -> dict:
    """Verifies OTP with Pocket FM and retrieves a new auth token."""
    url = f"{API_BASE}/v2/user_api/verify_otp"
    payload = {
        "phone_number": mobile_number,
        "country_code": "+91",
        "otp": otp,
        "platform": "web"
    }

    async with httpx.AsyncClient(headers=get_headers(), timeout=15.0) as client:
        try:
            res = await client.post(url, json=payload)
            data = res.json()
            
            # Capture cookies returned in headers
            cookies = res.cookies
            cookie_str = "; ".join([f"{k}={v}" for k, v in cookies.items()])
            
            return {"data": data, "cookie_str": cookie_str}
        except Exception as e:
            logger.error(f"Error verifying OTP: {e}")
            return {"data": {"status": "error"}, "cookie_str": ""}


async def fetch_show_episodes(show_id: str) -> list:
    """Queries Pocket FM API using active authentication headers to retrieve episodes."""
    url = f"{API_BASE}/v2/content_api/show.get_details?show_id={show_id}&info_level=max"
    
    async with httpx.AsyncClient(headers=get_headers(), timeout=20.0, follow_redirects=True) as client:
        try:
            response = await client.get(url)
            logger.info(f"API Response Code: {response.status_code}")
            
            if response.status_code != 200:
                logger.error(f"API Failed: {response.text}")
                return []

            data = response.json()
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
    """Downloads audio/video stream passing auth headers to yt-dlp."""
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
        cmd.extend(["--add-header", f"Cookie: {CURRENT_COOKIE}"])
    if AUTH_TOKEN:
        cmd.extend(["--add-header", f"Authorization: Bearer {AUTH_TOKEN}"])

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
    """Displays main menu."""
    keyboard = [
        [InlineKeyboardButton("📱 Login with Mobile Number", callback_data="btn_login_mobile")],
        [InlineKeyboardButton("🔑 Manual Cookie / Token Entry", callback_data="btn_set_cookie")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    msg = (
        "🤖 **Pocket FM Downloader Bot**\n\n"
        "Send me a **Show ID** or **Pocket FM Link** to download episodes.\n\n"
        "Click **Login with Mobile Number** below to authenticate directly via OTP."
    )
    await update.message.reply_text(msg, reply_markup=reply_markup, parse_mode="Markdown")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles button interactions."""
    query = update.callback_query
    await query.answer()

    if query.data == "btn_login_mobile":
        context.user_data["state"] = "AWAITING_PHONE"
        await query.message.reply_text(
            "📱 **Mobile Login Procedure**\n\n"
            "Please send your 10-digit mobile number (e.g., `9876543210`).",
            parse_mode="Markdown"
        )

    elif query.data == "btn_set_cookie":
        context.user_data["state"] = "AWAITING_COOKIE"
        await query.message.reply_text(
            "🔑 Send your raw `Cookie` header or `auth-token` string in your next message."
        )


async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes dynamic text input based on user conversation state."""
    global CURRENT_COOKIE, AUTH_TOKEN
    text = update.message.text.strip()
    user_state = context.user_data.get("state")

    # 1. Phone Number State
    if user_state == "AWAITING_PHONE":
        phone = re.sub(r'\D', '', text)
        if len(phone) < 10:
            await update.message.reply_text("❌ Invalid mobile number. Please send a valid 10-digit number.")
            return

        context.user_data["phone"] = phone
        status_msg = await update.message.reply_text(f"⏳ Requesting OTP for `+91{phone}`...", parse_mode="Markdown")
        
        response = await send_otp_request(phone)
        
        if response.get("status") in [200, "success", True] or response.get("code") == 200:
            context.user_data["state"] = "AWAITING_OTP"
            await status_msg.edit_text(
                f"📩 **OTP Sent to +91{phone}!**\n\n"
                "Please reply with the 4-digit or 6-digit OTP code you received via SMS.",
                parse_mode="Markdown"
            )
        else:
            err_msg = response.get("message", "Failed to send OTP.")
            context.user_data["state"] = None
            await status_msg.edit_text(f"❌ **OTP Request Failed:** {err_msg}")
        return

    # 2. OTP Verification State
    if user_state == "AWAITING_OTP":
        otp = re.sub(r'\D', '', text)
        phone = context.user_data.get("phone")

        status_msg = await update.message.reply_text("⏳ Verifying OTP with Pocket FM...", parse_mode="Markdown")
        res_data = await verify_otp_request(phone, otp)
        
        data = res_data.get("data", {})
        cookie_str = res_data.get("cookie_str", "")

        token = data.get("token") or data.get("result", {}).get("token") or data.get("auth_token")

        if token or cookie_str:
            if token:
                AUTH_TOKEN = token
            if cookie_str:
                CURRENT_COOKIE = cookie_str
            else:
                CURRENT_COOKIE = f"auth-token={AUTH_TOKEN}; platform=web;"

            context.user_data["state"] = None
            await status_msg.edit_text(
                "✅ **Login Successful!**\n\n"
                "Your authentication session is active. Send any **Show ID** or **Pocket FM URL** to fetch episodes.",
                parse_mode="Markdown"
            )
        else:
            await status_msg.edit_text("❌ **Invalid OTP.** Please try logging in again via /start.")
            context.user_data["state"] = None
        return

    # 3. Manual Cookie State
    if user_state == "AWAITING_COOKIE" or text.startswith("eyJ") or "auth-token=" in text:
        CURRENT_COOKIE = text
        token_match = re.search(r'auth-token=([^;]+)', text)
        if token_match:
            AUTH_TOKEN = token_match.group(1)
        elif text.startswith("eyJ"):
            AUTH_TOKEN = text

        context.user_data["state"] = None
        await update.message.reply_text("✅ Session updated! Send a Show ID or URL to proceed.")
        return

    # 4. Fetch Show ID or URL
    show_id = extract_show_id(text)
    status_msg = await update.message.reply_text(f"🔍 Fetching episodes for Show ID: `{show_id}`...", parse_mode="Markdown")

    episodes = await fetch_show_episodes(show_id)

    if not episodes:
        keyboard = [[InlineKeyboardButton("📱 Login via Mobile OTP", callback_data="btn_login_mobile")]]
        await status_msg.edit_text(
            "❌ **Could not fetch episodes.**\n\n"
            "Your session token may be missing or expired. Click below to log in.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    await status_msg.edit_text(f"📦 Found **{len(episodes)}** episodes! Downloading & uploading...", parse_mode="Markdown")

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

        except Exception as e:
            logger.error(f"Error processing episode {ep_id}: {e}")

        finally:
            if file_path.exists():
                file_path.unlink()

    await status_msg.edit_text(f"🎉 Complete! Uploaded **{success_count}/{len(episodes)}** episodes.")


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))

    logger.info("Bot starting up...")
    app.run_polling()


if __name__ == "__main__":
    main()
