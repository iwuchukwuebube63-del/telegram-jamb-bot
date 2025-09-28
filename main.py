# main.py
import os
import json
import logging
import threading
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ---------- Config ----------
BOT_TOKEN = os.getenv("BOT_TOKEN") or "YOUR_BOT_TOKEN"
ADMIN_ID = int(os.getenv("ADMIN_ID") or "123456789")  # replace with your Telegram ID
USERS_FILE = "users.json"
PORT = int(os.getenv("PORT", "8080"))

# ---------- Logging ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------- Keep-alive Flask server ----------
app_flask = Flask("keepalive")

@app_flask.route("/")
def home():
    return "Bot is alive"

def run_flask():
    app_flask.run(host="0.0.0.0", port=PORT)

# ---------- Persistence helpers ----------
def load_users():
    try:
        with open(USERS_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_users(users):
    with open(USERS_FILE, "w") as f:
        json.dump(users, f)

def ensure_user(user_id):
    users = load_users()
    uid = str(user_id)
    if uid not in users:
        users[uid] = {
            "calculations": 10,
            "referrals": 0,
            "referred_by": None
        }
        save_users(users)
    return users

def register_user(user_id, ref_code=None):
    users = load_users()
    uid = str(user_id)
    if uid in users:
        return
    users[uid] = {"calculations": 10, "referrals": 0, "referred_by": None}
    if ref_code:
        ref_uid = str(ref_code)
        if ref_uid in users and users[uid]["referred_by"] is None:
            users[ref_uid]["calculations"] = users[ref_uid].get("calculations", 0) + 5
            users[ref_uid]["referrals"] = users[ref_uid].get("referrals", 0) + 1
            users[uid]["referred_by"] = ref_uid
    save_users(users)

# ---------- Grade mapping and calculation ----------
GRADE_SCORES = {
    'A1': 90, 'B2': 80, 'B3': 70,
    'C4': 60, 'C5': 55, 'C6': 50,
    'D7': 0, 'E8': 0, 'F9': 0,
    'AR': 0, 'OUTSTANDING': 0
}

def grade_to_score(grade):
    return GRADE_SCORES.get(grade.upper(), 0)

def calculate_aggregate_utme_only(utme_score, grades, one_sitting=True):
    utme_part = float(utme_score) * 0.7
    olevel_total = sum(grade_to_score(g) for g in grades)
    bonus = 10 if one_sitting else 0
    olevel_part = (olevel_total + bonus) * 0.3
    return round(utme_part + olevel_part, 2)

def calculate_aggregate_with_postutme(utme_score, grades, one_sitting, postutme_score):
    utme_part = float(utme_score) * 0.7
    olevel_total = sum(grade_to_score(g) for g in grades)
    bonus = 10 if one_sitting else 0
    olevel_part = (olevel_total + bonus) * 0.2
    postutme_part = float(postutme_score) * 0.1
    return round(utme_part + olevel_part + postutme_part, 2)

# ---------- Conversation steps ----------
# step keys: mode, step, utme, grades(list), sitting(bool), postutme
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    ref_code = None
    if args:
        token = args[0]
        if token.startswith("ref_"):
            ref_code = token.replace("ref_", "")
    register_user(user.id, ref_code)
    keyboard = [
        [InlineKeyboardButton("No Post-UTME (UTME + O'Level)", callback_data='mode_utme_only')],
        [InlineKeyboardButton("Has Post-UTME", callback_data='mode_postutme')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Welcome. Choose an option to proceed:", reply_markup=reply_markup
    )

async def mode_choice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mode = query.data
    context.user_data.clear()
    context.user_data['mode'] = mode
    context.user_data['step'] = 'ask_utme'
    await query.edit_message_text("Enter your UTME score (0-400):")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "/calculate - Start a new aggregate calculation\n"
        "/developer - Developer info\n"
        "/refer - Get your referral link\n"
        "/broadcast <message> - Admin only broadcast\n"
    )
    await update.message.reply_text(text)

async def developer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👨‍💻 Developer: Daniel\nTelegram: @Danzy_101")

async def calculate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data['mode'] = 'mode_utme_only'
    context.user_data['step'] = 'ask_utme'
    await update.message.reply_text("Enter your UTME score (0–400):")

async def refer(update: Update, context: ContextTypes.DEFAULT_TYPE):    user = update.effective_user
    ensure_user(user.id)
    bot_username = (await context.bot.get_me()).username
    link = f"https://t.me/{bot_username}?start=ref_{user.id}"
    await update.message.reply_text(
        f"🔗 Invite your friends with this link:\n{link}\nDeveloped by Daniel (@Danzy_101)\nEarn 5 free calculations per referral!"
    )

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("⛔ You are not authorized to use this command.")
        return
    message = " ".join(context.args).strip()
    if not message:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    users = load_users()
    count = 0
    for uid in list(users.keys()):
        try:
            await context.bot.send_message(chat_id=int(uid), text=f"📢 Message from Admin:\n{message}")
            count += 1
        except Exception as e:
            logger.warning("Failed to send to %s: %s", uid, e)
            continue
    await update.message.reply_text(f"Broadcast sent to {count} users.")

# ---------- Handler for step-by-step inputs ----------
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    ensure_user(user.id)
    mode = context.user_data.get('mode')
    step = context.user_data.get('step')

    # If user explicitly asks /calculate start fresh
    if text.lower() == "/calculate":
        context.user_data.clear()
        context.user_data['mode'] = 'mode_utme_only'
        context.user_data['step'] = 'ask_utme'
        await update.message.reply_text("Enter your UTME score (0-400):")
        return

    if not step:
        await update.message.reply_text("Type /calculate to start or /help to see commands.")
        return

    # UTME step
    if step == 'ask_utme':
        try:
            utme = float(text)
            if utme < 0 or utme > 400:
                raise ValueError
            context.user_data['utme'] = utme
            context.user_data['grades'] = []
            context.user_data['step'] = 'grade1'
            await update.message.reply_text("Enter 1st O'Level grade (e.g., A1):")
        except:
            await update.message.reply_text("Invalid UTME. Enter a number between 0 and 400.")
        return

    # Grade steps 1-4
    if step.startswith('grade'):
        idx = int(step.replace('grade', ''))
        grade = text.upper()
        if grade not in GRADE_SCORES:
            await update.message.reply_text("Invalid grade. Valid examples: A1 B2 B3 C4 C5 C6 D7 E8 F9 AR")
            return
        context.user_data['grades'].append(grade)
        if idx < 4:
            context.user_data['step'] = f'grade{idx+1}'
            await update.message.reply_text(f"Enter {idx+1}th O'Level grade:")
            return
        else:
            context.user_data['step'] = 'sitting'
            await update.message.reply_text("Was it one sitting or two sittings? Reply: One or Two")
            return

    # Sitting step
    if step == 'sitting':
        resp = text.lower()
        if resp not in ('one', 'two'):
            await update.message.reply_text("Reply with 'One' for single sitting or 'Two' for two sittings.")
            return
        context.user_data['sitting'] = (resp == 'one')
        if context.user_data.get('mode') == 'mode_postutme':
            context.user_data['step'] = 'postutme'
            await update.message.reply_text("Enter your Post-UTME score (0-100):")
            return
        else:
            # compute and return
            utme = context.user_data['utme']
            grades = context.user_data['grades']
            one_sitting = context.user_data['sitting']
            users = load_users()
            uid = str(user.id)
            remaining = users.get(uid, {}).get("calculations", 0)
            if remaining <= 0:
                await update.message.reply_text("You have no free calculations left. Refer friends to earn more.")
                context.user_data.clear()
                return
            score = calculate_aggregate_utme_only(utme, grades, one_sitting)
            users[uid]["calculations"] = remaining - 1
            save_users(users)
            await update.message.reply_text(f"🎓 Your aggregate score is: {score}\nYou have {users[uid]['calculations']} calculations left.")
            context.user_data.clear()
            return

    # Post-UTME step
    if step == 'postutme':
        try:
            post = float(text)
            if post < 0 or post > 100:
                raise ValueError
            utme = context.user_data['utme']
            grades = context.user_data['grades']
            one_sitting = context.user_data['sitting']
            users = load_users()
            uid = str(user.id)
            remaining = users.get(uid, {}).get("calculations", 0)
            if remaining <= 0:
                await update.message.reply_text("You have no free calculations left. Refer friends to earn more.")
                context.user_data.clear()
                return
            score = calculate_aggregate_with_postutme(utme, grades, one_sitting, post)
            users[uid]["calculations"] = remaining - 1
            save_users(users)
            await update.message.reply_text(
                f"🎓 Your aggregate score (with Post-UTME) is: {score}\nYou have {users[uid]['calculations']} calculations left."
            )
            context.user_data.clear()
        except:
            await update.message.reply_text("Invalid Post-UTME score. Enter a number between 0 and 100.")
        return

# ---------- Startup ----------
def main():
    # Start Flask keep-alive in background thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Build the Telegram application
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("developer", developer))
    app.add_handler(CommandHandler("refer", refer))
    app.add_handler(CommandHandler("calculate", calculate_command))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CallbackQueryHandler(mode_choice_handler, pattern="^mode_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    # Run bot
    logger.info("Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()

