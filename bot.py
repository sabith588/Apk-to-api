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

# Expanded Regex Patterns for secrets, tokens, and general key-value pairs
REGEX_PATTERNS = {
    "Generic API Key / Secret": r'(?i)(api[_-]?key|secret|token|auth|bearer)\s*[:=]\s*["\']([a-zA-Z0-9_\-\.]{16,64})["\']',
    "XML / Config String Key": r'(?i)<string name="[^"]*(?:key|api|token|secret)[^"]*">([a-zA-Z0-9_\-\.]{16,64})</string>',
    "JWT Token": r'eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}',
    "Google / Firebase Key": r'AIzaSy[a-zA-Z0-9_-]{33}',
    "FCM / Server Key": r'AAAA[a-zA-Z0-9_-]{7}:[a-zA-Z0-9_-]{140}',
    "AWS Key ID": r'AKIA[0-9A-Z]{16}',
    "UUID / Hex Key": r'\b[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\b',
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

app = Client("apk_scanner_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)


def get_admin_keyboard():
    """Returns an inline keyboard with the update button."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Update Code from Git", callback_data="trigger_git_update")]
    ])


@app.on_message(filters.command("start"))
async def start_cmd(client: Client, message: Message):
    keyboard = get_admin_keyboard() if message.from_user.id in ADMIN_IDS else None
    await message.reply_text(
        "👋 Send me an APK file (.apk), and I will scan it for hardcoded API keys.",
        reply_markup=keyboard
    )


@app.on_message(filters.command("update"))
async def update_cmd(client: Client, message: Message):
    """Admin command to trigger git update."""
    if ADMIN_IDS and message.from_user.id not in ADMIN_IDS:
        await message.reply_text("⛔ Unauthorized user.")
        return
    await run_git_update(message)


@app.on_callback_query(filters.regex("^trigger_git_update$"))
async def update_callback(client: Client, callback_query: CallbackQuery):
    """Handles button clicks for updating code."""
    if ADMIN_IDS and callback_query.from_user.id not in ADMIN_IDS:
        await callback_query.answer("⛔ Unauthorized: Admin only.", show_alert=True)
        return

    await callback_query.answer("Starting Git update...")
    await run_git_update(callback_query.message)


async def run_git_update(message: Message):
    """Executes git pull and restarts the bot process."""
    status_msg = await message.reply_text("🔄 Pulling latest updates from Git...")
    try:
        git_output = subprocess.check_output(["git", "pull"], stderr=subprocess.STDOUT, text=True)
        await status_msg.edit_text(
            f"```\n{git_output}\n```\nRestarting bot process...",
            parse_mode=enums.ParseMode.MARKDOWN
        )
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except subprocess.CalledProcessError as e:
        await status_msg.edit_text(
            f"❌ Git Update Failed:\n```\n{e.output}\n```",
            parse_mode=enums.ParseMode.MARKDOWN
        )
    except Exception as e:
        await status_msg.edit_text(f"❌ Failed to restart bot: {str(e)}")


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
        # Download APK
        await message.download(file_name=apk_path)

        await status_msg.edit_text("⚙️ Decompiling APK with JADX (including resources)...")

        # Decompile both code and resources to scan strings.xml & config files
        cmd = ["jadx", "-d", output_dir, apk_path]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        await proc.communicate()

        await status_msg.edit_text("🔍 Scanning decompiled code and assets...")

        found_keys = set()
        
        # Walk through all decompiled files (java, xml, json, properties, txt)
        for root, _, files in os.walk(output_dir):
            for file_name in files:
                # Include configuration and resource files alongside source code
                if file_name.endswith((".java", ".kt", ".xml", ".json", ".properties", ".txt")):
                    file_path = os.path.join(root, file_name)
                    try:
                        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                            content = f.read()
                            for key_type, pattern in REGEX_PATTERNS.items():
                                matches = re.findall(pattern, content)
                                for match in matches:
                                    val = match[1] if isinstance(match, tuple) else match
                                    # Exclude standard placeholder values
                                    if val and not val.startswith("0x"):
                                        found_keys.add(f"**{key_type}**: `{val}`")
                    except Exception:
                        continue

        keyboard = get_admin_keyboard() if message.from_user.id in ADMIN_IDS else None

        if found_keys:
            response_text = "🔑 **Extracted Secrets:**\n\n" + "\n".join(list(found_keys)[:20])
        else:
            response_text = "⚠️ No obvious plaintext API keys found in source code or resource files."

        await status_msg.edit_text(
            response_text,
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=keyboard
        )

    except Exception as e:
        await status_msg.edit_text(f"❌ Processing error: {str(e)}")
    finally:
        # Cleanup
        if os.path.exists(apk_path):
            os.remove(apk_path)
        if os.path.exists(output_dir):
            shutil.rmtree(output_dir)


if __name__ == "__main__":
    print("Bot startup complete.")
    app.run()
