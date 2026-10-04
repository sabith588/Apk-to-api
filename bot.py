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
from playwright.async_api import async_playwright

# Logging Setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# CONFIGURATION
BOT_TOKEN = "8827979888:AAGXJJsYhKHcVEGK-aCgJH0RqQxVtJb8Us8"
TARGET_CHANNEL_ID = "-1004291729847"

# Session Storage
CURRENT_COOKIE = ""
AUTH_TOKEN = ""

DOWNLOAD_DIR = Path("./downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)


def extract_token(text: str) -> str:
    """Extracts JWT token string from cookies or raw token input."""
    match = re.search(r'auth-token=([^;]+)', text)
    if match:
        return match.group(1).strip()
    if text.strip().startswith("eyJ"):
        return text.strip()
    return ""


def get_headers() -> dict:
    """Builds HTTP headers using active cookies/tokens."""
    token = extract_token(CURRENT_COOKIE) or AUTH_TOKEN
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
        headers["Cookie"] = CURRENT_COOKIE.strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["auth-token"] = token

    return headers


def extract_show_id(text: str) -> str:
    """Extracts show ID from URL or raw input."""
    match = re.search(r'([a-f0-9]{32,40})', text, re.IGNORECASE)
    return match.group(1) if match else text.strip()


async def automate_phone_login(phone_number: str, user_data: dict) -> bool:
    """Navigates to pocketfm.com/login, clicks 'CONTINUE WITH PHONE', fills the phone number, and clicks 'SEND OTP'."""
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox']
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        logger.info("Opening https://pocketfm.com/login")
        await page.goto("https://pocketfm.com/login", wait_until="domcontentloaded", timeout=30000)

        # Step 1: Click 'CONTINUE WITH PHONE'
        phone_btn = page.locator("text='CONTINUE WITH PHONE'")[span_0](start_span)[span_0](end_span)
        await phone_btn.click()
        await page.wait_for_timeout(1000)

        # Step 2: Type phone number in input field
        phone_input = page.locator("input[type='tel'], input[type='text'], input")
        await phone_input.first.fill(phone_number)
        await page.wait_for_timeout(500)

        # Step 3: Click 'SEND OTP'
        send_otp_btn = page.locator("text='SEND OTP'")[span_1](start_span)[span_1](end_span)
        await send_otp_btn.click()

        # Step 4: Wait for OTP input page/field to load
        await page.wait_for_selector("input", timeout=15000)

        # Preserve state for Telegram OTP submission
        user_data["pw"] = pw
        user_data["browser"] = browser
        user_data["page"] = page
        return True

    except Exception as e:
        logger.error(f"Playwright Login Error: {e}")
        if 'browser' in locals():
            await browser.close()
        if 'pw' in locals():
            await pw.stop()
        return False


async def automate_otp_submission(otp_code: str, user_data: dict) -> bool:
    """Submits OTP on the active page, clicks login, and captures updated session cookies and tokens."""
    global CURRENT_COOKIE, AUTH_TOKEN
    try:
        page = user_data.get("page")
        browser = user_data.get("browser")
        pw = user_data.get("pw")

        if not page:
            return False

        # Fill OTP digit fields
        otp_inputs = page.locator("input[type='text'], input[type='number'], input[type='tel'], input")
        count = await otp_inputs.count()

        if count == 1:
            await otp_inputs.first.fill(otp_code)
        elif count > 1:
            for idx, char in enumerate(otp_code[:count]):
                await otp_inputs.nth(idx).fill(char)

        await page.wait_for_timeout(500)

        # Click Login / Verify / Submit button
        submit_btn = page.locator("button:has-text('VERIFY'), button:has-text('LOGIN'), button:has-text('SUBMIT'), button[type='submit']")
        if await submit_btn.count() > 0:
            await submit_btn.first.click()

        await page.wait_for_timeout(5000)

        # Extract Cookies
        cookies = await page.context.cookies()
        cookie_pairs = [f"{c['name']}={c['value']}" for c in cookies]
        CURRENT_COOKIE = "; ".join(cookie_pairs)

        for c in cookies:
            if c['name'] == 'auth-token':
                AUTH_TOKEN = c['value']

        if not AUTH_TOKEN:
            try:
                AUTH_TOKEN = await page.evaluate("() => localStorage.getItem('auth-token') || sessionStorage.getItem('auth-token')")
            except Exception:
                pass

        await browser.close()
        await pw.stop()

        user_data.pop("pw", None)
        user_data.pop("browser", None)
        user_data.pop("page", None)

        return bool(CURRENT_COOKIE or AUTH_TOKEN)

    except Exception as e:
        logger.error(f"OTP Submission Error: {e}")
        return False


async def fetch_show_episodes(show_id: str) -> list:
    """Queries Pocket FM API using active headers to retrieve episodes."""
    url = f"https://api.pocketfm.com/v2/content_api/show.get_details?show_id={show_id}&info_level=max"

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
    """Downloads audio stream passing cookies to yt-dlp."""
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
    keyboard = [
        [InlineKeyboardButton("🌐 Automated Web Login", callback_data="btn_web_login")],
        [InlineKeyboardButton("🔑 Manual Cookie Entry", callback_data="btn_set_cookie")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    msg = (
        "🤖 **Pocket FM Downloader Bot**\n\n"
        "Send me a **Show ID** or **Pocket FM Link** to fetch episodes.\n\n"
        "Tap **Automated Web Login** to log into pocketfm.com automatically."
    )
    await update.message.reply_text(msg, reply_markup=reply_markup, parse_mode="Markdown")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "btn_web_login":
        context.user_data["state"] = "AWAITING_PHONE"
        await query.message.reply_text(
            "📱 **Browser Login Automation**\n\n"
            "Please reply with your 10-digit phone number (e.g. `9876543210`).",
            parse_mode="Markdown"
        )
    elif query.data == "btn_set_cookie":
        context.user_data["state"] = "AWAITING_COOKIE"
        await query.message.reply_text("🔑 Send your raw `Cookie` header or `auth-token` string.")


async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CURRENT_COOKIE
    text = update.message.text.strip()
    user_state = context.user_data.get("state")

    # Step 1: Handle Phone Input
    if user_state == "AWAITING_PHONE":
        phone = re.sub(r'\D', '', text)
        if len(phone) < 10:
            await update.message.reply_text("❌ Please enter a valid 10-digit mobile number.")
            return

        status_msg = await update.message.reply_text("🌐 Opening browser, navigating to `https://pocketfm.com/login`, clicking 'CONTINUE WITH PHONE', typing number, and requesting OTP...", parse_mode="Markdown")
        
        success = await automate_phone_login(phone, context.user_data)
        
        if success:
            context.user_data["state"] = "AWAITING_OTP"
            await status_msg.edit_text(
                "📩 **OTP Sent!**\n\n"
                "Please reply with the OTP code sent to your mobile device.",
                parse_mode="Markdown"
            )
        else:
            context.user_data["state"] = None
            await status_msg.edit_text("❌ Failed to navigate or request OTP on pocketfm.com. Check logs or try manual cookie entry.")
        return

    # Step 2: Handle OTP Input
    if user_state == "AWAITING_OTP":
        otp = re.sub(r'\D', '', text)
        status_msg = await update.message.reply_text("⏳ Entering OTP and completing login...", parse_mode="Markdown")

        success = await automate_otp_submission(otp, context.user_data)
        context.user_data["state"] = None

        if success:
            await status_msg.edit_text(
                "✅ **Login Successful!**\n\n"
                "Session cookies and token retrieved. Send a Show ID or Link to download episodes.",
                parse_mode="Markdown"
            )
        else:
            await status_msg.edit_text("❌ Login failed. Try again using /start.")
        return

    # Step 3: Handle Manual Cookie Entry
    if user_state == "AWAITING_COOKIE" or text.startswith("eyJ") or "auth-token=" in text:
        CURRENT_COOKIE = text
        context.user_data["state"] = None
        await update.message.reply_text("✅ Cookie/Token updated! Send a Show ID or Link to proceed.")
        return

    # Step 4: Handle Show ID / Fetching Episodes
    show_id = extract_show_id(text)
    status_msg = await update.message.reply_text(f"🔍 Fetching episodes for Show ID: `{show_id}`...", parse_mode="Markdown")

    episodes = await fetch_show_episodes(show_id)

    if not episodes:
        keyboard = [[InlineKeyboardButton("🌐 Web Login", callback_data="btn_web_login")]]
        await status_msg.edit_text(
            "❌ **Could not fetch episodes.**\n\n"
            "Session token may be missing or expired. Click below to log in.",
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
