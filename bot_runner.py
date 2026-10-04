import os
import re
import asyncio
import logging
import subprocess
from urllib.parse import unquote
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ==================== CONFIGURATION ====================
BOT_TOKEN = ("8827979888:AAFdnb6pFvEuDva9-KKBjLx4fmeHt8H39Ek")
LOG_CHANNEL_ID = ("-1004291729847")
ADMIN_ID = ("8861377143"))
# =======================================================

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

FFMPEG_EXE = "ffmpeg"  # Natively available in Docker container

def extract_stream_url(raw_text: str) -> str:
    m3u8_match = re.search(r'(https?%3A%2F%2F[^\s&]+\.m3u8|https?://[^\s&]+\.m3u8)', raw_text)
    if m3u8_match:
        return unquote(m3u8_match.group(1))
    return raw_text.strip()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("👋 **Pocket FM Bot Ready!**\nSend any audio stream link to download.")

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    user_text = update.message.text.strip()
    if not ("http://" in user_text or "https://" in user_text):
        await update.message.reply_text("⚠️ Send a valid link starting with http:// or https://")
        return

    stream_url = extract_stream_url(user_text)
    status_msg = await update.message.reply_text(f"⏳ **Stream detected:**\n`{stream_url}`\n\nDownloading audio via FFmpeg...")

    output_file = "/tmp/episode_audio.mp3"
    if os.path.exists(output_file):
        os.remove(output_file)

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
                    title="Pocket FM Episode",
                    performer="Pocket FM Bot",
                    caption=f"🎉 **Audio Synced Successfully!**\n\n🔗 Stream: `{stream_url}`"
                )
            await status_msg.edit_text("🚀 **Success!** Sent to LogChannel.")

            if os.path.exists(output_file):
                os.remove(output_file)
        else:
            err_log = stderr.decode()[-250:] if stderr else "Empty output file"
            await status_msg.edit_text(f"❌ **FFmpeg Error:** Failed to process audio stream.\n\n`{err_log}`")

    except Exception as e:
        await status_msg.edit_text(f"❌ **Execution Error:** {str(e)}")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

    print("🤖 Bot is active and listening...")
    app.run_polling()

if __name__ == "__main__":
    main()
