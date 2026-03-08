#!/usr/bin/env python3
"""
Professional Telegram Group Guardian Bot
- AI Spam Detection via OOPSpam
- Auto warn / mute / ban system
- Daily English lessons (Gemini + Pexels + Unsplash)
- Interactive quizzes
- Welcome messages
- Full admin moderation commands
"""

import asyncio
import logging
import random
import os
import json
import threading
from datetime import time
from http.server import HTTPServer, BaseHTTPRequestHandler

import aiohttp
import google.generativeai as genai
from telegram import (
    Update, ChatPermissions,
    InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from telegram.constants import ChatMemberStatus

# ──────────────────────────────────────────────────────────────────────────────
#  LOGGING  (stdout only — Railway captures it automatically)
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
#  CONFIG  (reads from Railway Environment Variables)
# ──────────────────────────────────────────────────────────────────────────────
def _env(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise RuntimeError(f"Missing environment variable: {name}")
    return val

TELEGRAM_TOKEN      = _env("TELEGRAM_TOKEN")
OOPSPAM_API_KEY     = _env("OOPSPAM_API_KEY")
PEXELS_API_KEY      = _env("PEXELS_API_KEY")
UNSPLASH_ACCESS_KEY = _env("UNSPLASH_ACCESS_KEY")
REMOVE_BG_API_KEY   = _env("REMOVE_BG_API_KEY")
GEMINI_API_KEY      = _env("GEMINI_API_KEY")

# ──────────────────────────────────────────────────────────────────────────────
#  GEMINI
# ──────────────────────────────────────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-1.5-flash")

# ──────────────────────────────────────────────────────────────────────────────
#  IN-MEMORY STORAGE
#  Replace with a real database (SQLite / PostgreSQL) for production persistence
# ──────────────────────────────────────────────────────────────────────────────
warned_users: dict   = {}   # key: (chat_id, user_id) → int count
group_settings: dict = {}   # key: chat_id → dict

def get_settings(chat_id: int) -> dict:
    return group_settings.setdefault(chat_id, {
        "spam_protection": True,
        "welcome_message": True,
        "anti_link":       False,
        "learning_posts":  True,
        "max_warnings":    3,
    })

# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        m = await context.bot.get_chat_member(
            update.effective_chat.id, update.effective_user.id
        )
        return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        return False

def escape(text: str) -> str:
    """Escape special chars for MarkdownV2."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text

# ══════════════════════════════════════════════════════════════════════════════
#  OOPSPAM — SPAM DETECTION
# ══════════════════════════════════════════════════════════════════════════════
async def check_spam(text: str, user_id: int) -> bool:
    url = "https://api.oopspam.com/v1/spamdetection"
    headers = {"X-Api-Key": OOPSPAM_API_KEY, "Content-Type": "application/json"}
    payload = {
        "checkForLength": True,
        "content": text,
        "senderIP": "1.1.1.1",
        "email": f"user{user_id}@telegram.user",
        "allowedLanguages": ["en", "ar"],
        "allowedCountries": ["us", "gb", "eg", "sa", "ae"],
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=payload, headers=headers,
                              timeout=aiohttp.ClientTimeout(total=6)) as r:
                if r.status == 200:
                    data = await r.json()
                    return data.get("Score", 0) >= 3
    except Exception as e:
        logger.warning(f"OOPSpam error: {e}")
    return False

# ══════════════════════════════════════════════════════════════════════════════
#  IMAGE SERVICES — Pexels + Unsplash fallback
# ══════════════════════════════════════════════════════════════════════════════
async def pexels_image(query: str) -> str | None:
    url = f"https://api.pexels.com/v1/search?query={query}&per_page=15&orientation=landscape"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers={"Authorization": PEXELS_API_KEY},
                             timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    photos = (await r.json()).get("photos", [])
                    if photos:
                        return random.choice(photos)["src"]["large"]
    except Exception as e:
        logger.warning(f"Pexels error: {e}")
    return None

async def unsplash_image(query: str) -> str | None:
    url = f"https://api.unsplash.com/photos/random?query={query}&orientation=landscape"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers={"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"},
                             timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    return (await r.json())["urls"]["regular"]
    except Exception as e:
        logger.warning(f"Unsplash error: {e}")
    return None

async def get_image(topic: str) -> str | None:
    return await pexels_image(topic) or await unsplash_image(topic)

# ══════════════════════════════════════════════════════════════════════════════
#  GEMINI — ENGLISH LESSON GENERATOR
# ══════════════════════════════════════════════════════════════════════════════
CATEGORIES = [
    "idioms", "phrasal verbs", "vocabulary words", "grammar tips",
    "common mistakes", "pronunciation tips", "business English",
    "slang expressions", "collocations", "English proverbs",
    "linking words", "prepositions", "modal verbs", "conditionals"
]

async def generate_lesson() -> dict | None:
    category = random.choice(CATEGORIES)
    prompt = f"""
You are an expert English teacher. Create an engaging Telegram lesson post about: {category}

Respond ONLY with valid JSON — no markdown fences, no extra text:
{{
  "category": "{category}",
  "title": "short catchy title",
  "main_word_or_phrase": "the focus item",
  "definition": "clear simple definition",
  "example_sentences": ["example 1", "example 2", "example 3"],
  "pro_tip": "one memorable practical tip",
  "emoji": "one relevant emoji",
  "image_search_query": "2-3 word search query for a relevant image",
  "difficulty": "Beginner or Intermediate or Advanced",
  "quiz_question": "a multiple-choice question testing this lesson",
  "quiz_options": ["A) ...", "B) ...", "C) ...", "D) ..."],
  "quiz_answer": "A or B or C or D"
}}
"""
    try:
        response = await asyncio.to_thread(gemini_model.generate_content, prompt)
        raw = response.text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return None

def format_lesson(lesson: dict) -> str:
    lines = [
        f"🌟 *Daily English Lesson* 🌟",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"{lesson['emoji']} *{escape(lesson['title'])}*",
        f"📚 _{escape(lesson['category'].title())}_ \\| 🎯 _{lesson['difficulty']}_",
        f"",
        f"🔤 *{escape(lesson['main_word_or_phrase'])}*",
        f"",
        f"📖 *Definition:*",
        f"{escape(lesson['definition'])}",
        f"",
        f"✏️ *Examples:*",
    ]
    for i, ex in enumerate(lesson.get("example_sentences", []), 1):
        lines.append(f"  {i}\\. _{escape(ex)}_")
    lines += [
        f"",
        f"💡 *Pro Tip:*",
        f"{escape(lesson['pro_tip'])}",
        f"",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🧠 *Quick Quiz:*",
        f"{escape(lesson['quiz_question'])}",
        f"",
    ]
    for opt in lesson.get("quiz_options", []):
        lines.append(f"  {escape(opt)}")
    lines += [
        f"",
        f"_Tap your answer below\\! 👇_",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🤖 _Powered by AI English Bot_",
    ]
    return "\n".join(lines)

async def send_lesson(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    lesson = await generate_lesson()
    if not lesson:
        logger.warning(f"Could not generate lesson for chat {chat_id}")
        return

    image_url = await get_image(lesson.get("image_search_query", "english learning"))
    text = format_lesson(lesson)
    answer = lesson.get("quiz_answer", "A")

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("A", callback_data=f"quiz_A_{answer}"),
        InlineKeyboardButton("B", callback_data=f"quiz_B_{answer}"),
        InlineKeyboardButton("C", callback_data=f"quiz_C_{answer}"),
        InlineKeyboardButton("D", callback_data=f"quiz_D_{answer}"),
    ]])

    try:
        if image_url:
            await context.bot.send_photo(
                chat_id=chat_id, photo=image_url,
                caption=text, parse_mode="MarkdownV2",
                reply_markup=keyboard
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id, text=text,
                parse_mode="MarkdownV2", reply_markup=keyboard
            )
        logger.info(f"Lesson sent → chat {chat_id}")
    except Exception as e:
        logger.error(f"Failed to send lesson to {chat_id}: {e}")
        try:
            fallback = (
                f"📚 Daily English Lesson\n\n"
                f"📌 {lesson.get('title')}\n\n"
                f"🔤 {lesson.get('main_word_or_phrase')}\n"
                f"📖 {lesson.get('definition')}\n\n"
                f"💡 Tip: {lesson.get('pro_tip')}"
            )
            await context.bot.send_message(chat_id=chat_id, text=fallback)
        except Exception:
            pass

# ══════════════════════════════════════════════════════════════════════════════
#  SCHEDULED JOB
# ══════════════════════════════════════════════════════════════════════════════
async def daily_lesson_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Running daily lesson job...")
    for chat_id, settings in list(group_settings.items()):
        if settings.get("learning_posts", True):
            delay = random.randint(0, 1800)   # spread posts over 30 min
            await asyncio.sleep(delay)
            await send_lesson(context, chat_id)

# ══════════════════════════════════════════════════════════════════════════════
#  COMMAND HANDLERS
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "➕ Add me to your group",
            url=f"https://t.me/{(await context.bot.get_me()).username}?startgroup=true"
        )
    ], [
        InlineKeyboardButton("📖 Help", callback_data="show_help"),
        InlineKeyboardButton("📚 Get a Lesson", callback_data="get_lesson"),
    ]])
    text = (
        "👋 *Hello\\! I'm your Group Guardian Bot* 🤖\n\n"
        "🛡️ *Protection:*\n"
        "• AI spam detection \\(OOPSpam\\)\n"
        "• Auto warn → mute → ban\n"
        "• Anti\\-link filter\n"
        "• Welcome new members\n\n"
        "📚 *Daily English Lessons:*\n"
        "• AI\\-generated with Gemini\n"
        "• Beautiful images \\(Pexels/Unsplash\\)\n"
        "• Interactive quiz buttons\n"
        "• Posted daily at 9:00 AM UTC\n\n"
        "➕ *Add me to your group and make me Admin\\!*"
    )
    await update.message.reply_text(text, parse_mode="MarkdownV2", reply_markup=keyboard)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *Commands*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "👮 *Moderation \\(Admins only\\):*\n"
        "`/warn` — Warn a user \\(reply to msg\\)\n"
        "`/mute` — Mute a user\n"
        "`/unmute` — Unmute a user\n"
        "`/ban` — Ban a user\n"
        "`/kick` — Kick a user\n"
        "`/warnings` — Check user's warnings\n"
        "`/clearwarns` — Clear user's warnings\n\n"
        "⚙️ *Settings \\(Admins only\\):*\n"
        "`/settings` — Open settings panel\n\n"
        "📚 *Lessons:*\n"
        "`/lesson` — Get a lesson now\n\n"
        "📊 *Info:*\n"
        "`/stats` — Group statistics\n"
        "`/id` — Get user or chat ID\n"
        "`/help` — Show this menu\n"
    )
    if update.callback_query:
        await update.callback_query.message.edit_text(text, parse_mode="MarkdownV2")
    else:
        await update.message.reply_text(text, parse_mode="MarkdownV2")

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("⚠️ Use this command inside a group.")
        return
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Admins only.")
        return

    chat_id = update.effective_chat.id
    s = get_settings(chat_id)

    def btn(label: str, key: str):
        icon = "✅" if s.get(key) else "❌"
        return InlineKeyboardButton(f"{icon} {label}", callback_data=f"toggle_{key}_{chat_id}")

    keyboard = InlineKeyboardMarkup([
        [btn("Spam Protection", "spam_protection"), btn("Welcome Message", "welcome_message")],
        [btn("Anti-Link Filter", "anti_link"),       btn("Daily Lessons",   "learning_posts")],
        [InlineKeyboardButton(f"⚠️ Max Warnings: {s['max_warnings']}  (tap to change)",
                              callback_data=f"maxwarn_{chat_id}")],
        [InlineKeyboardButton("❌ Close", callback_data="close")],
    ])
    text = f"⚙️ *Settings — {escape(update.effective_chat.title or '')}*"
    await update.message.reply_text(text, parse_mode="MarkdownV2", reply_markup=keyboard)

async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Admins only.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("↩️ Reply to a message to warn the user.")
        return

    chat_id = update.effective_chat.id
    s = get_settings(chat_id)
    target = update.message.reply_to_message.from_user
    reason = " ".join(context.args) if context.args else "No reason given"
    key = (chat_id, target.id)
    warned_users[key] = warned_users.get(key, 0) + 1
    count = warned_users[key]
    max_w = s.get("max_warnings", 3)

    if count >= max_w:
        await context.bot.ban_chat_member(chat_id, target.id)
        del warned_users[key]
        await update.message.reply_text(
            f"🔨 [{target.first_name}](tg://user?id={target.id}) "
            f"has been **banned** after {max_w} warnings.\nReason: {reason}",
            parse_mode="Markdown"
        )
    else:
        remaining = max_w - count
        await update.message.reply_text(
            f"⚠️ [{target.first_name}](tg://user?id={target.id}) — "
            f"Warning **{count}/{max_w}**\n"
            f"Reason: {reason}\n"
            f"{'⛔ Next warning = ban!' if remaining == 1 else f'{remaining} warnings remaining.'}",
            parse_mode="Markdown"
        )

async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    if not update.message.reply_to_message:
        await update.message.reply_text("↩️ Reply to a message to mute the user.")
        return
    target = update.message.reply_to_message.from_user
    await context.bot.restrict_chat_member(
        update.effective_chat.id, target.id,
        ChatPermissions(can_send_messages=False)
    )
    await update.message.reply_text(
        f"🔇 [{target.first_name}](tg://user?id={target.id}) has been muted.",
        parse_mode="Markdown"
    )

async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    if not update.message.reply_to_message:
        await update.message.reply_text("↩️ Reply to a message to unmute.")
        return
    target = update.message.reply_to_message.from_user
    await context.bot.restrict_chat_member(
        update.effective_chat.id, target.id,
        ChatPermissions(
            can_send_messages=True, can_send_media_messages=True,
            can_send_polls=True, can_send_other_messages=True,
            can_add_web_page_previews=True
        )
    )
    await update.message.reply_text(
        f"🔊 [{target.first_name}](tg://user?id={target.id}) has been unmuted.",
        parse_mode="Markdown"
    )

async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    if not update.message.reply_to_message:
        await update.message.reply_text("↩️ Reply to a message to ban the user.")
        return
    target = update.message.reply_to_message.from_user
    reason = " ".join(context.args) if context.args else "No reason given"
    await context.bot.ban_chat_member(update.effective_chat.id, target.id)
    await update.message.reply_text(
        f"🔨 [{target.first_name}](tg://user?id={target.id}) has been **banned**.\nReason: {reason}",
        parse_mode="Markdown"
    )

async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    if not update.message.reply_to_message:
        await update.message.reply_text("↩️ Reply to a message to kick the user.")
        return
    target = update.message.reply_to_message.from_user
    await context.bot.ban_chat_member(update.effective_chat.id, target.id)
    await context.bot.unban_chat_member(update.effective_chat.id, target.id)
    await update.message.reply_text(
        f"👢 [{target.first_name}](tg://user?id={target.id}) has been kicked.",
        parse_mode="Markdown"
    )

async def cmd_warnings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("↩️ Reply to a message to check warnings.")
        return
    target = update.message.reply_to_message.from_user
    key = (update.effective_chat.id, target.id)
    count = warned_users.get(key, 0)
    max_w = get_settings(update.effective_chat.id).get("max_warnings", 3)
    await update.message.reply_text(
        f"📊 [{target.first_name}](tg://user?id={target.id}) — **{count}/{max_w}** warnings.",
        parse_mode="Markdown"
    )

async def cmd_clearwarns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    if not update.message.reply_to_message:
        await update.message.reply_text("↩️ Reply to a message to clear warnings.")
        return
    target = update.message.reply_to_message.from_user
    key = (update.effective_chat.id, target.id)
    warned_users.pop(key, None)
    await update.message.reply_text(
        f"✅ Warnings cleared for [{target.first_name}](tg://user?id={target.id}).",
        parse_mode="Markdown"
    )

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [
        f"👤 Your ID: `{update.effective_user.id}`",
        f"💬 Chat ID: `{update.effective_chat.id}`",
    ]
    if update.message.reply_to_message:
        t = update.message.reply_to_message.from_user
        lines.append(f"👤 {t.first_name}'s ID: `{t.id}`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    count = await context.bot.get_chat_member_count(chat_id)
    active_warns = sum(1 for (c, _) in warned_users if c == chat_id)
    s = get_settings(chat_id)
    await update.message.reply_text(
        f"📊 *Group Statistics*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👥 Members: `{count}`\n"
        f"⚠️ Active warnings: `{active_warns}`\n"
        f"🛡️ Spam protection: {'Active ✅' if s['spam_protection'] else 'Off ❌'}\n"
        f"📚 Daily lessons: {'Active ✅' if s['learning_posts'] else 'Off ❌'}",
        parse_mode="Markdown"
    )

async def cmd_lesson(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Generating your English lesson, please wait...")
    await send_lesson(context, update.effective_chat.id)
    try:
        await msg.delete()
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════════════════════
#  CALLBACK QUERY HANDLER
# ══════════════════════════════════════════════════════════════════════════════
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data  = query.data

    # ── Quiz answer ──────────────────────────────────────────────────────────
    if data.startswith("quiz_"):
        parts   = data.split("_")
        chosen  = parts[1]
        correct = parts[2]
        if chosen == correct:
            await query.answer(f"✅ Correct! '{chosen}' is right! 🎉", show_alert=True)
        else:
            await query.answer(f"❌ Wrong! Correct answer is '{correct}'. Keep going! 💪", show_alert=True)
        return

    # ── Settings toggle ───────────────────────────────────────────────────────
    if data.startswith("toggle_"):
        _, key, chat_id_str = data.split("_", 2)
        chat_id = int(chat_id_str)
        s = get_settings(chat_id)
        s[key] = not s.get(key, False)
        state = "enabled ✅" if s[key] else "disabled ❌"
        await query.answer(f"{key.replace('_',' ').title()} {state}")
        # Refresh the settings keyboard
        def btn(label: str, k: str):
            icon = "✅" if s.get(k) else "❌"
            return InlineKeyboardButton(f"{icon} {label}", callback_data=f"toggle_{k}_{chat_id}")
        keyboard = InlineKeyboardMarkup([
            [btn("Spam Protection", "spam_protection"), btn("Welcome Message", "welcome_message")],
            [btn("Anti-Link Filter", "anti_link"),       btn("Daily Lessons",   "learning_posts")],
            [InlineKeyboardButton(f"⚠️ Max Warnings: {s['max_warnings']}",
                                  callback_data=f"maxwarn_{chat_id}")],
            [InlineKeyboardButton("❌ Close", callback_data="close")],
        ])
        try:
            await query.message.edit_reply_markup(reply_markup=keyboard)
        except Exception:
            pass
        return

    # ── Max warnings cycle: 2 → 3 → 4 → 5 → 2 ───────────────────────────────
    if data.startswith("maxwarn_"):
        chat_id = int(data.split("_")[1])
        s = get_settings(chat_id)
        s["max_warnings"] = (s["max_warnings"] % 5) + 1   # cycles 1→5
        if s["max_warnings"] < 2:
            s["max_warnings"] = 2
        await query.answer(f"Max warnings set to {s['max_warnings']}")
        def btn(label: str, k: str):
            icon = "✅" if s.get(k) else "❌"
            return InlineKeyboardButton(f"{icon} {label}", callback_data=f"toggle_{k}_{chat_id}")
        keyboard = InlineKeyboardMarkup([
            [btn("Spam Protection", "spam_protection"), btn("Welcome Message", "welcome_message")],
            [btn("Anti-Link Filter", "anti_link"),       btn("Daily Lessons",   "learning_posts")],
            [InlineKeyboardButton(f"⚠️ Max Warnings: {s['max_warnings']}  (tap to change)",
                                  callback_data=f"maxwarn_{chat_id}")],
            [InlineKeyboardButton("❌ Close", callback_data="close")],
        ])
        try:
            await query.message.edit_reply_markup(reply_markup=keyboard)
        except Exception:
            pass
        return

    # ── Help button ───────────────────────────────────────────────────────────
    if data == "show_help":
        await cmd_help(update, context)
        return

    # ── Inline lesson request ─────────────────────────────────────────────────
    if data == "get_lesson":
        await query.answer("Generating lesson... 📚")
        await send_lesson(context, query.message.chat_id)
        return

    # ── Close / delete message ────────────────────────────────────────────────
    if data == "close":
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass
        return

# ══════════════════════════════════════════════════════════════════════════════
#  MESSAGE HANDLER — SPAM + ANTI-LINK
# ══════════════════════════════════════════════════════════════════════════════
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    if update.effective_chat.type == "private":
        return

    chat_id = update.effective_chat.id
    user    = update.effective_user
    text    = update.message.text or update.message.caption or ""
    s       = get_settings(chat_id)

    if await is_admin(update, context):
        return   # never act on admins

    # ── Anti-link ─────────────────────────────────────────────────────────────
    if s.get("anti_link") and any(p in text for p in ("http://", "https://", "t.me/")):
        try:
            await update.message.delete()
            await context.bot.send_message(
                chat_id,
                f"🔗 Links are not allowed here, "
                f"[{user.first_name}](tg://user?id={user.id})!",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Anti-link delete failed: {e}")
        return

    # ── Spam detection ────────────────────────────────────────────────────────
    if s.get("spam_protection") and len(text) > 10:
        is_spam = await check_spam(text, user.id)
        if is_spam:
            logger.info(f"Spam from user {user.id}: {text[:60]}")
            try:
                await update.message.delete()
            except Exception:
                pass

            key   = (chat_id, user.id)
            warned_users[key] = warned_users.get(key, 0) + 1
            count = warned_users[key]
            max_w = s.get("max_warnings", 3)

            if count >= max_w:
                try:
                    await context.bot.ban_chat_member(chat_id, user.id)
                    del warned_users[key]
                    await context.bot.send_message(
                        chat_id,
                        f"🔨 [{user.first_name}](tg://user?id={user.id}) "
                        f"was **banned** for repeated spam.",
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.error(f"Ban failed: {e}")
            else:
                try:
                    await context.bot.send_message(
                        chat_id,
                        f"🚫 Spam detected! [{user.first_name}](tg://user?id={user.id}) "
                        f"— warning **{count}/{max_w}**.",
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.error(f"Warn message failed: {e}")

# ══════════════════════════════════════════════════════════════════════════════
#  WELCOME NEW MEMBERS
# ══════════════════════════════════════════════════════════════════════════════
async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.new_chat_members:
        return
    s = get_settings(update.effective_chat.id)
    if not s.get("welcome_message"):
        return

    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📚 Get Today's Lesson", callback_data="get_lesson")
        ]])
        try:
            await update.message.reply_text(
                f"👋 Welcome, [{member.first_name}](tg://user?id={member.id})!\n\n"
                f"🌟 Glad you joined *{update.effective_chat.title}*\n"
                f"📌 Please respect the group rules.\n"
                f"🤖 I'm here to keep things safe and share daily English lessons!",
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        except Exception as e:
            logger.warning(f"Welcome message failed: {e}")

# ══════════════════════════════════════════════════════════════════════════════
#  HEALTH CHECK SERVER  (keeps Railway service alive)
# ══════════════════════════════════════════════════════════════════════════════
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass  # silence HTTP logs

def start_health_server():
    port = int(os.getenv("PORT", 8080))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    # Start health check server in background
    threading.Thread(target=start_health_server, daemon=True).start()
    logger.info("Health check server started")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # ── Commands ──────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("help",       cmd_help))
    app.add_handler(CommandHandler("settings",   cmd_settings))
    app.add_handler(CommandHandler("warn",       cmd_warn))
    app.add_handler(CommandHandler("mute",       cmd_mute))
    app.add_handler(CommandHandler("unmute",     cmd_unmute))
    app.add_handler(CommandHandler("ban",        cmd_ban))
    app.add_handler(CommandHandler("kick",       cmd_kick))
    app.add_handler(CommandHandler("warnings",   cmd_warnings))
    app.add_handler(CommandHandler("clearwarns", cmd_clearwarns))
    app.add_handler(CommandHandler("id",         cmd_id))
    app.add_handler(CommandHandler("stats",      cmd_stats))
    app.add_handler(CommandHandler("lesson",     cmd_lesson))

    # ── Callbacks ─────────────────────────────────────────────────────────────
    app.add_handler(CallbackQueryHandler(handle_callback))

    # ── Messages ──────────────────────────────────────────────────────────────
    app.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_member
    ))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, handle_message
    ))

    # ── Scheduled daily lesson at 09:00 UTC ───────────────────────────────────
    app.job_queue.run_daily(
        daily_lesson_job,
        time=time(hour=9, minute=0, second=0)
    )

    logger.info("🤖 Bot is running on Railway!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
