import asyncio
import html
import json
import logging
import os
import random
import re
import unicodedata
from pathlib import Path

from pyrogram import Client, filters, enums
from pyrogram.errors import FloodWait, RPCError

# Required environment variables: API_ID, API_HASH, BOT_TOKEN
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("PandeyMentionBot")

app = Client(
    "PandeyMentionBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=4,
)

BASE_DIR = Path(__file__).resolve().parent
QUESTIONS_FILE = BASE_DIR / "questions.json"

try:
    with QUESTIONS_FILE.open("r", encoding="utf-8") as f:
        QUESTION_BANK = json.load(f)
except Exception:
    log.exception("questions.json load nahi hua")
    QUESTION_BANK = []

# One active sending task per group.
running_jobs = {}
# chat_id -> user_id -> {"answers": [...], "question": "..."}
active_answers = {}
# chat_id -> user_id -> score
quiz_scores = {}


def normalize_answer(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold().strip()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


async def is_admin(client, chat_id: int, user_id: int) -> bool:
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member.status in (
            enums.ChatMemberStatus.OWNER,
            enums.ChatMemberStatus.ADMINISTRATOR,
        )
    except RPCError:
        return False


async def admin_only(client, message) -> bool:
    if not message.from_user:
        await message.reply_text("यह कमांड केवल ग्रुप एडमिन चला सकता है।")
        return False
    if not await is_admin(client, message.chat.id, message.from_user.id):
        await message.reply_text("❌ यह कमांड केवल ग्रुप एडमिन के लिए है।")
        return False
    return True


async def safe_send(client, chat_id: int, text: str):
    try:
        await client.send_message(
            chat_id,
            text,
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except FloodWait as e:
        await asyncio.sleep(int(e.value) + 1)
        await client.send_message(
            chat_id,
            text,
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
        )


def member_name(user) -> str:
    name = " ".join(x for x in [user.first_name, user.last_name] if x).strip()
    return name or user.username or str(user.id)


@app.on_message(filters.command("start") & filters.private)
async def start_private(client, message):
    await message.reply_text(
        "नमस्ते! मैं Pandey Mention Bot हूँ।\n"
        "मुझे अपने ग्रुप में जोड़ें और एडमिन अधिकार दें।\n"
        "ग्रुप में /help लिखकर कमांड देखें।"
    )


@app.on_message(filters.command("start") & filters.group)
async def start_group(client, message):
    await message.reply_text(
        "नमस्ते! Pandey Mention Bot तैयार है।\n/help लिखें।"
    )


@app.on_message(filters.command("help"))
async def help_command(client, message):
    await message.reply_text(
        "📌 <b>Bot Commands</b>\n\n"
        "/start - Bot शुरू करें\n"
        "/help - कमांड की जानकारी\n"
        "/mention - सभी सदस्यों को टैग करें\n"
        "/mention आपका संदेश - संदेश के साथ सभी को टैग करें\n"
        "/cancel - चल रही Mention/Question प्रक्रिया रोकें\n"
        "/users - ग्रुप में सदस्यों की संख्या\n"
        "/id - ग्रुप और यूजर ID देखें\n"
        "/question - हर सदस्य को अलग Quiz सवाल भेजें\n\n"
        "⚠️ /mention, /cancel और /question केवल ग्रुप एडमिन चला सकते हैं।\n"
        "Quiz के सवाल हिंदी में हैं। जवाब देने पर Bot सही/गलत बताएगा।",
        parse_mode=enums.ParseMode.HTML,
    )


async def mention_worker(client, chat_id: int, custom_message: str, cancel_event):
    batch = []
    try:
        async for member in client.get_chat_members(chat_id):
            if cancel_event.is_set():
                break
            user = member.user
            if user.is_bot or user.is_deleted:
                continue
            label = html.escape(member_name(user))
            batch.append(f'<a href="tg://user?id={user.id}">{label}</a>')
            if len(batch) >= 5:
                prefix = (html.escape(custom_message) + "\n\n") if custom_message else ""
                await safe_send(client, chat_id, prefix + " ".join(batch))
                batch.clear()
                await asyncio.sleep(1)
        if batch and not cancel_event.is_set():
            prefix = (html.escape(custom_message) + "\n\n") if custom_message else ""
            await safe_send(client, chat_id, prefix + " ".join(batch))
        if cancel_event.is_set():
            await safe_send(client, chat_id, "⏹️ Mention प्रक्रिया रोक दी गई।")
        else:
            await safe_send(client, chat_id, "✅ सभी उपलब्ध सदस्यों को Mention कर दिया गया।")
    except FloodWait as e:
        await safe_send(client, chat_id, f"⏳ Telegram ने थोड़ी देर रुकने को कहा है। {e.value} सेकंड बाद फिर कोशिश करें।")
    except RPCError:
        log.exception("Mention RPC error")
        await safe_send(client, chat_id, "❌ Mention करते समय Telegram error आया। Bot के admin अधिकार जाँचें।")
    finally:
        running_jobs.pop(chat_id, None)


@app.on_message(filters.command("mention") & filters.group)
async def mention_command(client, message):
    if not await admin_only(client, message):
        return
    chat_id = message.chat.id
    if chat_id in running_jobs:
        await message.reply_text("एक Mention प्रक्रिया पहले से चल रही है। रोकने के लिए /cancel लिखें।")
        return
    custom_message = message.text.partition(" ")[2].strip()
    cancel_event = asyncio.Event()
    task = asyncio.create_task(mention_worker(client, chat_id, custom_message, cancel_event))
    running_jobs[chat_id] = {"task": task, "cancel": cancel_event}
    await message.reply_text("🔎 सदस्यों को ढूँढकर Mention शुरू कर रहा हूँ...")


def choose_question():
    if not QUESTION_BANK:
        return None
    return random.choice(QUESTION_BANK)


async def question_worker(client, chat_id: int, cancel_event):
    active_answers.setdefault(chat_id, {})
    quiz_scores.setdefault(chat_id, {})
    sent = 0
    try:
        async for member in client.get_chat_members(chat_id):
            if cancel_event.is_set():
                break
            user = member.user
            if user.is_bot or user.is_deleted:
                continue
            item = choose_question()
            if not item:
                await safe_send(client, chat_id, "❌ questions.json में सवाल नहीं मिले।")
                return
            answers = item.get("answers", [])
            if not answers:
                continue
            active_answers[chat_id][user.id] = {
                "answers": [normalize_answer(a) for a in answers],
                "question": item["question"],
                "category": item.get("category", "सामान्य ज्ञान"),
            }
            quiz_scores[chat_id].setdefault(user.id, 0)
            qtext = (
                f"🧠 <b>{html.escape(item.get('category', 'Quiz'))}</b>\n"
                f'<a href="tg://user?id={user.id}">{html.escape(member_name(user))}</a>, '
                f"आपका सवाल:\n<b>{html.escape(item['question'])}</b>"
            )
            await safe_send(client, chat_id, qtext)
            sent += 1
            await asyncio.sleep(1)
        if cancel_event.is_set():
            await safe_send(client, chat_id, f"⏹️ Quiz सवाल भेजना रोक दिया गया। {sent} सवाल भेजे गए।")
        else:
            await safe_send(client, chat_id, f"✅ Quiz के {sent} अलग-अलग सवाल भेज दिए गए। जवाब आने पर जाँच होगी।")
    except FloodWait as e:
        await safe_send(client, chat_id, f"⏳ Telegram FloodWait: {e.value} सेकंड। कुछ सवाल भेजे गए; बाद में फिर कोशिश करें।")
    except RPCError:
        log.exception("Question RPC error")
        await safe_send(client, chat_id, "❌ सवाल भेजते समय Telegram error आया। Bot के admin अधिकार जाँचें।")
    finally:
        running_jobs.pop(chat_id, None)


@app.on_message(filters.command("question") & filters.group)
async def question_command(client, message):
    if not await admin_only(client, message):
        return
    chat_id = message.chat.id
    if chat_id in running_jobs:
        await message.reply_text("एक प्रक्रिया पहले से चल रही है। पहले /cancel करें।")
        return
    if not QUESTION_BANK:
        await message.reply_text("questions.json में सवाल नहीं हैं।")
        return
    cancel_event = asyncio.Event()
    task = asyncio.create_task(question_worker(client, chat_id, cancel_event))
    running_jobs[chat_id] = {"task": task, "cancel": cancel_event}
    quiz_scores[chat_id] = {}
    await message.reply_text("🧠 Quiz शुरू! हर सदस्य को 1 सेकंड के अंतर पर अलग सवाल मिलेगा।")


@app.on_message(filters.command("cancel") & filters.group)
async def cancel_command(client, message):
    if not await admin_only(client, message):
        return
    job = running_jobs.get(message.chat.id)
    if not job:
        await message.reply_text("अभी कोई Mention या Quiz प्रक्रिया नहीं चल रही।")
        return
    job["cancel"].set()
    await message.reply_text("⏹️ प्रक्रिया रोकने का अनुरोध भेज दिया गया है।")


@app.on_message(filters.command("users") & filters.group)
async def users_command(client, message):
    try:
        count = await client.get_chat_members_count(message.chat.id)
        await message.reply_text(f"👥 इस ग्रुप में कुल {count} सदस्य हैं।")
    except RPCError:
        await message.reply_text("❌ सदस्य संख्या नहीं मिल सकी।")


@app.on_message(filters.command("id"))
async def id_command(client, message):
    lines = [f"ग्रुप ID: <code>{message.chat.id}</code>"]
    if message.from_user:
        lines.append(f"आपकी User ID: <code>{message.from_user.id}</code>")
    await message.reply_text("\n".join(lines), parse_mode=enums.ParseMode.HTML)


@app.on_message(filters.group & filters.text & ~filters.command(
    ["start", "help", "mention", "cancel", "users", "id", "question"]
))
async def check_quiz_answer(client, message):
    if not message.from_user or message.from_user.is_bot:
        return
    chat_id = message.chat.id
    expected = active_answers.get(chat_id, {}).get(message.from_user.id)
    if not expected:
        return
    received = normalize_answer(message.text)
    if received in expected["answers"]:
        quiz_scores.setdefault(chat_id, {})
        quiz_scores[chat_id][message.from_user.id] = quiz_scores[chat_id].get(message.from_user.id, 0) + 1
        score = quiz_scores[chat_id][message.from_user.id]
        await message.reply_text(f"✅ आपका जवाब सही है! 🎉\nआपका स्कोर: {score}")
    else:
        correct = expected["answers"][0]
        await message.reply_text(f"❌ आपका जवाब गलत है।\nसही जवाब: {correct}")
    # One question per member in each quiz run.
    active_answers[chat_id].pop(message.from_user.id, None)


if __name__ == "__main__":
    log.info("Pandey Mention Bot starting...")
    app.run()
