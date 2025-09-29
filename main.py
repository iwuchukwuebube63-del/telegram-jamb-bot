#!/usr/bin/env python3
# main.py
# Telegram bot with:
# - Paged university selector (3 pages)
# - Four aggregate methods + UNIZIK screening flow
# - Referral system (start with ref_<id>), +5 points to referrer
# - Balance and transaction history (SQLite)
# - Calculation consumes 1 point (adjustable)
#
# Requirements:
#   pip install python-telegram-bot aiosqlite
#
# Configure:
#   export BOT_TOKEN="your_bot_token_here"
#   (or set BOT_TOKEN in environment before running)
#
# Run:
#   python main.py

import os
import logging
import aiosqlite
from typing import List, Tuple, Optional
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# --- Configuration ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", None)
DB_PATH = os.environ.get("DB_PATH", "bot_data.sqlite")
CALC_COST = 1          # points deducted per calculation
REF_BONUS = 5          # points awarded per successful referral

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- University pages (3 pages) ---
UNIV_PAGES: List[List[str]] = [
    # Page 0
    ["UNILAG", "UNIBEN", "DELSU", "FUTO", "UNICAL", "UNIPORT", "UNIJOS", "UNILORIN"],
    # Page 1
    ["UNN", "ABU", "UI", "OAU", "COVENANT", "FUTA", "BUK", "FUPRE"],
    # Page 2
    ["LASU", "UNIOSUN", "AAUA", "UNIZIK", "PRIVATE1", "PRIVATE2", "POLY1", "COLLEGE1"],
]

# --- Mapping of university -> method key ---
# METHOD_1: JAMB + Post-UTME
# METHOD_2: JAMB + Post-UTME + O'Level
# METHOD_3: JAMB + O'Level only
# METHOD_4: Screening only / no aggregate
# UNIZIK: special UNIZIK screening flow
METHOD_MAP = {
    "UNILAG": "METHOD_1", "UNIBEN": "METHOD_1", "DELSU": "METHOD_1", "FUTO": "METHOD_1",
    "UNICAL": "METHOD_1", "UNIPORT": "METHOD_1", "UNIJOS": "METHOD_1", "UNILORIN": "METHOD_1",
    "UNN": "METHOD_2", "ABU": "METHOD_2", "UI": "METHOD_2", "OAU": "METHOD_2",
    "COVENANT": "METHOD_2", "FUTA": "METHOD_2", "BUK": "METHOD_2", "FUPRE": "METHOD_2",
    "LASU": "METHOD_3", "UNIOSUN": "METHOD_3", "AAUA": "METHOD_3",
    "UNIZIK": "UNIZIK",
    # defaults for others may be added/updated dynamically
}

# --- Database helpers ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                referred_by INTEGER,
                referred INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS balances (
                user_id INTEGER PRIMARY KEY,
                points INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tx (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                change INTEGER,
                reason TEXT,
                ts DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

async def ensure_user(user_id: int, username: Optional[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users(user_id, username) VALUES (?, ?)",
            (user_id, username or "")
        )
        await db.execute(
            "INSERT OR IGNORE INTO balances(user_id, points) VALUES (?, 0)",
            (user_id,)
        )
        await db.commit()

async def get_balance(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT points FROM balances WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row else 0

async def add_points(user_id: int, points: int, reason: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO tx(user_id, change, reason) VALUES (?, ?, ?)", (user_id, points, reason))
        await db.execute("INSERT OR IGNORE INTO balances(user_id, points) VALUES (?, 0)", (user_id,))
        await db.execute("UPDATE balances SET points = points + ? WHERE user_id = ?", (points, user_id))
        await db.commit()

async def get_history(user_id: int, limit: int = 25) -> List[Tuple[int, str, str]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT change, reason, ts FROM tx WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit)
        )
        rows = await cur.fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

async def set_referred(new_user_id: int, referrer_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT referred FROM users WHERE user_id = ?", (new_user_id,))
        row = await cur.fetchone()
        if row and row[0] == 1:
            return False
        await db.execute("UPDATE users SET referred_by = ?, referred = 1 WHERE user_id = ?", (referrer_id, new_user_id))
        await db.commit()
        return True

# --- UI helpers ---
def univ_page_keyboard(page_index: int) -> InlineKeyboardMarkup:
    buttons = []
    for name in UNIV_PAGES[page_index]:
        buttons.append([InlineKeyboardButton(name, callback_data=f"select_univ|{name}")])
    nav_row = []
    if page_index > 0:
        nav_row.append(InlineKeyboardButton("◀ Prev", callback_data=f"nav|{page_index-1}"))
    if page_index < len(UNIV_PAGES) - 1:
        nav_row.append(InlineKeyboardButton("Next ▶", callback_data=f"nav|{page_index+1}"))
    if nav_row:
        buttons.append(nav_row)
    buttons.append([
        InlineKeyboardButton("Refer", callback_data="show_refer"),
        InlineKeyboardButton("My Balance", callback_data="show_balance"),
        InlineKeyboardButton("History", callback_data="show_history")
    ])
    return InlineKeyboardMarkup(buttons)

# --- Calculation helpers ---
def calc_method_1(jamb: int, post_utme: int) -> float:
    return (jamb / 8.0) + (post_utme / 2.0)

def calc_method_2(jamb: int, post_utme: int, olevel_grades: List[str]) -> float:
    gmap = {"A1": 2.0, "B2": 1.8, "B3": 1.6, "C4": 1.4, "C5": 1.2, "C6": 1.0}
    olevel_score = sum(gmap.get(g.upper(), 0) for g in olevel_grades[:5])
    return (jamb / 8.0) + (post_utme / 2.0) + olevel_score

def calc_method_3(jamb: int, olevel_grades: List[str]) -> float:
    gmap = {"A1": 4.0, "B2": 3.6, "B3": 3.2, "C4": 2.8, "C5": 2.4, "C6": 2.0}
    olevel_score = sum(gmap.get(g.upper(), 0) for g in olevel_grades[:5])
    return (jamb / 8.0) + olevel_score

def calc_unizik(jamb: int, grades4: List[str], one_sitting: bool) -> float:
    # grade mapping as described earlier (A1=90,B2=80,...)
    gmap = {"A1": 90, "B2": 80, "B3": 70, "C4": 60, "C5": 55, "C6": 50}
    olevel_points = sum(gmap.get(g.upper(), 0) for g in grades4[:4])
    bonus = 10 if one_sitting else 0
    jamb_weighted = jamb * 0.7
    olevel_weighted = (olevel_points + bonus) * 0.3
    return jamb_weighted + olevel_weighted

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await init_db()
    await ensure_user(user.id, user.username)
    # handle referral token: /start ref_<id>
    args = context.args
    if args and args[0].startswith("ref_"):
        try:
            ref_id = int(args[0].split("_", 1)[1])
        except Exception:
            ref_id = None
        if ref_id and ref_id != user.id:
            ok = await set_referred(user.id, ref_id)
            if ok:
                await add_points(ref_id, REF_BONUS, f"Referral bonus for referring {user.id}")
                # notify referrer
                try:
                    await context.bot.send_message(
                        chat_id=ref_id,
                        text=f"🎉 You referred @{user.username or user.id}! +{REF_BONUS} calculations added to your balance."
                    )
                except Exception:
                    logger.info("Unable to notify referrer %s", ref_id)
                await update.message.reply_text("Thanks for starting with a referral link. Your referrer was rewarded.")
    # show first page
    await update.message.reply_text("Welcome! Select your university category:", reply_markup=univ_page_keyboard(0))

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data.startswith("nav|"):
        page = int(data.split("|", 1)[1])
        await query.edit_message_text("Choose your university:", reply_markup=univ_page_keyboard(page))
        return

    if data == "show_refer":
        user = update.effective_user
        me = await context.bot.get_me()
        ref_link = f"https://t.me/{me.username}?start=ref_{user.id}"
        text = f"Share this link to refer others:\n\n{ref_link}\n\nWhen someone starts the bot with that link, you get +{REF_BONUS} calculations."
        await query.edit_message_text(text)
        return

    if data == "show_balance":
        user_id = update.effective_user.id
        bal = await get_balance(user_id)
        await query.edit_message_text(f"Your balance: {bal} calculation(s).")
        return

    if data == "show_history":
        user_id = update.effective_user.id
        rows = await get_history(user_id, limit=25)
        if not rows:
            await query.edit_message_text("No transactions yet.")
            return
        lines = [f"{r[2]}: {'+' if r[0]>0 else ''}{r[0]} — {r[1]}" for r in rows]
        await query.edit_message_text("Recent transactions:\n" + "\n".join(lines))
        return

    if data.startswith("select_univ|"):
        uni = data.split("|", 1)[1]
        method = METHOD_MAP.get(uni, "METHOD_1")
        context.user_data.clear()
        context.user_data["selected_uni"] = uni
        context.user_data["selected_method"] = method
        # Begin appropriate flow
        await query.edit_message_text(f"You selected {uni}. Proceeding...")
        if method == "METHOD_1":
            context.user_data["state"] = "M1_JAMB"
            await context.bot.send_message(chat_id=update.effective_user.id, text="Enter your JAMB score (0-400):")
        elif method == "METHOD_2":
            context.user_data["state"] = "M2_JAMB"
            await context.bot.send_message(chat_id=update.effective_user.id, text="Enter your JAMB score (0-400):")
        elif method == "METHOD_3":
            context.user_data["state"] = "M3_JAMB"
            await context.bot.send_message(chat_id=update.effective_user.id, text="Enter your JAMB score (0-400):")
        elif method == "UNIZIK":
            context.user_data["state"] = "UNIZIK_JAMB"
            await context.bot.send_message(chat_id=update.effective_user.id, text="Enter your JAMB score (0-400):")
        else:
            await context.bot.send_message(chat_id=update.effective_user.id, text="Mapping missing for this university. Contact admin.")
        return

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    text = (update.message.text or "").strip()
    state = context.user_data.get("state")

    def parse_int(s: str) -> Optional[int]:
        try:
            return int(s)
        except Exception:
            return None

    # METHOD 1 flow
    if state == "M1_JAMB":
        jamb = parse_int(text)
        if jamb is None or not (0 <= jamb <= 400):
            await update.message.reply_text("Invalid JAMB. Enter an integer 0-400.")
            return
        context.user_data["jamb"] = jamb
        context.user_data["state"] = "M1_POST"
        await update.message.reply_text("Enter your Post-UTME score (0-100):")
        return

    if state == "M1_POST":
        post = parse_int(text)
        if post is None or not (0 <= post <= 100):
            await update.message.reply_text("Invalid Post-UTME. Enter an integer 0-100.")
            return
        jamb = context.user_data.get("jamb", 0)
        aggregate = calc_method_1(jamb, post)
        # deduct cost
        await ensure_user(user_id, user.username)
        await add_points(user_id, -CALC_COST, f"Calculation used for {context.user_data.get('selected_uni')}")
        await update.message.reply_text(f"Aggregate: {aggregate:.2f}%\n-{CALC_COST} point deducted from your balance.")
        context.user_data.clear()
        return

    # METHOD 2 flow
    if state == "M2_JAMB":
        jamb = parse_int(text)
        if jamb is None or not (0 <= jamb <= 400):
            await update.message.reply_text("Invalid JAMB. Enter an integer 0-400.")
            return
        context.user_data["jamb"] = jamb
        context.user_data["state"] = "M2_POST"
        await update.message.reply_text("Enter your Post-UTME score (0-100):")
        return

    if state == "M2_POST":
        post = parse_int(text)
        if post is None or not (0 <= post <= 100):
            await update.message.reply_text("Invalid Post-UTME. Enter an integer 0-100.")
            return
        context.user_data["post"] = post
        context.user_data["state"] = "M2_OLEVEL"
        await update.message.reply_text("Enter 5 O'Level grades separated by commas (e.g., A1,B2,B3,C4,C5):")
        return

    if state == "M2_OLEVEL":
        grades = [g.strip().upper() for g in text.split(",") if g.strip()]
        if len(grades) < 5:
            await update.message.reply_text("Please enter 5 grades (best 5).")
            return
        jamb = context.user_data.get("jamb", 0)
        post = context.user_data.get("post", 0)
        aggregate = calc_method_2(jamb, post, grades)
        await ensure_user(user_id, user.username)
        await add_points(user_id, -CALC_COST, f"Calculation used for {context.user_data.get('selected_uni')}")
        await update.message.reply_text(f"Aggregate: {aggregate:.2f}%\n-{CALC_COST} point deducted from your balance.")
        context.user_data.clear()
        return

    # METHOD 3 flow
    if state == "M3_JAMB":
        jamb = parse_int(text)
        if jamb is None or not (0 <= jamb <= 400):
            await update.message.reply_text("Invalid JAMB. Enter an integer 0-400.")
            return
        context.user_data["jamb"] = jamb
        context.user_data["state"] = "M3_OLEVEL"
        await update.message.reply_text("Enter 5 O'Level grades separated by commas (e.g., A1,B2,B3,C4,C5):")
        return

    if state == "M3_OLEVEL":
        grades = [g.strip().upper() for g in text.split(",") if g.strip()]
        if len(grades) < 5:
            await update.message.reply_text("Please enter 5 grades (best 5).")
            return
        jamb = context.user_data.get("jamb", 0)
        aggregate = calc_method_3(jamb, grades)
        await ensure_user(user_id, user.username)
        await add_points(user_id, -CALC_COST, f"Calculation used for {context.user_data.get('selected_uni')}")
        await update.message.reply_text(f"Aggregate: {aggregate:.2f}%\n-{CALC_COST} point deducted from your balance.")
        context.user_data.clear()
        return

    # UNIZIK flow: JAMB -> 4 O'Level -> one-sitting buttons
    if state == "UNIZIK_JAMB":
        jamb = parse_int(text)
        if jamb is None or not (0 <= jamb <= 400):
            await update.message.reply_text("Invalid JAMB. Enter an integer 0-400.")
            return
        context.user_data["jamb"] = jamb
        context.user_data["state"] = "UNIZIK_OLEVEL"
        await update.message.reply_text("Enter grades for the 4 JAMB subjects separated by commas (e.g., A1,B3,C4,B2):")
        return

    if state == "UNIZIK_OLEVEL":
        grades = [g.strip().upper() for g in text.split(",") if g.strip()]
        if len(grades) != 4:
            await update.message.reply_text("Please enter exactly 4 grades.")
            return
        context.user_data["olevel_grades"] = grades
        context.user_data["state"] = "UNIZIK_SITTING"
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Yes (one sitting)", callback_data="unizik_sit|yes"),
                InlineKeyboardButton("No (multiple sittings)", callback_data="unizik_sit|no"),
            ]
        ])
        await update.message.reply_text("Were the 4 credits obtained in a single sitting?", reply_markup=keyboard)
        return

    # fallback
    await update.message.reply_text("Send /start to begin or use the buttons shown.")

async def unizik_sitting_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    try:
        _, choice = data.split("|", 1)
    except Exception:
        await query.edit_message_text("Invalid choice. Restart with /start.")
        return
    one_sitting = choice.lower() == "yes"
    jamb = context.user_data.get("jamb", 0)
    grades4 = context.user_data.get("olevel_grades", [])
    final_score = calc_unizik(jamb, grades4, one_sitting)
    user = update.effective_user
    await ensure_user(user.id, user.username)
    await add_points(user.id, -CALC_COST, f"UNIZIK screening calc for {context.user_data.get('selected_uni')}")
    await query.edit_message_text(
        f"UNIZIK Screening Result\n"
        f"JAMB: {jamb}\n"
        f"O'Level raw points (4 subjects): {grades4}\n"
        f"{'One sitting bonus applied' if one_sitting else 'No one-sitting bonus'}\n"
        f"Final screening score: {final_score:.2f}\n"
        f"-{CALC_COST} point deducted from your balance."
    )
    context.user_data.clear()

# /balance command
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await ensure_user(user.id, user.username)
    bal = await get_balance(user.id)
    await update.message.reply_text(f"Your balance: {bal} calculation(s).")

# /history command
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    rows = await get_history(user.id, limit=25)
    if not rows:
        await update.message.reply_text("No transactions yet.")
        return
    lines = [f"{r[2]}: {'+' if r[0]>0 else ''}{r[0]} — {r[1]}" for r in rows]
    await update.message.reply_text("Recent transactions:\n" + "\n".join(lines))

# Admin helper: currently not exposed as command in this file
# You can add commands to modify METHOD_MAP or balances if required.

# --- Bot startup ---
async def main():
    await init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback_router, pattern=r"^(nav\||select_univ\||show_refer|show_balance|show_history)"))
    app.add_handler(CallbackQueryHandler(unizik_sitting_cb, pattern=r"^unizik_sit\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("history", cmd_history))

    logger.info("Bot starting...")
    await app.run_polling()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
