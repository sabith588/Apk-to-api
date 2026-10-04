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
LOG_CHANNEL_ID = "-1004291729847"
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
    # Extracts .m3u8 or .mp4 links out of nested Google Analytics parameters
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

    # Parse 'dt' parameter from Google Analytics URL
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
            InlineKeyboardButton("🔄 Update & Restart Code", callback_data="update_code"),
            InlineKeyboardButton("ℹ️ Check Status", callback_data="check_status")
        ],
        [
            InlineKeyboardButton("🧹 Clear Temp Cache", callback_data="clear_cache")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "⚙️ **Admin Control Panel**\nSelect an action below:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    if query.data == "update_code":
        await query.edit_message_text("🔄 **Updating Code & Restarting...**\nPlease wait standard deployment time.")
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
            f"🔹 **Engine:** Ultra-Fast yt-dlp Multi-Threading\n"
            f"🔹 **Channel:** `{LOG_CHANNEL_ID}`"
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
        url, update = task
        try:
            await execute_download(url, update, app)
        except Exception as e:
            logging.error(f"Execution Error: {e}")
        finally:
            download_queue.task_done()

async def execute_download(url: str, update: Update, app: Application):
    stream_url = extract_stream_url(url)
    display_title, file_slug = parse_show_details(url)
    clean_url = stream_url.split('?')[0].lower()
    remaining = download_queue.qsize()

    # 1. PHOTO/IMAGE HANDLING
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
                    await status_msg.edit_text(f"🚀 **{display_title}** image uploaded!")
                    if os.path.exists(img_path): os.remove(img_path)
        except Exception as e:
            await status_msg.edit_text(f"❌ Image Error: {str(e)}")
        return

    # 2. AUDIO / VIDEO HIGH-SPEED EXTRACTOR USING YT-DLP
    is_video = 'video' in clean_url or clean_url.endswith('.mp4')
    ext = "mp4" if is_video else "m4a"
    output_path = f"/tmp/{file_slug}.{ext}"

    status_msg = await update.message.reply_text(
        f"⚡ **Ultra-Fast Extracting ({ext.upper()}):** `{display_title}`\n⏳ Queue remaining: `{remaining}`..."
    )

    if os.path.exists(output_path):
        os.remove(output_path)

    # Multi-threaded download command (Concurrent 16 threads for maximum network speed)
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

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        _, stderr = await process.communicate()

        if os.path.exists(output_path) and os.path.getsize(output_path) > 10000:
            file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
            await status_msg.edit_text(f"⚡ Downloaded ({file_size_mb:.2f} MB).\n📤 Uploading `{file_slug}.{ext}`...")

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

            await status_msg.edit_text(f"🚀 **{display_title}** finished in seconds!")
            if os.path.exists(output_path): os.remove(output_path)
        else:
            err_log = stderr.decode()[-250:] if stderr else "Download error"
            await status_msg.edit_text(f"❌ **yt-dlp Error:** `{err_log}`")

    except Exception as e:
        await status_msg.edit_text(f"❌ Execution Error: {str(e)}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text("⚡ **High-Speed Downloader Ready!**\nSend stream links or use /admin.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return

    if update.message.text:
        text = update.message.text.strip()
        urls = re.findall(r'https?://[^\s]+', text)
        if urls:
            for url in urls:
                await download_queue.put((url, update))
            await update.message.reply_text(f"📥 Added {len(urls)} item(s) to queue!")

def main():
    threading.Thread(target=start_dummy_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    loop = asyncio.get_event_loop()
    loop.create_task(process_queue_worker(app))

    print("🤖 Ultra-Fast yt-dlp Downloader Bot Active...")
    app.run_polling()

if __name__ == "__main__":
    main()
