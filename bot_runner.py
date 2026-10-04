import os
import re
import sys
import asyncio
import logging
import subprocess
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import unquote
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# ==================== CONFIGURATION ====================
BOT_TOKEN = "8827979888:AAGXJJsYhKHcVEGK-aCgJH0RqQxVtJb8Us8"
# Converted to integer for telegram-bot API accuracy
LOG_CHANNEL_ID = -1004291729847
ADMIN_ID = 8861377143
# =======================================================

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

download_queue = asyncio.Queue()

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

def parse_show_details(raw_text: str) -> tuple[str, str]:
    decoded = unquote(raw_text)
    show_name = ""
    ep_num = ""

    dt_match = re.search(r'[?&]dt=([^&]+)', raw_text)
    if dt_match:
        dt_val = unquote(dt_match.group(1))
        ep_m = re.search(r'(?:ep|episode)\s*(\d+)', dt_val, re.IGNORECASE)
        if ep_m:
            ep_num = f"ep{ep_m.group(1)}"

        parts = [p.strip() for p in dt_val.split('-') if p.strip()]
        if len(parts) >= 2:
            show_name = parts[-1]
        elif '|' in dt_val:
            show_name = dt_val.split('|')[-1].strip()

    if not ep_num:
        st_match = re.search(r'[?&]up\.story_title=([^&]+)', raw_text)
        if st_match:
            st_val = unquote(st_match.group(1))
            ep_m = re.search(r'(?:ep|episode)\s*(\d+)', st_val, re.IGNORECASE)
            if ep_m:
                ep_num = f"ep{ep_m.group(1)}"

    if not show_name:
        show_name = "super-yoddha"
    if not ep_num:
        ep_num = "ep"

    clean_show = re.sub(r'[^a-zA-Z0-9\s]', '', show_name).strip()
    formatted_show = re.sub(r'\s+', '-', clean_show).lower()
    slug = f"{formatted_show}-{ep_num}"
    display_name = f"{clean_show.title()} {ep_num.upper()}"

    return display_name, slug

# ==================== ADMIN PANEL ====================

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    keyboard = [
        [
            InlineKeyboardButton("🔄 Update & Restart", callback_data="update_code"),
            InlineKeyboardButton("ℹ️ Check Status", callback_data="check_status")
        ],
        [
            InlineKeyboardButton("🧹 Clear Temp Cache", callback_data="clear_cache")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "⚙️ **Admin Control Panel**",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    if query.data == "update_code":
        await query.edit_message_text("🔄 **Restarting Bot...**")
        try:
            subprocess.run(["git", "pull"], check=True)
        except Exception:
            pass
        os.execv(sys.executable, [sys.executable] + sys.argv)

    elif query.data == "check_status":
        queue_size = download_queue.qsize()
        await query.edit_message_text(
            f"📊 **Bot Status**\n\n"
            f"🔹 **Queue:** `{queue_size}` remaining\n"
            f"🔹 **Target Channel:** `{LOG_CHANNEL_ID}`"
        )

    elif query.data == "clear_cache":
        count = 0
        for f in os.listdir("/tmp"):
            if f.endswith((".m4a", ".mp4", ".jpg", ".png", ".webp")):
                os.remove(os.path.join("/tmp", f))
                count += 1
        await query.edit_message_text(f"🧹 **Cache Cleared:** Removed {count} file(s).")

# ==================== DOWNLOAD WORKER ====================

async def process_queue_worker(app: Application):
    while True:
        task = await download_queue.get()
        url, force_video, update = task
        try:
            await execute_download(url, force_video, update, app)
        except Exception as e:
            logging.error(f"Execution Error: {e}")
        finally:
            download_queue.task_done()

async def execute_download(url: str, force_video: bool, update: Update, app: Application):
    stream_url = extract_stream_url(url)
    display_title, file_slug = parse_show_details(url)
    clean_url = stream_url.split('?')[0].lower()
    remaining = download_queue.qsize()

    # 1. IMAGE HANDLING
    if any(clean_url.endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.webp']):
        status_msg = await update.message.reply_text(f"⚡ **Downloading Image:** `{display_title}`...")
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
                resp = await client.get(stream_url)
                if resp.status_code == 200:
                    img_path = f"/tmp/{file_slug}.jpg"
                    with open(img_path, "wb") as f: f.write(resp.content)
                    
                    with open(img_path, "rb") as photo:
                        await app.bot.send_photo(
                            chat_id=LOG_CHANNEL_ID,
                            photo=photo,
                            caption=f"🖼 **{display_title}**\n\n🔗 `{stream_url}`"
                        )
                    await status_msg.edit_text(f"🚀 **{display_title}** image uploaded to channel!")
                    if os.path.exists(img_path): os.remove(img_path)
        except Exception as e:
            await status_msg.edit_text(f"❌ Upload Error to Channel: `{str(e)}`")
        return

    # 2. MEDIA EXTRACTION
    is_video = force_video or ('video' in clean_url or clean_url.endswith('.mp4'))
    ext = "mp4" if is_video else "m4a"
    output_path = f"/tmp/{file_slug}.{ext}"

    status_msg = await update.message.reply_text(
        f"⚡ **Downloading {ext.upper()}:** `{display_title}`\n⏳ Queue remaining: `{remaining}`..."
    )

    if os.path.exists(output_path):
        os.remove(output_path)

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--concurrent-fragments", "16",
        "--add-header", "User-Agent: Mozilla/5.0 (Linux; Android 10; Mobile)",
        "--add-header", "Origin: https://pocketfm.com",
        "--add-header", "Referer: https://pocketfm.com/",
        "-o", output_path,
        stream_url
    ]

    if is_video:
        cmd.extend(["--recode-video", "mp4"])

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        _, stderr = await process.communicate()

        if os.path.exists(output_path) and os.path.getsize(output_path) > 10000:
            file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
            await status_msg.edit_text(f"⚡ Downloaded ({file_size_mb:.2f} MB).\n📤 Uploading `{file_slug}.{ext}` to Channel...")

            try:
                with open(output_path, "rb") as media_file:
                    if is_video:
                        await app.bot.send_video(
                            chat_id=LOG_CHANNEL_ID,
                            video=media_file,
                            filename=f"{file_slug}.mp4",
                            caption=f"🎬 **{display_title} (Video)**\n📁 File: `{file_slug}.mp4`"
                        )
                    else:
                        await app.bot.send_audio(
                            chat_id=LOG_CHANNEL_ID,
                            audio=media_file,
                            filename=f"{file_slug}.m4a",
                            title=display_title,
                            performer="Pocket FM Bot",
                            caption=f"🎉 **{display_title}**\n📁 File: `{file_slug}.m4a`"
                        )
                await status_msg.edit_text(f"🚀 **{display_title}** ({ext.upper()}) uploaded to channel successfully!")
            except Exception as upload_err:
                await status_msg.edit_text(f"❌ **Channel Upload Failed:** `{str(upload_err)}`\n\nMake sure the bot is an **Admin** in channel ID `{LOG_CHANNEL_ID}` with posting permissions.")

            if os.path.exists(output_path): os.remove(output_path)
        else:
            err_log = stderr.decode()[-250:] if stderr else "Download error"
            await status_msg.edit_text(f"❌ **Error:** `{err_log}`")

    except Exception as e:
        await status_msg.edit_text(f"❌ Execution Error: {str(e)}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text(
        "⚡ **High-Speed Downloader Ready!**\n\n"
        "• Paste any Pocket FM link directly for Audio\n"
        "• Type `/video <link>` to download Video (`.mp4`)"
    )

async def handle_video_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    
    if context.args:
        url = context.args[0]
        await download_queue.put((url, True, update))
        await update.message.reply_text("📥 Added 1 **Video** task to queue!")
    else:
        await update.message.reply_text("⚠️ Please provide a link: `/video <link>`")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return

    if update.message.text:
        text = update.message.text.strip()
        urls = re.findall(r'https?://[^\s]+', text)
        if urls:
            for url in urls:
                await download_queue.put((url, False, update))
            await update.message.reply_text(f"📥 Added {len(urls)} item(s) to queue!")

def main():
    threading.Thread(target=start_dummy_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("video", handle_video_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    loop = asyncio.get_event_loop()
    loop.create_task(process_queue_worker(app))

    print("🤖 Downloader Active...")
    app.run_polling()

if __name__ == "__main__":
    main()
