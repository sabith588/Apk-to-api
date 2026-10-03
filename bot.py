import os
import sys
import re
import shutil
import logging
import asyncio
import subprocess
from dotenv import load_dotenv
from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

# Load environment variables
load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# Telegram User IDs allowed to trigger updates
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

# Marker file path to track restarts across updates
RESTART_MARKER_FILE = "restart.txt"

# Search patterns for API keys, secrets, and configuration fields
REGEX_PATTERNS = {
    "Generic API Key / Secret": r'(?i)(api[_-]?key|secret|token|auth|bearer)\s*[:=]\s*["\']([a-zA-Z0-9_\-\.]{16,64})["\']',
    "XML String Key": r'(?i)<string name="[^"]*(?:key|api|token|secret)[^"]*">([a-zA-Z0-9_\-\.]{16,64})</string>',
    "JWT Token": r'eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}',
    "Google / Firebase Key": r'AIzaSy[a-zA-Z0-9_-]{33}',
    "FCM / Server Key": r'AAAA[a-zA-Z0-9_-]{7}:[a-zA-Z0-9_-]{140}',
    "AWS Key ID": r'AKIA[0-9A-Z]{16}',
    "API Endpoint / URL": r'https?://[a-zA-Z0-9\-\.]+\.[a-zA-Z]{2,}(?:/[a-zA-Z0-9_%\-\.]*)*',
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

app = Client("apk_scanner_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)


def get_admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Restart / Check Status", callback_data="trigger_restart")]
    ])


@app.on_message(filters.command("start"))
async def start_cmd(client: Client, message: Message):
    keyboard = get_admin_keyboard() if message.from_user.id in ADMIN_IDS else None
    await message.reply_text(
        "👋 Send me an APK file (.apk), and I will scan its source code, resources, and `.so` binaries for hardcoded keys and endpoints.",
        reply_markup=keyboard
    )


@app.on_message(filters.command("update"))
async def update_cmd(client: Client, message: Message):
    if ADMIN_IDS and message.from_user.id not in ADMIN_IDS:
        await message.reply_text("⛔ Unauthorized user.")
        return
    await initiate_restart(message)


@app.on_callback_query(filters.regex("^trigger_restart$"))
async def update_callback(client: Client, callback_query: CallbackQuery):
    if ADMIN_IDS and callback_query.from_user.id not in ADMIN_IDS:
        await callback_query.answer("⛔ Unauthorized: Admin only.", show_alert=True)
        return

    await callback_query.answer("Initiating bot restart...")
    await initiate_restart(callback_query.message)


async def initiate_restart(message: Message):
    """Saves the chat ID and restarts the bot process."""
    status_msg = await message.reply_text("🔄 Saving state and restarting bot process...")
    
    # Save target chat ID to marker file
    try:
        with open(RESTART_MARKER_FILE, "w") as f:
            f.write(f"{message.chat.id}:{status_msg.id}")
    except Exception as e:
        logging.error(f"Failed to write restart marker: {e}")

    # Restart Python process
    os.execv(sys.executable, [sys.executable] + sys.argv)


async def check_startup_notification():
    """Checks if the bot was restarted via command/update and notifies the admin."""
    if os.path.exists(RESTART_MARKER_FILE):
        try:
            with open(RESTART_MARKER_FILE, "r") as f:
                data = f.read().strip()

            if ":" in data:
                chat_id, msg_id = data.split(":", 1)
                chat_id = int(chat_id)
                msg_id = int(msg_id)

                # Edit previous message or send a new notification
                try:
                    await app.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text="✅ **Bot updated and restarted successfully!**\n\nAll services are online and ready.",
                        parse_mode=enums.ParseMode.MARKDOWN,
                        reply_markup=get_admin_keyboard()
                    )
                except Exception:
                    await app.send_message(
                        chat_id=chat_id,
                        text="✅ **Bot updated and restarted successfully!**",
                        parse_mode=enums.ParseMode.MARKDOWN,
                        reply_markup=get_admin_keyboard()
                    )
        except Exception as e:
            logging.error(f"Error handling startup notification: {e}")
        finally:
            if os.path.exists(RESTART_MARKER_FILE):
                os.remove(RESTART_MARKER_FILE)


@app.on_message(filters.document)
async def process_apk(client: Client, message: Message):
    doc = message.document

    if not doc.file_name or not doc.file_name.lower().endswith(".apk"):
        await message.reply_text("❌ Please upload a valid `.apk` file.")
        return

    status_msg = await message.reply_text(
        f"📥 Downloading APK ({round(doc.file_size / (1024*1024), 2)} MB)..."
    )

    file_id = doc.file_id
    apk_path = f"{file_id}.apk"
    output_dir = f"decompiled_{file_id}"

    try:
        await message.download(file_name=apk_path)
        await status_msg.edit_text("⚙️ Decompiling APK source code and native binaries...")

        cmd = ["jadx", "-d", output_dir, apk_path]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        await proc.communicate()

        await status_msg.edit_text("🔍 Scanning decompiled files & .so native libraries...")

        found_keys = set()

        for root, _, files in os.walk(output_dir):
            for file_name in files:
                file_path = os.path.join(root, file_name)

                if file_name.endswith((".java", ".kt", ".xml", ".json", ".properties", ".txt", ".so")):
                    try:
                        with open(file_path, "rb") as f:
                            raw_data = f.read()
                            content = raw_data.decode("latin-1", errors="ignore")

                            for key_type, pattern in REGEX_PATTERNS.items():
                                matches = re.findall(pattern, content)
                                for match in matches:
                                    val = match[1] if isinstance(match, tuple) else match
                                    if val and len(val) > 10 and not val.startswith("0x"):
                                        found_keys.add(f"**{key_type}**: `{val[:80]}`")
                    except Exception:
                        continue

        keyboard = get_admin_keyboard() if message.from_user.id in ADMIN_IDS else None

        if found_keys:
            results_list = list(found_keys)[:25]
            response_text = "🔑 **Extracted Secrets & Endpoints:**\n\n" + "\n".join(results_list)
        else:
            response_text = (
                "⚠️ **No static API keys found in plaintext.**\n\n"
                "The app likely relies on dynamic network authentication or obfuscated parameters."
            )

        await status_msg.edit_text(
            response_text,
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=keyboard
        )

    except Exception as e:
        await status_msg.edit_text(f"❌ Processing error: {str(e)}")
    finally:
        if os.path.exists(apk_path):
            os.remove(apk_path)
        if os.path.exists(output_dir):
            shutil.rmtree(output_dir)


async def main():
    await app.start()
    print("Bot startup complete.")
    # Send notification if recovering from a restart
    await check_startup_notification()
    # Keep running
    await asyncio.Event().wait()


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
