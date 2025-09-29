#!/usr/bin/env python3
# main.py - Telegram bot with referral, balances, history, broadcast, paged university selector
# Added: /help, /calculate (start calc flow), new-user 10 free calculations,
#        referral gives referrer +5, /developer, /refer
# Requirements: python-telegram-bot>=20.0, aiosqlite, Flask, httpx
# Set environment variables:
#   BOT_TOKEN (required)
#   ADMIN_ID  (optional, numeric)
#   DB_PATH   (optional, default bot_data.sqlite)
#   PORT      (optional, default 8080)
#
# Run:
#   python main.py

import os
import logging
from threading import Thread
from typing import List, Tuple, Optional

import aiosqlite
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# --- Configuration ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required")

ADMIN_ID_ENV = os.environ.get("ADMIN_ID")
try:
    ADMIN_ID = int(ADMIN_ID_ENV) if ADMIN_ID_ENV else None
except Exception:
    ADMIN_ID = None

DB_PATH = os.environ.get("DB_PATH", "bot_data.sqlite")
CALC_COST = int(os.environ.get("CALC_COST", "1"))   # points per calculation
NEW_USER_BONUS = int(os.environ.get("NEW_USER_BONUS", "10"))  # points for new user
REF_BONUS = int(os.environ.get("REF_BONUS", "5"))   # points per referral
FLASK_PORT = int(os.environ.get("PORT", "8080"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- University pages (3 pages) ---
UNIV_PAGES: List[List[str]] = [
    ["UNILAG", "UNIBEN", "DELSU", "FUTO", "UNICAL", "UNIPORT", "UNIJOS", "UNILORIN"],
    ["UNN", "ABU", "UI", "OAU", "COVENANT", "FUTA", "BUK", "FUPRE"],
    ["LASU", "UNIOSUN", "AAUA", "UNIZIK", "PRIVATE1", "PRIVATE2", "POLY1", "COLLEGE1"],
]

# --- Mapping of university -> method key ---
METHOD_MAP = {
    "UNILAG": "METHOD_1", "UNIBEN": "METHOD_1", "DELSU": "METHOD_1", "FUTO": "METHOD_1",
    "UNICAL": "METHOD_1", "UNIPORT": "METHOD_1", "UNIJOS": "METHOD_1", "UNILORIN": "METHOD_1",
    "UNN": "METHOD_2", "ABU": "METHOD_2", "UI": "METHOD_2", "OAU": "METHOD_2",
    "COVENANT": "METHOD_2", "FUTA": "METHOD_2", "BUK": "METHOD_2", "FUPRE": "METHOD_2",
    "LASU": "METHOD_3", "UNIOSUN": "METHOD_3", "AAUA": "METHOD_3",
    "UNIZIK": "UNIZIK",
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
    # ensures user exists; if newly created, give NEW_USER_BONUS points
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        exists = await cur.fetchone()
        if not exists:
            await db.execute("INSERT INTO users(user_id, username, referred, referred_by) VALUES (?, ?, 0, NULL)",
                             (user_id, username or ""))
            await db.execute("INSERT OR IGNORE INTO balances(user_id, points) VALUES (?, 0)", (user_id,))
            await db.execute("UPDATE balances SET points = points + ? WHERE user_id = ?", (NEW_USER_BONUS, user_id))
            await db.execute("INSERT INTO tx(user_id, change, reason) VALUES (?, ?, ?)",
                             (user_id, NEW_USER_BONUS, "New user bonus"))
            await db.commit()
        else:
            # update username if changed
            await db.execute("UPDATE users SET username = ? WHERE user_id = ?", (username or "", user_id))
            await db.execute("INSERT OR IGNORE INTO balances(user_id, points) VALUES (?, 0)", (user_id,))
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
        cur = await db.execute("SELECT change, reason, ts FROM tx WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                               (user_id, limit))
        rows = await cur.fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

async def set_referred(new_user_id: int, referrer_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT referred FROM users WHERE user_id = ?", (new_user_id,))
        row = await cur.fetchone()
        if row and row[0] == 1:
            return False
        await db.execute("UPDATE users SET referred_by = ?, referred = 1 WHERE user_id = ?", (referrer_id, new_user_id))
        await db.execute("INSERT OR IGNORE INTO balances(user_id, points) VALUES (?, 0)", (referrer_id,))
        await db.execute("UPDATE balances SET points = points + ? WHERE user_id = ?", (REF_BONUS, referrer_id))
        await db.execute("INSERT INTO tx(user_id, change, reason) VALUES (?, ?, ?)",
                         (referrer_id, REF_BONUS, f"Referral bonus for referring {new_user_id}"))
        await db.commit()
        return True

async def fetch_all_user_ids() -> List[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM users")
        rows = await cur.fetchall()
        return [r[0] for r in rows]

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
    # referral handling: /start ref_<id>
    args = context.args
    if args and args[0].startswith("ref_"):
        try:
            ref_id = int(args[0].split("_", 1)[1])
        except Exception:
            ref_id = None
        if ref_id and ref_id != user.id:
            ok = await set_referred(user.id, ref_id)
            if ok:
                try:
                    await context.bot.send_message(chat_id=ref_id,
                                                   text=f"🎉 You referred @{user.username or user.id}! +{REF_BONUS} calculations added.")
                except Exception:
                    logger.info("Unable to notify referrer %s", ref_id)
                await update.message.reply_text("Thanks for using a referral link. Your referrer was rewarded.")
    # show main menu (university page 0)
    await update.message.reply_text("Welcome! Select your university category:", reply_markup=univ_page_keyboard(0))

# New command: /calculate - same flow as selecting a university from /start
async def calculate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await init_db()
    await ensure_user(user.id, user.username)
    await update.message.reply_text("Start a new calculation. Select your university category:", reply_markup=univ_page_keyboard(0))

# /refer command to show referral link
async def refer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = await context.bot.get_me()
    user = update.effective_user
    ref_link = f"https://t.me/{me.username}?start=ref_{user.id}"
    await update.message.reply_text(f"Share this link to refer others:\n\n{ref_link}\n\nEach successful referral gives the referrer +{REF_BONUS} calculations.")

# /help command listing available commands
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "Available commands:\n"
        "/start - Start and open university selector; accepts referral tokens\n"
        "/calculate - Start a new calculation (same as selecting university)\n"
        "/refer - Get your referral link\n"
        "/balance - Show your calculation balance\n"
        "/history - Show recent transactions\n"
        "/help - Show this help text\n"
        "/developer - Show developer info\n"
        "/broadcast - Admin-only: send message to all users\n"
    )
    await update.message.reply_text(help_text)

# /developer command: show author info
async def developer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Developer: Daniel\nTelegram: @Danzy_101")

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
        text = f"Share this link to refer others:\n\n{ref_link}\n\nEach successful referral gives the referrer +{REF_BONUS} calculations."
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

    # METHOD 1
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
        await ensure_user(user_id, user.username)
        # deduct cost (allow negative balances if desired)
        await add_points(user_id, -CALC_COST, f"Calculation used for {context.user_data.get('selected_uni')}")
        await update.message.reply_text(f"Aggregate: {aggregate:.2f}\n-{CALC_COST} point deducted from your balance.")
        context.user_data.clear()
        return

    # METHOD 2
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
        await update.message.reply_text(f"Aggregate: {aggregate:.2f}\n-{CALC_COST} point deducted from your balance.")
        context.user_data.clear()
        return

    # METHOD 3
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
        await update.message.reply_text(f"Aggregate: {aggregate:.2f}\n-{CALC_COST} point deducted from your balance.")
        context.user_data.clear()
        return

    # UNIZIK
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
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Yes (one sitting)", callback_data="unizik_sit|yes"),
                                         InlineKeyboardButton("No (multiple sittings)", callback_data="unizik_sit|no")]])
        await update.message.reply_text("Were the 4 credits obtained in a single sitting?", reply_markup=keyboard)
        return

    await update.message.reply_text("Send /start or /calculate to begin or use the buttons shown.")

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
    await addpoints(user.id, -CALCCOST, f"UNIZIK screening calc for {context.userdata.get('selecteduni')}")
    await query.editmessagetext(
        f"UNIZIK Screening Result\nJAMB: {jamb}\nO'Level raw points (4 subjects): {grades4}\n"
        f"{'One sitting bonus applied' if onesitting else 'No one-sitting bonus'}\nFinal screening score: {finalscore:.2f}\n"
        f"-{CALC_COST} point deducted from your balance."
    )
    context.user_data.clear()

#/balance command
async def cmdbalance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await ensure_user(user.id, user.username)
    bal = await get_balance(user.id)
    await update.message.reply_text(f"Your balance: {bal} calculation(s).")

#/history command
async def cmdhistory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    rows = await get_history(user.id, limit=25)
    if not rows:
        await update.message.reply_text("No transactions yet.")
        return
    lines = [f"{r[2]}: {'+' if r[0]>0 else ''}{r[0]} — {r[1]}" for r in rows]
    await update.message.reply_text("Recent transactions:\n" + "\n".join(lines))

#/broadcast admin-only
async def cmdbroadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    senderid = update.effectiveuser.id
    if ADMINID is None or senderid != ADMIN_ID:
        await update.message.reply_text("Unauthorized. This command is for the bot admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast Your message here")
        return
    text = " ".join(context.args)
    userids = await fetchalluserids()
    if not user_ids:
        await update.message.reply_text("No users found in the database.")
        return
    sent = 0
    failed = 0
    for uid in user_ids:
        try:
            await context.bot.sendmessage(chatid=uid, text=text)
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"Broadcast complete. Sent: {sent}. Failed: {failed}.")

# Bot startup (synchronous run_polling) 
def build_app():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.addhandler(CommandHandler("calculate", calculatecmd))
    app.addhandler(CommandHandler("refer", refercmd))
    app.addhandler(CommandHandler("help", helpcmd))
    app.addhandler(CommandHandler("developer", developercmd))
    app.addhandler(CallbackQueryHandler(callbackrouter, pattern=r"^(nav\||selectuniv\||showrefer|showbalance|showhistory)"))
    app.addhandler(CallbackQueryHandler(uniziksittingcb, pattern=r"^uniziksit\|"))
    app.addhandler(MessageHandler(filters.TEXT & ~filters.COMMAND, textrouter))
    app.addhandler(CommandHandler("balance", cmdbalance))
    app.addhandler(CommandHandler("history", cmdhistory))
    app.addhandler(CommandHandler("broadcast", cmdbroadcast))
    return app

# Dummy keep-alive Flask server

flaskapp = Flask("keepalive")

@flaskapp.route("/")
def home():
    return "Bot is running"

def run_flask():
    flaskapp.run(host="0.0.0.0", port=FLASKPORT)

# Entry pointt
if __name__ == "__main__":
    import asyncio
    # create/init database
    asyncio.run(init_db())
    # start Flask keep-alive server in background thread
    t = Thread(target=run_flask, daemon=True)
    t.start()
    logger.info("Started keep-alive server on port %s", FLASK_PORT)
    # build and run Telegram app (blocking)
    app = build_app()
    logger.info("Bot starting (run_polling)...")
    app.run_polling()

