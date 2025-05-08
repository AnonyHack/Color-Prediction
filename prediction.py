import os
import logging
import random
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import BadRequest
from aiohttp import web

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('prediction_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Bot configuration from environment variables
CONFIG = {
    'token': os.getenv('TELEGRAM_BOT_TOKEN', ''),
    'admin_id': int(os.getenv('ADMIN_ID', '')),
    'required_channels': os.getenv('REQUIRED_CHANNELS', 'Freenethubz,megahubbots,Freenethubchannel').split(','),
    'channel_links': os.getenv('CHANNEL_LINKS', 'https://t.me/Freenethubz,https://t.me/megahubbots,https://t.me/Freenethubchannel').split(',')
}

# MongoDB connection
try:
    mongodb_uri = os.getenv('MONGODB_URI')
    if not mongodb_uri:
        raise ValueError("MONGODB_URI environment variable not set")
    
    # Add retryWrites and SSL parameters if not already in URI
    if "retryWrites" not in mongodb_uri:
        if "?" in mongodb_uri:
            mongodb_uri += "&retryWrites=true&w=majority"
        else:
            mongodb_uri += "?retryWrites=true&w=majority"
    
    # Force SSL/TLS connection
    if "ssl=true" not in mongodb_uri.lower():
        if "?" in mongodb_uri:
            mongodb_uri += "&ssl=true"
        else:
            mongodb_uri += "?ssl=true"
    
    client = MongoClient(
        mongodb_uri,
        tls=True,
        tlsAllowInvalidCertificates=False,
        connectTimeoutMS=30000,
        socketTimeoutMS=30000,
        serverSelectionTimeoutMS=30000
    )
    
    # Test the connection immediately
    client.admin.command('ping')
    logger.info("Successfully connected to MongoDB")
    
except Exception as e:
    logger.error(f"Failed to connect to MongoDB: {str(e)}")
    raise

db = client[os.getenv('DATABASE_NAME', '')]

# Collections
users_collection = db['users']
predictions_collection = db['predictions']
leaderboard_collection = db['leaderboard']

# Webhook configuration
PORT = int(os.getenv('PORT', 10000))
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET', '')
WEBHOOK_URL = os.getenv('WEBHOOK_URL', '') + WEBHOOK_PATH

# Data Syncing Animation Frames
SYNC_FRAMES = [
    "📊 𝑪𝒉𝒆𝒄𝒌𝒊𝒏𝒈 𝑷𝒓𝒆𝒅𝒊𝒄𝒕𝒊𝒐𝒏𝒔 [▓▓░░░░░░░░░░░] 15%",
    "📊 𝑪𝒉𝒆𝒄𝒌𝒊𝒏𝒈 𝑷𝒓𝒆𝒅𝒊𝒄𝒕𝒊𝒐𝒏𝒔 [▓▓▓░░░░░░░░░] 30%",
    "📊 𝑪𝒉𝒆𝒄𝒌𝒊𝒏𝒈 𝑷𝒓𝒆𝒅𝒊𝒄𝒕𝒊𝒐𝒏𝒔 [▓▓▓▓▓░░░░░░░] 45%",
    "📊 𝑪𝒉𝒆𝒄𝒌𝒊𝒏𝒈 𝑷𝒓𝒆𝒅𝒊𝒄𝒕𝒊𝒐𝒏𝒔 [▓▓▓▓▓▓▓░░░░░] 60%",
    "📊 𝑪𝒉𝒆𝒄𝒌𝒊𝒏𝒈 𝑷𝒓𝒆𝒅𝒊𝒄𝒕𝒊𝒐𝒏𝒔 [▓▓▓▓▓▓▓▓▓░░░] 80%",
    "📊 𝑪𝒉𝒆𝒄𝒌𝒊𝒏𝒈 𝑷𝒓𝒆𝒅𝒊𝒄𝒕𝒊𝒐𝒏𝒔 [▓▓▓▓▓▓▓▓▓▓▓] 100%",
]

# === DATABASE FUNCTIONS ===
def add_user(user):
    """Add user to database if not exists"""
    users_collection.update_one(
        {'user_id': user.id},
        {'$set': {
            'username': user.username,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'balance': 0,
            'predictions': 0,
            'join_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }},
        upsert=True
    )

def add_prediction(user_id, prediction_type, result):
    """Add a prediction record"""
    predictions_collection.insert_one({
        'user_id': user_id,
        'type': prediction_type,
        'result': result,
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    })

def update_leaderboard(user_id, username, points=1):
    """Update leaderboard with points"""
    leaderboard_collection.update_one(
        {'user_id': user_id},
        {'$set': {'username': username},
        '$inc': {'score': points}
    },
    upsert=True
    )

def get_leaderboard():
    """Get top 10 users from leaderboard"""
    return list(leaderboard_collection.find().sort('score', -1).limit(10))

def get_user_stats(user_id):
    """Get user statistics"""
    user = users_collection.find_one({'user_id': user_id})
    prediction_count = predictions_collection.count_documents({'user_id': user_id})
    return user, prediction_count

# === FORCE JOIN FUNCTIONALITY ===
async def is_user_member(user_id, bot):
    """Check if user is member of all required channels"""
    for channel in CONFIG['required_channels']:
        try:
            chat_member = await bot.get_chat_member(chat_id=f"@{channel}", user_id=user_id)
            if chat_member.status not in ["member", "administrator", "creator"]:
                return False
        except Exception as e:
            logger.error(f"Error checking membership for {user_id} in {channel}: {e}")
            return False
    return True

async def ask_user_to_join(update):
    """Send message with join buttons"""
    buttons = [
        [InlineKeyboardButton(f"Join {CONFIG['required_channels'][i]}", url=CONFIG['channel_links'][i])] 
        for i in range(len(CONFIG['required_channels']))
    ]
    buttons.append([InlineKeyboardButton("✅ Verify", callback_data="verify_membership")])
    
    await update.message.reply_text(
        "🪬 Verification Status: ⚠️ You must join the following channels to use this bot and verify you're not a robot 🚨\n\n"
        "Click the buttons below to join, then press *'✅ Verify'*.",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )

async def verify_membership(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle membership verification"""
    query = update.callback_query
    user_id = query.from_user.id

    if await is_user_member(user_id, context.bot):
        await query.message.edit_text("✅ You are verified! You can now use the bot.")
        await start(update, context)
    else:
        await query.answer("⚠️ You haven't joined all the required channels yet!", show_alert=True)

# === PREDICTION FUNCTIONS ===
async def show_sync_animation(query):
    """Show data syncing animation"""
    message = await query.message.reply_text(SYNC_FRAMES[0])
    for frame in SYNC_FRAMES[1:]:
        await asyncio.sleep(1)
        await message.edit_text(frame)
    return message

async def handle_color_prediction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle color prediction"""
    query = update.callback_query
    await query.answer()

    # Show animation
    sync_message = await show_sync_animation(query)

    # Generate prediction
    color = random.choice(["🔴 RED", "🟢 GREEN"])
    image_url = "https://t.me/megahubbots/16" if color == "🔴 RED" else "https://t.me/megahubbots/15"

    # Save prediction
    add_prediction(query.from_user.id, "color", color)
    update_leaderboard(query.from_user.id, query.from_user.username)

    # Create response
    keyboard = [[InlineKeyboardButton("🔄 Get Prediction Again", callback_data="color_prediction")]]
    await sync_message.delete()
    await query.message.reply_photo(
        photo=image_url,
        caption=f"🎨 Color Prediction:\n\n{color}\n\n"
        "📑 Remember: Read how to bet from the /howtobet command before you proceed!\n\n"
        f'🔗 <a href="https://matatulord.com?referral_code=W7GNJF">Make Money with Matatu Game</a>',
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_number_prediction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle number prediction"""
    query = update.callback_query
    await query.answer()

    # Show animation
    sync_message = await show_sync_animation(query)

    # Generate prediction
    number = random.randint(1, 10)
    size = "SMALL" if number <= 4 else "BIG"
    image_url = "https://t.me/megahubbots/13" if size == "SMALL" else "https://t.me/megahubbots/14"

    # Save prediction
    add_prediction(query.from_user.id, "number", f"{number} ({size})")
    update_leaderboard(query.from_user.id, query.from_user.username)

    # Create response
    keyboard = [[InlineKeyboardButton("🔄 Get Prediction Again", callback_data="number_prediction")]]
    await sync_message.delete()
    await query.message.reply_photo(
        photo=image_url,
        caption=f"🔢 Number Prediction:\n\n🎰 Number: {number} ({size})\n\n"
        "📑 Remember: Read how to bet from the /howtobet command before you proceed!\n\n"
        f'🔗 <a href="https://matatulord.com?referral_code=W7GNJF">Make Money with Matatu Game</a>',
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# === COMMAND HANDLERS ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user = update.effective_user
    add_user(user)

    if not await is_user_member(user.id, context.bot):
        await ask_user_to_join(update)
        return

    keyboard = [
        [InlineKeyboardButton("🎨 Color Prediction", callback_data="color_prediction")],
        [InlineKeyboardButton("🔢 Number Prediction", callback_data="number_prediction")],
    ]
    await update.message.reply_text(
        "🎉 Welcome to the Color and Number Prediction Bot!\n\n"
        "📢 Disclaimer: This bot provides randomized predictions for entertainment only.\n\n"
        "Use the buttons below to get started:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def how_to_bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /howtobet command"""
    user_id = update.effective_user.id
    if not await is_user_member(user_id, context.bot):
        await ask_user_to_join(update)
        return

    await update.message.reply_text(
        "🎲 *How to Bet on Color & Number Prediction Games* 🎲\n\n"
        "1️⃣ Choose a trusted betting platform\n"
        "2️⃣ Deposit funds into your account\n"
        "3️⃣ Select the game type\n"
        "4️⃣ Place your bet\n"
        "5️⃣ Wait for results\n\n"
        "⚠️ *Bet Responsibly!*",
        parse_mode="Markdown"
    )

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /profile command"""
    user = update.effective_user
    if not await is_user_member(user.id, context.bot):
        await ask_user_to_join(update)
        return

    user_data, prediction_count = get_user_stats(user.id)
    await update.message.reply_text(
        f"👤 User Info:\n\n"
        f"🆔 User ID: {user.id}\n"
        f"🤵 Name: {user.first_name}\n"
        f"👤 Username: {user.username or 'N/A'}\n"
        f"📊 Predictions: {prediction_count}\n"
        f"⏳ Joined: {user_data.get('join_date', 'N/A')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /leaderboard command"""
    leaderboard_data = get_leaderboard()
    if not leaderboard_data:
        await update.message.reply_text("No users on the leaderboard yet.")
        return

    leaderboard_text = "🏆 Top 10 Users:\n\n"
    for i, user in enumerate(leaderboard_data, 1):
        leaderboard_text += f"{i}. {user['username']}: {user['score']} points\n"

    await update.message.reply_text(leaderboard_text)

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /broadcast command (admin only)"""
    if update.effective_user.id != CONFIG['admin_id']:
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return

    if not context.args:
        await update.message.reply_text("❌ Please provide a message to broadcast.")
        return

    message = " ".join(context.args)
    users = users_collection.find({}, {'user_id': 1})
    success = 0

    for user in users:
        try:
            await context.bot.send_message(user['user_id'], message)
            success += 1
        except Exception as e:
            logger.error(f"Failed to send to {user['user_id']}: {e}")

    await update.message.reply_text(f"✅ Broadcast sent to {success} users.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /stats command."""
    if update.effective_user.id != CONFIG['admin_id']:
        await update.message.reply_text("⚠️ You are not authorized to use this command.")
        return

    user_count = users_collection.count_documents({})
    total_predictions = predictions_collection.count_documents({})
    text = f"📊 *Bᴏᴛ Sᴛᴀᴛɪꜱᴛɪᴄꜱ*\n\n"
    text += f"👥 Tᴏᴛᴀʟ Uꜱᴇʀꜱ: {user_count}\n"
    text += f"🔍 Tᴏᴛᴀʟ Predictions: {total_predictions}\n\n"

    await update.message.reply_text(text, parse_mode="Markdown")

async def contact_us(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /contactus command."""
    contact_text = (
        "📞 ★彡( 𝕮𝖔𝖓𝖙𝖆𝖈𝖙 𝖀𝖘 )彡★ 📞\n\n"
        "📧 Eᴍᴀɪʟ: `freenethubbusiness@gmail.com`\n\n"
        "Fᴏʀ Aɴʏ Iꜱꜱᴜᴇꜱ, Bᴜꜱɪɴᴇꜱꜱ Dᴇᴀʟꜱ Oʀ IɴQᴜɪʀɪᴇꜱ, Pʟᴇᴀꜱᴇ Rᴇᴀᴄʜ Oᴜᴛ Tᴏ Uꜱ \n\n"
        "❗ *ONLY FOR BUSINESS AND HELP, DON'T SPAM!*"
    )
    
    keyboard = [[InlineKeyboardButton("📩 Mᴇꜱꜱᴀɢᴇ Aᴅᴍɪɴ", url="https://t.me/Silando")]]
    
    await update.message.reply_text(
        contact_text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# === WEBHOOK SETUP ===
async def health_check(request):
    """Health check endpoint"""
    return web.Response(text="OK")

async def telegram_webhook(request):
    """Handle incoming webhook requests"""
    update = Update.de_json(await request.json(), application.bot)
    await application.update_queue.put(update)
    return web.Response(text="OK")

def main():
    """Run the bot"""
    global application
    application = Application.builder().token(CONFIG['token']).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("howtobet", how_to_bet))
    application.add_handler(CommandHandler("profile", profile))
    application.add_handler(CommandHandler("leaderboard", leaderboard))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(CommandHandler("stats", stats))  # Updated stats command
    application.add_handler(CommandHandler("contactus", contact_us))  # Updated contactus command
    application.add_handler(CallbackQueryHandler(handle_color_prediction, pattern="^color_prediction$"))
    application.add_handler(CallbackQueryHandler(handle_number_prediction, pattern="^number_prediction$"))
    application.add_handler(CallbackQueryHandler(verify_membership, pattern="^verify_membership$"))

    # Start the bot with webhook if running on Render
    if os.getenv('RENDER'):
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=WEBHOOK_PATH,
            webhook_url=WEBHOOK_URL
        )
    else:
        application.run_polling()

if __name__ == "__main__":
    main()
