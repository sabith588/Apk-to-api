import os
import re
import asyncio
import logging
import subprocess
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import unquote, parse_qs, urlparse
import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ==================== CONFIGURATION ====================
BOT_TOKEN = "8827979888:AAGXJJsYhKHcVEGK-aCgJH0RqQxVtJb8Us8"
LOG_CHANNEL_ID = "-1004291729847"
ADMIN_ID = 8861377143
# =======================================================

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

FFMPEG_EXE = "ffmpeg"

class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

def start_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), DummyHandler)
    server.serve_forever()

def extract_stream_url(raw_text: str) -> str:
    m3u8_match = re.search(
        r'(https?%3A%2F%2F[^\s&]+\.(?:m3u8|mp4|jpg|jpeg|png|webp)|https?://[^\s&]+\.(?:m3u8|mp4|jpg|jpeg|png|webp))',
        raw_text, re.IGNORECASE
    )
    if m3u8_match:
        return unquote(m3u8_match.group(1))
    return raw_text.strip()

def extract_title_and_filename(raw_text: str) -> tuple[str, str]:
    """
    Extracts show name and episode number to format clean titles like: 'spiderman-ep2'
    """
    decoded_text = unquote(raw_text)
    show_name = ""
    ep_num = ""

    # 1. Parse 'dt' tracking parameter (e.g. dt=Ep 2 - Jaduyi Angoothi - Super Yoddha)
    dt_match = re.search(r'[?&]dt=([^&]+)', raw_text)
    if dt_match:
        title_str = unquote(dt_match.group(1))
        
        # Extract Episode Number
        ep_match = re.search(r'(?:ep|episode)[\s\-_]*(\d+)', title_str, re.IGNORECASE)
        if ep_match:
            ep_num = f"ep{ep_match.group(1)}"
            
        # Extract Show Name (takes the last segment after pipe | or main title)
        parts = [p.strip() for p in title_str.split('|') if p.strip()]
        if len(parts) > 1:
            show_name = parts[-1]
        else:
            show_name = parts[0]

    # 2. Fallback to URL path searching if dt isn't found
    if not ep_num:
        ep_match = re.search(r'(?:ep|episode)[\s\-_]*(\d+)', decoded_text, re.IGNORECASE)
        if ep_match:
            ep_num = f"ep{ep_match.group(1)}"

    if not show_name:
        # Check for common words or fallback to 'pocketfm'
        words = re.findall(r'[a-zA-Z0-9]+', decoded_text)
        cleaned_words = [w.lower() for w in words if w.lower() not in ['https', 'http', 'com', 'pocketfm', 'episode', 'm3u8', 'cloudfront', 'default', 'qvbr', 'collect', 'analytics']]
        if cleaned_words:
            show_name = "-".join(cleaned_words[:2])
        else:
            show_name = "pocketfm"

    # Clean show name for filename formatting
    show_name = re.sub(r'[^a-zA-Z0-9]', '', show_name).lower()
    if not show_name:
        show_name = "pocketfm"

    # Combine into clean filename format: spiderman-ep2
    if ep_num:
        clean_title = f"{show_name}-{ep_num}"
    else:
        clean_title = show_name

    display_title = clean_title.replace('-', ' ').title()
    return display_title, clean_title

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("⚡ **Auto-Title Media Downloader Ready!**\nSend any audio stream link, video, or photo link.")

async def process_media_url(url: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    stream_url = extract_stream_url(url)
    display_title, file_slug = extract_title_and_filename(url)
    clean_url = stream_url.split('?')[0].lower()
    
    # 1. PHOTO HANDLING
    if any(clean_url.endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.webp']):
        status_msg = await update.message.reply_text(f"⚡ Downloading image for **{display_title}**...")
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
                resp = await client.get(stream_url)
                if resp.status_code == 200:
                    img_path = f"/tmp/{file_slug}.jpg"
                    with open(img_path, "wb") as f:
                        f.write(resp.content)
                    
                    with open(img_path, "rb") as photo:
                        await context.bot.send_photo(
                            chat_id=LOG_CHANNEL_ID,
                            photo=photo,
                            caption=f"🖼 **{display_title}**\n\n🔗 Source: `{stream_url}`"
                        )
                    await status_msg.edit_text("🚀 Sent to Log Channel!")
                    os.remove(img_path)
                else:
                    await status_msg.edit_text(f"❌ Download failed (HTTP {resp.status_code}).")
        except Exception as e:
            await status_msg.edit_text(f"❌ **Error:** {str(e)}")
        return

    # 2. VIDEO MP4 HANDLING
    elif clean_url.endswith('.mp4'):
        status_msg = await update.message.reply_text(f"⚡ Downloading video **{display_title}**...")
        output_file = f"/tmp/{file_slug}.mp4"
        if os.path.exists(output_file): os.remove(output_file)
        
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
                async with client.stream("GET", stream_url) as resp:
                    with open(output_file, "wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=1024*1024):
                            f.write(chunk)
            
            with open(output_file, "rb") as video:
                await context.bot.send_video(
                    chat_id=LOG_CHANNEL_ID,
                    video=video,
                    filename=f"{file_slug}.mp4",
                    caption=f"🎥 **{display_title}**\n\n🔗 Source: `{stream_url}`"
                )
            await status_msg.edit_text("🚀 Sent to Log Channel!")
            os.remove(output_file)
        except Exception as e:
            await status_msg.edit_text(f"❌ **Video Error:** {str(e)}")
        return

    # 3. FAST HLS AUDIO PROCESSING WITH DYNAMIC NAMING
    status_msg = await update.message.reply_text(f"⚡ Fast-extracting audio for **{display_title}**...")
    output_file = f"/tmp/{file_slug}.m4a"
    if os.path.exists(output_file): os.remove(output_file)

    headers = (
        "User-Agent: Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36\r\n"
        "Origin: https://pocketfm.com\r\n"
        "Referer: https://pocketfm.com/\r\n"
    )

    cmd = [
        FFMPEG_EXE, "-y",
        "-headers", headers,
        "-i", stream_url,
        "-c:a", "copy",
        "-threads", "0",
        output_file
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        _, stderr = await process.communicate()

        if os.path.exists(output_file) and os.path.getsize(output_file) > 50000:
            file_size_mb = os.path.getsize(output_file) / (1024 * 1024)
            await status_msg.edit_text(f"⚡ Downloaded ({file_size_mb:.2f} MB).\n📤 Uploading `{file_slug}.m4a` to LogChannel...")

            with open(output_file, "rb") as audio:
                await context.bot.send_audio(
                    chat_id=LOG_CHANNEL_ID,
                    audio=audio,
                    filename=f"{file_slug}.m4a",
                    title=display_title,
                    performer="Pocket FM Bot",
                    caption=f"🎉 **{display_title}**\n\n📁 **File:** `{file_slug}.m4a`\n🔗 **Stream:** `{stream_url}`"
                )
            await status_msg.edit_text("🚀 **Success!** Sent to LogChannel.")

            if os.path.exists(output_file): os.remove(output_file)
        else:
            err_log = stderr.decode()[-250:] if stderr else "Empty output file"
            await status_msg.edit_text(f"❌ **FFmpeg Error:** Could not process stream.\n\n`{err_log}`")

    except Exception as e:
        await status_msg.edit_text(f"❌ **Execution Error:** {str(e)}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    if update.message.text:
        user_text = update.message.text.strip()
        if "http://" in user_text or "https://" in user_text:
            await process_media_url(user_text, update, context)
            return

def main():
    threading.Thread(target=start_dummy_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    print("🤖 Auto-Title Media Downloader Bot is active...")
    app.run_polling()

if __name__ == "__main__":
    main()
