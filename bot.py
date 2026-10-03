import os
import sys
import re
import shutil
import logging
import asyncio
import subprocess
from dotenv import load_dotenv
from pyrogram import Client, filters, enums
from pyrogram.types import Message

# Load environment variables
load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# Telegram User IDs allowed to trigger /update
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

# Regex patterns to search for API keys and tokens
REGEX_PATTERNS = {
    "Generic API Key / Secret": r'(?i)(api[_-]?key|secret|token|auth)\s*[:=]\s*["\']([a-zA-Z0-9_\-\.]{16,64})["\']',
    "JWT Token": r'eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}',
    "Google API Key": r'AIzaSy[a-zA-Z0-9_-]{33}',
    "Firebase / FCM": r'AAAA[a-zA-Z0-9_-]{7}:[a-zA-Z0-9_-]{140}',
    "AWS Key ID": r'AKIA[0-9A-Z]{16}',
    "UUID Format Key": r'\b[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\b',
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

app = Client("apk_scanner_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)


@app.on_message(filters.command("start"))
async def start_cmd(client: Client, message: Message):
    await message.reply_text(
        "👋 Send me an APK file (.apk), and I will scan it for hardcoded API keys."
    )


@app.on_message(filters.command("update"))
async def update_cmd(client: Client, message: Message):
    """Admin-only command to pull updates from Git and restart."""
    user_id = message.from_user.id

    if ADMIN_IDS and user_id not in ADMIN_IDS:
        await message.reply_text("⛔ Unauthorized: You do not have permission to run updates.")
        return

    status_msg = await message.reply_text("🔄 Pulling latest updates from Git...")

    try:
        git_output = subprocess.check_output(["git", "pull"], stderr=subprocess.STDOUT, text=True)
        await status_msg.edit_text(
            f"```\n{git_output}\n```\nRestarting bot process...",
            parse_mode=enums.ParseMode.MARKDOWN
        )

        # Restart Python process
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

    status_msg = await message.reply_text(f"📥 Downloading APK ({round(doc.file_size / (1024*1024), 2)} MB)...")

    file_id = doc.file_id
    apk_path = f"{file_id}.apk"
    output_dir = f"decompiled_{file_id}"

    try:
        # Download APK via Pyrogram (supports up to 2 GB)
        await message.download(file_name=apk_path)

        await status_msg.edit_text("⚙️ Decompiling APK with JADX...")

        # Run JADX command asynchronously
        cmd = ["jadx", "-d", output_dir, "--no-res", apk_path]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        await proc.communicate()

        await status_msg.edit_text("🔍 Scanning decompiled source code...")

        found_keys = set()
        for root, _, files in os.walk(output_dir):
            for file_name in files:
                if file_name.endswith((".java", ".kt", ".xml", ".json")):
                    file_path = os.path.join(root, file_name)
                    try:
                        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                            content = f.read()
                            for key_type, pattern in REGEX_PATTERNS.items():
                                matches = re.findall(pattern, content)
                                for match in matches:
                                    val = match[1] if isinstance(match, tuple) else match
                                    found_keys.add(f"**{key_type}**: `{val}`")
                    except Exception:
                        continue

        if found_keys:
            response_text = "🔑 **Extracted Secrets:**\n\n" + "\n".join(list(found_keys)[:20])
        else:
            response_text = "⚠️ No obvious plaintext API keys found in decompiled code."

        await status_msg.edit_text(response_text, parse_mode=enums.ParseMode.MARKDOWN)

    except Exception as e:
        await status_msg.edit_text(f"❌ Processing error: {str(e)}")
    finally:
        # Clean up temporary files
        if os.path.exists(apk_path):
            os.remove(apk_path)
        if os.path.exists(output_dir):
            shutil.rmtree(output_dir)


if __name__ == "__main__":
    print("Bot startup complete.")
    app.run()
