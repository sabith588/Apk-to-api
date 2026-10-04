import os
import re
import asyncio
import logging
import subprocess
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import unquote
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
    m3u8_match = re.search(r'(https?%3A%2F%2F[^\s&]+\.(?:m3u8|mp4|jpg|jpeg|png|webp)|https?://[^\s&]+\.(?:m3u8|mp4|jpg|jpeg|png|webp))', raw_text, re.IGNORECASE)
    if m3u8_match:
        return unquote(m3u8_match.group(1))
    return raw_text.strip()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("👋 **Pocket FM Media Downloader Ready!**\nSend any audio stream, video, photo link, or media file.")

async def process_media_url(url: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    stream_url = extract_stream_url(url)
    clean_url = stream_url.split('?')[0].lower()
    
    # 1. PHOTO HANDLING
    if any(clean_url.endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.webp']):
        status_msg = await update.message.reply_text("📸 **Image detected!** Downloading...")
        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                resp = await client.get(stream_url)
                if resp.status_code == 200:
                    img_path = "/tmp/downloaded_image.jpg"
                    with open(img_path, "wb") as f:
                        f.write(resp.content)
                    
                    with open(img_path, "rb") as photo:
                        await context.bot.send_photo(
                            chat_id=LOG_CHANNEL_ID,
                            photo=photo,
                            caption=f"🖼 **Photo Downloaded**\n\n🔗 Source: `{stream_url}`"
                        )
                    await status_msg.edit_text("🚀 Photo sent to Log Channel!")
                    os.remove(img_path)
                else:
                    await status_msg.edit_text(f"❌ Failed to download photo (HTTP {resp.status_code}).")
        except Exception as e:
            await status_msg.edit_text(f"❌ **Photo Download Error:** {str(e)}")
        return

    # 2. VIDEO MP4 HANDLING
    elif clean_url.endswith('.mp4'):
        status_msg = await update.message.reply_text("🎬 **Direct Video (.mp4) detected!** Downloading...")
        output_file = "/tmp/media_video.mp4"
        if os.path.exists(output_file): os.remove(output_file)
        
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
                async with client.stream("GET", stream_url) as resp:
                    with open(output_file, "wb") as f:
                        async for chunk in resp.aiter_bytes():
                            f.write(chunk)
            
            with open(output_file, "rb") as video:
                await context.bot.send_video(
                    chat_id=LOG_CHANNEL_ID,
                    video=video,
                    caption=f"🎥 **Video Downloaded**\n\n🔗 Source: `{stream_url}`"
                )
            await status_msg.edit_text("🚀 Video sent to Log Channel!")
            os.remove(output_file)
        except Exception as e:
            await status_msg.edit_text(f"❌ **Video Download Error:** {str(e)}")
        return

    # 3. STREAM AUDIO / VIDEO HLS (.m3u8 / CloudFront)
    status_msg = await update.message.reply_text(f"⏳ **Stream detected:**\n`{stream_url}`\n\nProcessing media via FFmpeg...")
    output_file = "/tmp/episode_audio.mp3"
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
        "-acodec", "libmp3lame",
        "-ab", "128k",
        output_file
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        _, stderr = await process.communicate()

        if os.path.exists(output_file) and os.path.getsize(output_file) > 50000:
            file_size_mb = os.path.getsize(output_file) / (1024 * 1024)
            await status_msg.edit_text(f"✅ Downloaded ({file_size_mb:.2f} MB).\n📤 Uploading to LogChannel...")

            with open(output_file, "rb") as audio:
                await context.bot.send_audio(
                    chat_id=LOG_CHANNEL_ID,
                    audio=audio,
                    title="Pocket FM Media",
                    performer="Pocket FM Bot",
                    caption=f"🎉 **Media Synced Successfully!**\n\n🔗 Stream: `{stream_url}`"
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

    # Handle direct text links
    if update.message.text:
        user_text = update.message.text.strip()
        if "http://" in user_text or "https://" in user_text:
            await process_media_url(user_text, update, context)
            return

    # Handle uploaded photos attached in Telegram
    if update.message.photo:
        status_msg = await update.message.reply_text("📥 Processing attached photo...")
        photo_file = await update.message.photo[-1].get_file()
        save_path = "/tmp/telegram_photo.jpg"
        await photo_file.download_to_drive(save_path)
        
        with open(save_path, "rb") as photo:
            await context.bot.send_photo(
                chat_id=LOG_CHANNEL_ID,
                photo=photo,
                caption="🖼 **Photo Archived via Bot**"
            )
        await status_msg.edit_text("🚀 Photo uploaded to Log Channel!")
        os.remove(save_path)
        return

    # Handle uploaded video files attached in Telegram
    if update.message.video:
        status_msg = await update.message.reply_text("📥 Processing attached video...")
        video_file = await update.message.video.get_file()
        save_path = "/tmp/telegram_video.mp4"
        await video_file.download_to_drive(save_path)
        
        with open(save_path, "rb") as video:
            await context.bot.send_video(
                chat_id=LOG_CHANNEL_ID,
                video=video,
                caption="🎥 **Video Archived via Bot**"
            )
        await status_msg.edit_text("🚀 Video uploaded to Log Channel!")
        os.remove(save_path)
        return

def main():
    threading.Thread(target=start_dummy_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    print("🤖 Media Auto-Downloader Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
