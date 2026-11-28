import asyncio
import html
import json
import logging
import os
import random
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pyrogram import Client, filters, enums
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

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
STATS_DB = BASE_DIR / "pandey_stats.sqlite3"
INDIA_TZ = ZoneInfo("Asia/Kolkata")

try:
    with QUESTIONS_FILE.open("r", encoding="utf-8") as f:
        QUESTION_BANK = json.load(f)
except Exception:
    log.exception("questions.json load nahi hua")
    QUESTION_BANK = []

# One active sending task per group.
running_jobs = {}
# chat_id -> bot_question_message_id -> question state (answer is tied to that exact message)
question_messages = {}
# chat_id -> user_id -> score
quiz_scores = {}



def init_stats_db():
    """Create persistent per-group message counters."""
    with sqlite3.connect(STATS_DB) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS member_stats (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                username TEXT,
                total INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_stats (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, user_id, day)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_chat_day ON daily_stats(chat_id, day)")


def record_group_message(chat_id: int, user):
    """Count any message type from a real group member."""
    if not user or user.is_bot or user.is_deleted:
        return
    name = member_name(user)
    username = user.username or ""
    today = datetime.now(INDIA_TZ).date().isoformat()
    with sqlite3.connect(STATS_DB) as conn:
        conn.execute("""
            INSERT INTO member_stats(chat_id, user_id, name, username, total)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                name=excluded.name,
                username=excluded.username,
                total=member_stats.total + 1
        """, (chat_id, user.id, name, username))
        conn.execute("""
            INSERT INTO daily_stats(chat_id, user_id, day, count)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(chat_id, user_id, day) DO UPDATE SET
                count=daily_stats.count + 1
        """, (chat_id, user.id, today))


def get_user_counts(chat_id: int, user_id: int):
    today = datetime.now(INDIA_TZ).date()
    week_start = (today - timedelta(days=6)).isoformat()
    today_text = today.isoformat()
    with sqlite3.connect(STATS_DB) as conn:
        row = conn.execute(
            "SELECT total FROM member_stats WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
        day_row = conn.execute(
            "SELECT count FROM daily_stats WHERE chat_id=? AND user_id=? AND day=?",
            (chat_id, user_id, today_text),
        ).fetchone()
        week_row = conn.execute(
            "SELECT COALESCE(SUM(count), 0) FROM daily_stats WHERE chat_id=? AND user_id=? AND day>=?",
            (chat_id, user_id, week_start),
        ).fetchone()
    return (row[0] if row else 0, day_row[0] if day_row else 0, week_row[0] if week_row else 0)


def get_top_members(chat_id: int, period: str, limit: int = 10):
    today = datetime.now(INDIA_TZ).date()
    with sqlite3.connect(STATS_DB) as conn:
        if period == "day":
            rows = conn.execute("""
                SELECT m.user_id, m.name, m.username, d.count
                FROM daily_stats d JOIN member_stats m
                  ON m.chat_id=d.chat_id AND m.user_id=d.user_id
                WHERE d.chat_id=? AND d.day=?
                ORDER BY d.count DESC, m.name COLLATE NOCASE ASC LIMIT ?
            """, (chat_id, today.isoformat(), limit)).fetchall()
            title = "आज का Top 10"
        elif period == "week":
            week_start = (today - timedelta(days=6)).isoformat()
            rows = conn.execute("""
                SELECT m.user_id, m.name, m.username, SUM(d.count) AS total
                FROM daily_stats d JOIN member_stats m
                  ON m.chat_id=d.chat_id AND m.user_id=d.user_id
                WHERE d.chat_id=? AND d.day>=?
                GROUP BY m.user_id, m.name, m.username
                ORDER BY total DESC, m.name COLLATE NOCASE ASC LIMIT ?
            """, (chat_id, week_start, limit)).fetchall()
            title = "पिछले 7 दिनों का Top 10"
        else:
            rows = conn.execute("""
                SELECT user_id, name, username, total
                FROM member_stats WHERE chat_id=?
                ORDER BY total DESC, name COLLATE NOCASE ASC LIMIT ?
            """, (chat_id, limit)).fetchall()
            title = "All Time Top 10"
    return title, rows


def build_stats_text(chat_id: int, period: str, viewer_id: int = None):
    if period == "mine":
        if not viewer_id:
            return "अपनी Stats देखने के लिए ग्रुप में कमांड चलाएँ।"
        total, today_count, week_count = get_user_counts(chat_id, viewer_id)
        return (
            "👤 <b>Your Message Stats</b>\n\n"
            f"📅 आज: <b>{today_count}</b>\n"
            f"📆 पिछले 7 दिन: <b>{week_count}</b>\n"
            f"🌍 All Time: <b>{total}</b>"
        )

    title, rows = get_top_members(chat_id, period)
    if not rows:
        return "📊 अभी कोई Message Stats उपलब्ध नहीं है। Bot के रिकॉर्ड शुरू होने के बाद Messages यहाँ दिखेंगे।"
    lines = [f"🏆 <b>{html.escape(title)}</b>\n"]
    medals = ["🥇", "🥈", "🥉"]
    for index, (user_id, name, username, count) in enumerate(rows, start=1):
        marker = medals[index - 1] if index <= 3 else f"{index}."
        safe_name = html.escape(name or username or str(user_id))
        lines.append(f'{marker} <a href="tg://user?id={user_id}">{safe_name}</a> — <b>{count}</b>')
    return "\n".join(lines)


def stats_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 Today", callback_data="stats:day"),
            InlineKeyboardButton("📆 Weekly", callback_data="stats:week"),
        ],
        [
            InlineKeyboardButton("🏆 All Time", callback_data="stats:all"),
            InlineKeyboardButton("👤 My Stats", callback_data="stats:mine"),
        ],
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="stats:refresh"),
            InlineKeyboardButton("❌ Close", callback_data="stats:close"),
        ],
    ])


async def send_admin_mentions(client, chat_id: int, cancel_event):
    batch = []
    try:
        async for member in client.get_chat_members(
            chat_id, filter=enums.ChatMembersFilter.ADMINISTRATORS
        ):
            if cancel_event.is_set():
                break
            user = member.user
            if not user or user.is_bot or user.is_deleted:
                continue
            label = html.escape(member_name(user))
            batch.append(f'<a href="tg://user?id={user.id}">{label}</a>')
            if len(batch) >= 5:
                await safe_send(client, chat_id, "👮 <b>Group Admins</b>\n" + "\n".join(batch))
                batch.clear()
                await asyncio.sleep(1)
        if batch and not cancel_event.is_set():
            await safe_send(client, chat_id, "👮 <b>Group Admins</b>\n" + "\n".join(batch))
        if cancel_event.is_set():
            await safe_send(client, chat_id, "⏹️ Admin tagging रोक दी गई।")
        else:
            await safe_send(client, chat_id, "✅ उपलब्ध Group Admins को Mention कर दिया गया।")
    except FloodWait as e:
        await safe_send(client, chat_id, f"⏳ Telegram FloodWait: {e.value} सेकंड।")
    except RPCError:
        log.exception("Admin mention RPC error")
        await safe_send(client, chat_id, "❌ Admin list नहीं मिल सकी। Bot के admin अधिकार जाँचें।")
    finally:
        running_jobs.pop(chat_id, None)



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


init_stats_db()


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
        "/tagadmin - केवल ग्रुप Admins को Mention करें (Admin only)\n"
        "/stats - Message Statistics का Button Menu\n"
        "/topday /topweek /topall - Top 10 Leaderboard\n"
        "/mystats - अपने Messages की गिनती\n"
        "/cancel - चल रही Mention/Question प्रक्रिया रोकें\n"
        "/users - ग्रुप में सदस्यों की संख्या\n"
        "/id - ग्रुप और यूजर ID देखें\n"
        "/question - हर सदस्य को अलग Quiz सवाल भेजें\n\n"
        "⚠️ /mention, /tagadmin, /cancel और /question केवल ग्रुप एडमिन चला सकते हैं।\n"
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


@app.on_message(filters.command("tagadmin") & filters.group)
async def tagadmin_command(client, message):
    if not await admin_only(client, message):
        return
    chat_id = message.chat.id
    if chat_id in running_jobs:
        await message.reply_text("एक प्रक्रिया पहले से चल रही है। पहले /cancel करें।")
        return
    cancel_event = asyncio.Event()
    task = asyncio.create_task(send_admin_mentions(client, chat_id, cancel_event))
    running_jobs[chat_id] = {"task": task, "cancel": cancel_event}
    await message.reply_text("👮 केवल Group Admins को Mention किया जा रहा है...")



def normalize_answer(value: str) -> str:
    if not QUESTION_BANK:
        return None
    return random.choice(QUESTION_BANK)


def prepare_options(item):
    """Return four unique choices and the normalized accepted answers."""
    accepted = item.get("answers") or []
    correct = item.get("correct_answer") or (accepted[0] if accepted else "")
    accepted_norm = {normalize_answer(a) for a in accepted if normalize_answer(a)}
    accepted_norm.add(normalize_answer(correct))

    options = list(item.get("options") or [])
    if not any(normalize_answer(x) in accepted_norm for x in options):
        options.append(correct)

    # Deduplicate choices without changing their visible wording/order.
    unique = []
    seen = set()
    for option in options:
        option = str(option).strip()
        key = normalize_answer(option)
        if option and key and key not in seen:
            unique.append(option)
            seen.add(key)

    # Fill missing choices from the question bank's other answers/options.
    distractors = []
    for other in QUESTION_BANK:
        for candidate in (other.get("options") or []) + (other.get("answers") or []):
            candidate = str(candidate).strip()
            key = normalize_answer(candidate)
            if candidate and key and key not in seen and key not in accepted_norm:
                distractors.append(candidate)
                seen.add(key)
    random.shuffle(distractors)
    unique.extend(distractors[:max(0, 4 - len(unique))])

    # A valid MCQ always has four choices; rare sparse data gets neutral fillers.
    fillers = ["इनमें से कोई नहीं", "उपरोक्त में से कोई नहीं", "इनमें से कोई भी नहीं", "अन्य"]
    for filler in fillers:
        if len(unique) >= 4:
            break
        if normalize_answer(filler) not in seen:
            unique.append(filler)
            seen.add(normalize_answer(filler))
    unique = unique[:4]
    random.shuffle(unique)
    return unique, accepted_norm, str(correct)


async def send_user_question(client, chat_id: int, user, item=None):
    item = item or choose_question()
    if not item:
        return None
    options, accepted, correct = prepare_options(item)
    if len(options) != 4:
        log.warning("Question has fewer than four options: %s", item.get("question"))
        return None

    qtext = (
        f"🧠 <b>{html.escape(str(item.get('category', 'Quiz')))}</b>\n"
        f'<a href="tg://user?id={user.id}">{html.escape(member_name(user))}</a>, '
        f"आपका सवाल:\n<b>{html.escape(str(item.get('question', '')))}</b>\n\n"
        "नीचे दिए गए विकल्पों में से जवाब चुनें या इस सवाल के जवाब में लिखें।"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{chr(65+i)}. {option}", callback_data=f"quiz:{i}")]
        for i, option in enumerate(options)
    ])
    sent_message = await client.send_message(
        chat_id,
        qtext,
        parse_mode=enums.ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    question_messages.setdefault(chat_id, {})[sent_message.id] = {
        "question": str(item.get("question", "")),
        "category": str(item.get("category", "सामान्य ज्ञान")),
        "options": options,
        "answers": accepted,
        "correct": correct,
        "answered": False,
    }
    return sent_message


async def send_next_user_question(client, chat_id: int, user):
    item = choose_question()
    if not item:
        await client.send_message(chat_id, "❌ questions.json में सवाल नहीं मिले।")
        return
    try:
        await send_user_question(client, chat_id, user, item)
    except FloodWait as e:
        await asyncio.sleep(int(e.value) + 1)
        await send_user_question(client, chat_id, user, item)


async def question_worker(client, chat_id: int, cancel_event):
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
            try:
                await send_user_question(client, chat_id, user, item)
                sent += 1
            except FloodWait as e:
                await asyncio.sleep(int(e.value) + 1)
                await send_user_question(client, chat_id, user, item)
                sent += 1
            await asyncio.sleep(1)
        if cancel_event.is_set():
            await safe_send(client, chat_id, f"⏹️ Quiz सवाल भेजना रोक दिया गया। {sent} सवाल भेजे गए।")
        else:
            await safe_send(client, chat_id, f"✅ {sent} अलग-अलग सवाल भेज दिए गए। किसी सवाल के बटन पर टैप करें या उसी सवाल का Reply करके जवाब लिखें।")
    except FloodWait as e:
        await safe_send(client, chat_id, f"⏳ Telegram FloodWait: {e.value} सेकंड। कुछ सवाल भेजे गए; बाद में फिर कोशिश करें।")
    except RPCError:
        log.exception("Question RPC error")
        await safe_send(client, chat_id, "❌ सवाल भेजते समय Telegram error आया। Bot के admin अधिकार जाँचें।")
    finally:
        running_jobs.pop(chat_id, None)


async def process_quiz_answer(client, chat_id, question_message_id, user, selected_answer):
    state = question_messages.get(chat_id, {}).get(question_message_id)
    if not state or state.get("answered"):
        return False

    state["answered"] = True
    is_correct = normalize_answer(selected_answer) in state["answers"]
    user_scores = quiz_scores.setdefault(chat_id, {})
    user_scores.setdefault(user.id, 0)
    if is_correct:
        user_scores[user.id] += 1
        feedback = f"✅ <a href=\"tg://user?id={user.id}\">{html.escape(member_name(user))}</a> का जवाब सही है! 🎉\nस्कोर: {user_scores[user.id]}"
    else:
        feedback = (
            f"❌ <a href=\"tg://user?id={user.id}\">{html.escape(member_name(user))}</a> का जवाब गलत है।\n"
            f"सही जवाब: <b>{html.escape(state['correct'])}</b>"
        )

    try:
        await client.send_message(
            chat_id,
            feedback,
            parse_mode=enums.ParseMode.HTML,
            reply_to_message_id=question_message_id,
        )
    except RPCError:
        log.exception("Quiz feedback send failed")
    await send_next_user_question(client, chat_id, user)
    return True


@app.on_callback_query(filters.regex(r"^quiz:[0-3]$"))
async def quiz_option_callback(client, query):
    if not query.message or not query.from_user:
        await query.answer("यह सवाल उपलब्ध नहीं है।", show_alert=True)
        return
    chat_id = query.message.chat.id
    state = question_messages.get(chat_id, {}).get(query.message.id)
    if not state or state.get("answered"):
        await query.answer("इस सवाल का जवाब पहले ही दिया जा चुका है।", show_alert=True)
        return
    option_index = int(query.data.split(":", 1)[1])
    options = state.get("options", [])
    if option_index >= len(options):
        await query.answer("यह विकल्प उपलब्ध नहीं है।", show_alert=True)
        return
    await query.answer("जवाब जाँचा जा रहा है…")
    await process_quiz_answer(client, chat_id, query.message.id, query.from_user, options[option_index])


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
    question_messages[chat_id] = {}
    await message.reply_text("🧠 Quiz शुरू! हर सदस्य को अलग सवाल और 4 विकल्प मिलेंगे।")


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


@app.on_message(filters.command("stats") & filters.group)
async def stats_command(client, message):
    await message.reply_text(
        "📊 <b>PANDEY GROUP STATS</b>\nनीचे से Leaderboard चुनें:",
        parse_mode=enums.ParseMode.HTML,
        reply_markup=stats_keyboard(),
    )


@app.on_message(filters.command("topday") & filters.group)
async def topday_command(client, message):
    await message.reply_text(
        build_stats_text(message.chat.id, "day"),
        parse_mode=enums.ParseMode.HTML,
        disable_web_page_preview=True,
    )


@app.on_message(filters.command("topweek") & filters.group)
async def topweek_command(client, message):
    await message.reply_text(
        build_stats_text(message.chat.id, "week"),
        parse_mode=enums.ParseMode.HTML,
        disable_web_page_preview=True,
    )


@app.on_message(filters.command("topall") & filters.group)
async def topall_command(client, message):
    await message.reply_text(
        build_stats_text(message.chat.id, "all"),
        parse_mode=enums.ParseMode.HTML,
        disable_web_page_preview=True,
    )


@app.on_message(filters.command("mystats") & filters.group)
async def mystats_command(client, message):
    if not message.from_user:
        return
    await message.reply_text(
        build_stats_text(message.chat.id, "mine", message.from_user.id),
        parse_mode=enums.ParseMode.HTML,
        disable_web_page_preview=True,
    )


@app.on_callback_query(filters.regex(r"^stats:(day|week|all|mine|refresh|close)$"))
async def stats_callback(client, query):
    if not query.message:
        await query.answer("यह Stats Menu उपलब्ध नहीं है।", show_alert=True)
        return
    if query.data == "stats:close":
        try:
            await query.message.delete()
        except RPCError:
            await query.message.edit_reply_markup(reply_markup=None)
        await query.answer("Menu बंद कर दिया गया।")
        return

    if query.data == "stats:refresh":
        period = "all"
    else:
        period = query.data.split(":", 1)[1]

    viewer_id = query.from_user.id if query.from_user else None
    text = build_stats_text(query.message.chat.id, period, viewer_id)
    await query.message.edit_text(
        text,
        parse_mode=enums.ParseMode.HTML,
        reply_markup=stats_keyboard(),
        disable_web_page_preview=True,
    )
    await query.answer()


@app.on_message(filters.group, group=1)
async def count_all_group_messages(client, message):
    # Counts text, media, stickers, GIFs, files, voice notes, polls, etc.
    # Service updates and anonymous-admin messages without a from_user are skipped.
    if message.from_user:
        try:
            record_group_message(message.chat.id, message.from_user)
        except Exception:
            log.exception("Message statistics update failed")



@app.on_message(filters.command("id"))
async def id_command(client, message):
    lines = [f"ग्रुप ID: <code>{message.chat.id}</code>"]
    if message.from_user:
        lines.append(f"आपकी User ID: <code>{message.from_user.id}</code>")
    await message.reply_text("\n".join(lines), parse_mode=enums.ParseMode.HTML)


@app.on_message(filters.group & filters.text & ~filters.command(
    ["start", "help", "mention", "tagadmin", "cancel", "users", "id", "question", "stats", "topday", "topweek", "topall", "mystats"]
))
async def check_quiz_answer(client, message):
    if not message.from_user or message.from_user.is_bot:
        return
    # Only replies to a bot message that is a recorded quiz question count.
    replied = message.reply_to_message
    if not replied or not replied.from_user or not replied.from_user.is_bot:
        return
    state = question_messages.get(message.chat.id, {}).get(replied.id)
    if not state or state.get("answered"):
        return
    await process_quiz_answer(
        client,
        message.chat.id,
        replied.id,
        message.from_user,
        message.text,
    )


if __name__ == "__main__":
    log.info("Pandey Mention Bot starting...")
    app.run()
