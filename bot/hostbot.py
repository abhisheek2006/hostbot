# hostbot.py - HostBot (Python 3.14.x) - Complete fixed version
import telebot
import subprocess
import os
import zipfile
import tempfile
import shutil
from telebot import types
from pymongo import MongoClient
import time
from datetime import datetime, timedelta
import psutil
import json
import logging
import threading
import re
import sys
import atexit
import requests
import hashlib
import signal
from dotenv import load_dotenv

# ====================== HOSTBOT CONFIGURATION ======================
# Load configuration from .env (works on VPS, Docker, and locally)
load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

def _env_int(name, default):
    """Read an integer from the environment, tolerating trailing inline comments
    (some env-file loaders - e.g. systemd EnvironmentFile - do not strip them)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = str(raw).strip()
    if '#' in raw:
        raw = raw.split('#', 1)[0].strip()
    try:
        return int(raw)
    except ValueError:
        logger = logging.getLogger(__name__)
        logger.warning(f"Invalid integer for {name}={raw!r}, using default {default}")
        return default

# Persistent data directory.
#   - VPS / Docker : /data  (mount a persistent volume)
#   - Local        : ./data (auto-created next to the bot)
DATA_DIR = os.environ.get('DATA_DIR', os.path.join(BASE_DIR, 'data'))
# Resolve relative DATA_DIR against the bot folder so uploads always land in
# a predictable place regardless of the working directory the process is started
# from (local, systemd, Docker all differ).
if not os.path.isabs(DATA_DIR):
    DATA_DIR = os.path.normpath(os.path.join(BASE_DIR, DATA_DIR))
UPLOAD_BOTS_DIR = os.path.join(DATA_DIR, 'upload_bots')
IROTECH_DIR = os.path.join(DATA_DIR, 'inf')

# ---- MongoDB (hosting plans, users, files, approvals) ----
# Connection string from MongoDB Atlas, e.g. mongodb+srv://user:pass@cluster.mongodb.net/
MONGO_URI = os.environ.get('MONGO_URI', '')
MONGO_DB_NAME = os.environ.get('MONGO_DB_NAME', 'hostbot')

# Create directories with proper permissions
os.makedirs(UPLOAD_BOTS_DIR, exist_ok=True, mode=0o755)
os.makedirs(IROTECH_DIR, exist_ok=True, mode=0o755)

# ---- Bot credentials (from .env) ----
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
if not TOKEN:
    sys.exit('TELEGRAM_BOT_TOKEN is not set! Copy .env.example to .env and fill in your token.')
OWNER_ID = _env_int('OWNER_ID', 0)
ADMIN_ID = _env_int('ADMIN_ID', OWNER_ID or 0)
YOUR_USERNAME = os.environ.get('YOUR_USERNAME', '@hostbot')
UPDATE_CHANNEL = os.environ.get('UPDATE_CHANNEL', 'https://t.me/yourchannel')

# ---- MPX AI (optional) ----
A4F_API_URL = os.environ.get('A4F_API_URL', 'https://samuraiapi.in/v1/chat/completions')
A4F_API_KEY = os.environ.get('A4F_API_KEY', '')
A4F_MODEL = os.environ.get('A4F_MODEL', 'provider10-claude-sonnet-4-20250514(clinesp)')

# ---- Optional lightweight status server (feeds the Vercel landing page) ----
STATUS_SERVER_ENABLED = os.environ.get('STATUS_SERVER_ENABLED', 'false').lower() == 'true'
STATUS_SERVER_PORT = _env_int('STATUS_SERVER_PORT', 9090)
STATUS_TOKEN = os.environ.get('STATUS_TOKEN', '')

BOT_START_TIME = datetime.now()

def get_uptime():
    uptime = datetime.now() - BOT_START_TIME
    days = uptime.days
    hours, remainder = divmod(uptime.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{days}d {hours}h {minutes}m {seconds}s"

# ---- Hosting plans ----
# Free users can host FREE_BOT_LIMIT bots. Paid plans allow many more.
ADMIN_LIMIT = 999
OWNER_LIMIT = float('inf')
FREE_BOT_LIMIT = _env_int('FREE_BOT_LIMIT', 3)
SUBSCRIBED_USER_LIMIT = _env_int('SUBSCRIBED_USER_LIMIT', 15)

# Plan definitions: key -> (label, bot limit)
# Adjust limits in .env: PLAN_STARTER_LIMIT, PLAN_PRO_LIMIT, PLAN_BUSINESS_LIMIT
PLANS = {
    'free':     {'name': 'Free',     'limit': FREE_BOT_LIMIT},
    'starter':  {'name': 'Starter',  'limit': _env_int('PLAN_STARTER_LIMIT', 8)},
    'pro':      {'name': 'Pro',      'limit': _env_int('PLAN_PRO_LIMIT', 20)},
    'business': {'name': 'Business', 'limit': _env_int('PLAN_BUSINESS_LIMIT', 50)},
}

# ---- Web dashboard login (VPS status server) ----
# Public web dashboard / landing page URL shown to users.
WEB_URL = os.environ.get('WEB_URL', 'https://hostbot-fawn.vercel.app')
# Default single user: WEB_USERNAME / WEB_PASSWORD maps to WEB_OWNER_ID
# Or multiple users: WEB_USERS="user1:pass1:telegramid1;user2:pass2:telegramid2"
WEB_USERNAME = os.environ.get('WEB_USERNAME', '@ABHISHEEK16')
WEB_PASSWORD = os.environ.get('WEB_PASSWORD', 'abhisheek2006')
WEB_OWNER_ID = _env_int('WEB_OWNER_ID', OWNER_ID or 0)
WEB_USERS_RAW = os.environ.get('WEB_USERS', '')

WEB_SESSION_TTL = _env_int('WEB_SESSION_TTL', 86400)  # seconds (24h)
web_sessions = {}  # token -> {telegram_id, username, expires}

def load_web_users():
    """Returns dict username -> {'password': ..., 'telegram_id': ...}"""
    users = {}
    try:
        if WEB_USERS_RAW:
            for entry in WEB_USERS_RAW.split(';'):
                entry = entry.strip()
                if not entry:
                    continue
                parts = entry.split(':')
                if len(parts) >= 3:
                    users[parts[0].strip()] = {
                        'password': parts[1],
                        'telegram_id': int(parts[2].strip()),
                    }
    except Exception as e:
        logger.error(f"Error parsing WEB_USERS: {e}", exc_info=True)
    if not users and WEB_USERNAME:
        users[WEB_USERNAME] = {'password': WEB_PASSWORD, 'telegram_id': WEB_OWNER_ID}
    return users

WEB_USERS = load_web_users()

# Pending registrations started from the bot: user_id -> {'username': ..., 'password': ...}
pending_regs = {}

def _hash_password(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000).hex()

def register_web_user(username, password, telegram_id, plan='free'):
    """Create a web dashboard account (bot or /api/register). Returns (ok, message)."""
    username = (username or '').strip()
    if not re.match(r'^[A-Za-z0-9_]{3,24}$', username):
        return False, 'Username must be 3-24 characters (letters, digits, underscore).'
    if not password or len(password) < 6:
        return False, 'Password must be at least 6 characters.'
    plan = (plan or 'free').lower()
    if plan not in PLANS:
        return False, f"Plan must be one of: {', '.join(PLANS.keys())}."
    if not isinstance(telegram_id, int) or telegram_id <= 0:
        return False, 'Invalid Telegram ID.'
    if WEB_USERS.get(username) or db.web_users.find_one({'username': username}):
        return False, 'This username is already taken.'
    if db.web_users.find_one({'telegram_id': telegram_id}):
        return False, 'This Telegram ID is already registered.'
    salt = os.urandom(16)
    db.web_users.insert_one({
        'username': username,
        'salt': salt.hex(),
        'password_hash': _hash_password(password, salt),
        'telegram_id': telegram_id,
        'plan': plan,
        'created_at': datetime.now().isoformat(),
    })
    if plan != 'free':
        try:
            bot.send_message(
                OWNER_ID,
                f"🆕 Web registration with paid plan\nUser: `{username}`\n"
                f"Telegram ID: `{telegram_id}`\nChosen plan: `{plan}`\n"
                f"Activate: `/subscriptions` -> Add `{telegram_id} 30 {plan}`",
                parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Failed to notify owner of paid registration: {e}")
    return True, 'Account created.'

def verify_web_login(username, password):
    """Check .env WEB_USERS first, then MongoDB web_users. Returns (telegram_id, account_doc|None)."""
    env_user = WEB_USERS.get(username or '')
    if env_user and env_user['password'] == password:
        return env_user['telegram_id'], None
    doc = db.web_users.find_one({'username': (username or '').strip()})
    if doc:
        try:
            salt = bytes.fromhex(doc['salt'])
            if _hash_password(password or '', salt) == doc['password_hash']:
                return doc['telegram_id'], doc
        except Exception as e:
            logger.error(f"Error verifying password for '{username}': {e}", exc_info=True)
    return None, None

bot = telebot.TeleBot(TOKEN)

bot_scripts = {}
user_subscriptions = {}
user_files = {}
active_users = set()
admin_ids = {ADMIN_ID, OWNER_ID}
bot_locked = False

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# File approval status constants
FILE_STATUS_PENDING = "pending"
FILE_STATUS_APPROVED = "approved"
FILE_STATUS_REJECTED = "rejected"

COMMAND_BUTTONS_LAYOUT_USER_SPEC = [
    ["📢 Updates Channel", "⏱ Uptime"],
    ["📤 Upload File", "📂 Check Files"],
    ["⚡ Bot Speed", "📊 Statistics"],
    ["💠 Plans", "📝 Register"],
    ["🌐 Web Dashboard", "🤖 MPX Ai"],
    ["📞 Contact Owner"]
]

ADMIN_COMMAND_BUTTONS_LAYOUT_USER_SPEC = [
    ["📢 Updates Channel", "🌐 Web Dashboard"],
    ["📤 Upload File", "📂 Check Files"],
    ["⚡ Bot Speed", "📊 Statistics"],
    ["💠 Plans", "💳 Subscriptions"],
    ["📝 Register", "📢 Broadcast"],
    ["🔒 Lock Bot", "🟢 Running All Code"],
    ["👑 Admin Panel", "📞 Contact Owner"],
    ["🤖 MPX Ai", "⏱ Uptime"],
]

mongo_client = None
db = None

def init_db():
    """Connect to MongoDB Atlas and ensure required collections + indexes."""
    global mongo_client, db
    if not MONGO_URI:
        logger.error("MONGO_URI is not set. Copy .env.example to .env and set your MongoDB connection string.")
        raise SystemExit("MONGO_URI required (MongoDB Atlas connection string)")
    logger.info(f"Connecting to MongoDB database: {MONGO_DB_NAME}")
    try:
        mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)
        mongo_client.admin.command('ping')
        db = mongo_client[MONGO_DB_NAME]
        db.subscriptions.create_index([('user_id', 1)], unique=True)
        db.user_files.create_index([('user_id', 1), ('file_name', 1)], unique=True)
        db.active_users.create_index([('user_id', 1)], unique=True)
        db.admins.create_index([('user_id', 1)], unique=True)
        db.file_approvals.create_index([('user_id', 1), ('file_name', 1)], unique=True)
        db.web_users.create_index([('username', 1)], unique=True)
        db.web_users.create_index([('telegram_id', 1)], unique=True)
        logger.info("MongoDB connected successfully.")
    except Exception as e:
        logger.error(f"MongoDB connection error: {e}", exc_info=True)
        raise SystemExit(f"MongoDB connection failed: {e}")

def load_data():
    logger.info("Loading data from MongoDB...")
    try:
        for doc in db.subscriptions.find({}):
            user_id = doc['user_id']
            try:
                plan = doc.get('plan', 'pro')
                user_subscriptions[user_id] = {
                    'expiry': datetime.fromisoformat(doc['expiry']),
                    'plan': plan if plan in PLANS else 'pro',
                }
            except (ValueError, KeyError):
                logger.warning(f"Invalid subscription record for user {user_id}: {doc}. Skipping.")

        for doc in db.user_files.find({}):
            user_id = doc['user_id']
            if user_id not in user_files:
                user_files[user_id] = []
            user_files[user_id].append((doc['file_name'], doc['file_type']))

        active_users.update(doc['user_id'] for doc in db.active_users.find({}))
        admin_ids.update(doc['user_id'] for doc in db.admins.find({}))

        logger.info(f"Data loaded: {len(active_users)} users, {len(user_subscriptions)} subscriptions, {len(admin_ids)} admins.")
    except Exception as e:
        logger.error(f"Error loading data: {e}", exc_info=True)

init_db()
load_data()

# File approval functions (MongoDB needs no lock; DB_LOCK guards in-memory dict mutations)
DB_LOCK = threading.Lock()

def save_file_approval(user_id, file_name, file_type, status=FILE_STATUS_PENDING, reviewed_by=None, message_id=None):
    """Save or update file approval status"""
    try:
        uploaded_time = datetime.now().isoformat()
        review_time = datetime.now().isoformat() if reviewed_by else None
        db.file_approvals.replace_one(
            {'user_id': user_id, 'file_name': file_name},
            {'user_id': user_id, 'file_name': file_name, 'file_type': file_type,
             'status': status, 'reviewed_by': reviewed_by, 'review_time': review_time,
             'uploaded_time': uploaded_time, 'message_id': message_id},
            upsert=True)
        logger.info(f"File approval saved: {user_id}/{file_name} -> {status}")
    except Exception as e:
        logger.error(f"Error saving file approval: {e}", exc_info=True)

def get_file_status(user_id, file_name):
    """Get approval status of a file"""
    try:
        doc = db.file_approvals.find_one({'user_id': user_id, 'file_name': file_name})
        if doc:
            return {
                'status': doc.get('status', FILE_STATUS_PENDING),
                'reviewed_by': doc.get('reviewed_by'),
                'review_time': doc.get('review_time'),
                'file_type': doc.get('file_type'),
            }
        return {'status': FILE_STATUS_PENDING, 'file_type': 'unknown'}
    except Exception as e:
        logger.error(f"Error getting file status: {e}")
        return {'status': FILE_STATUS_PENDING, 'file_type': 'unknown'}

def update_file_status(user_id, file_name, status, admin_id):
    """Update file approval status"""
    try:
        review_time = datetime.now().isoformat()
        db.file_approvals.update_one(
            {'user_id': user_id, 'file_name': file_name},
            {'$set': {'status': status, 'reviewed_by': admin_id, 'review_time': review_time}})
        logger.info(f"File status updated: {user_id}/{file_name} -> {status} by {admin_id}")
        return True
    except Exception as e:
        logger.error(f"Error updating file status: {e}")
        return False

def get_all_pending_files():
    """Get all files pending approval as list of (user_id, file_name, file_type, uploaded_time)"""
    try:
        docs = db.file_approvals.find({'status': FILE_STATUS_PENDING}).sort('uploaded_time', -1)
        return [(d['user_id'], d['file_name'], d.get('file_type'), d.get('uploaded_time')) for d in docs]
    except Exception as e:
        logger.error(f"Error getting pending files: {e}")
        return []

def get_pending_files_count():
    """Get count of pending files"""
    try:
        return db.file_approvals.count_documents({'status': FILE_STATUS_PENDING})
    except Exception as e:
        logger.error(f"Error getting pending files count: {e}")
        return 0

def send_file_for_approval(message, user_id, file_name, file_type):
    """Send file to all admins for approval"""
    user = message.from_user
    file_info = (
        f"📄 **NEW FILE FOR APPROVAL**\n\n"
        f"👤 **User:** {user.first_name}\n"
        f"📛 **Username:** @{user.username or 'N/A'}\n"
        f"🆔 **User ID:** `{user_id}`\n"
        f"📁 **File:** `{file_name}`\n"
        f"📊 **Type:** {file_type}\n"
        f"🕐 **Time:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"**Choose action:**"
    )
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ Approve", callback_data=f'approve_{user_id}_{file_name}'),
        types.InlineKeyboardButton("❌ Reject", callback_data=f'reject_{user_id}_{file_name}')
    )
    markup.add(types.InlineKeyboardButton("📋 View All Pending", callback_data='view_pending'))
    
    for admin_id in admin_ids:
        try:
            bot.forward_message(admin_id, message.chat.id, message.message_id)
            sent_msg = bot.send_message(admin_id, file_info, 
                                      reply_markup=markup, 
                                      parse_mode='Markdown')
            save_file_approval(user_id, file_name, file_type, 
                             FILE_STATUS_PENDING, None, sent_msg.message_id)
        except Exception as e:
            logger.error(f"Failed to send file for approval to admin {admin_id}: {e}")

def get_user_folder(user_id):
    user_folder = os.path.join(UPLOAD_BOTS_DIR, str(user_id))
    os.makedirs(user_folder, exist_ok=True, mode=0o755)
    return user_folder

def get_user_plan(user_id):
    """Return the active plan key for a user: admin, owner, plan name, or free."""
    if user_id == OWNER_ID:
        return 'owner'
    if user_id in admin_ids:
        return 'admin'
    sub = user_subscriptions.get(user_id)
    if sub and sub.get('expiry', datetime.min) > datetime.now():
        return sub.get('plan', 'pro') if sub.get('plan') in PLANS else 'pro'
    return 'free'

def get_plan_limit(user_id):
    """Return the number of bots a user may host."""
    if user_id == OWNER_ID:
        return OWNER_LIMIT
    if user_id in admin_ids:
        return ADMIN_LIMIT
    plan_key = get_user_plan(user_id)
    return PLANS.get(plan_key, PLANS['free'])['limit']

def get_user_file_limit(user_id):
    return get_plan_limit(user_id)

def get_user_file_count(user_id):
    return len(user_files.get(user_id, []))

def get_plans_text(user_id=None):
    """Human-readable list of all plans for the /plans command."""
    lines = ["💠 <b>HostBot Hosting Plans</b>", ""]
    for key in ['free', 'starter', 'pro', 'business']:
        p = PLANS[key]
        lines.append(f"• <b>{p['name']}</b> - {p['limit']} bots")
    if user_id is not None:
        plan_key = get_user_plan(user_id)
        if plan_key in ('owner', 'admin'):
            lines.append(f"\nYour plan: <b>{plan_key.title()}</b> (unlimited)")
        else:
            p = PLANS.get(plan_key, PLANS['free'])
            lines.append(f"\nYour plan: <b>{p['name']}</b> ({p['limit']} bots)")
    return "\n".join(lines)

def is_bot_running(script_owner_id, file_name):
    script_key = f"{script_owner_id}_{file_name}"
    script_info = bot_scripts.get(script_key)
    if script_info and script_info.get('process'):
        try:
            proc = psutil.Process(script_info['process'].pid)
            is_running = proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
            if not is_running:
                logger.warning(f"Process {script_info['process'].pid} for {script_key} found in memory but not running/zombie. Cleaning up.")
                if 'log_file' in script_info and hasattr(script_info['log_file'], 'close') and not script_info['log_file'].closed:
                    try:
                        script_info['log_file'].close()
                    except Exception as log_e:
                        logger.error(f"Error closing log file during zombie cleanup {script_key}: {log_e}")
                if script_key in bot_scripts:
                    del bot_scripts[script_key]
            return is_running
        except psutil.NoSuchProcess:
            logger.warning(f"Process for {script_key} not found (NoSuchProcess). Cleaning up.")
            if 'log_file' in script_info and hasattr(script_info['log_file'], 'close') and not script_info['log_file'].closed:
                try:
                     script_info['log_file'].close()
                except Exception as log_e:
                     logger.error(f"Error closing log file during cleanup of non-existent process {script_key}: {log_e}")
            if script_key in bot_scripts:
                 del bot_scripts[script_key]
            return False
        except Exception as e:
            logger.error(f"Error checking process status for {script_key}: {e}", exc_info=True)
            return False
    return False

def kill_process_tree(process_info):
    pid = None
    log_file_closed = False
    script_key = process_info.get('script_key', 'N/A')

    try:
        if 'log_file' in process_info and hasattr(process_info['log_file'], 'close') and not process_info['log_file'].closed:
            try:
                process_info['log_file'].close()
                log_file_closed = True
                logger.info(f"Closed log file for {script_key} (PID: {process_info.get('process', {}).get('pid', 'N/A')})")
            except Exception as log_e:
                logger.error(f"Error closing log file during kill for {script_key}: {log_e}")

        process = process_info.get('process')
        if process and hasattr(process, 'pid'):
           pid = process.pid
           if pid:
                try:
                    parent = psutil.Process(pid)
                    children = parent.children(recursive=True)
                    logger.info(f"Attempting to kill process tree for {script_key} (PID: {pid}, Children: {[c.pid for c in children]})")

                    for child in children:
                        try:
                            child.terminate()
                            logger.info(f"Terminated child process {child.pid} for {script_key}")
                        except psutil.NoSuchProcess:
                            logger.warning(f"Child process {child.pid} for {script_key} already gone.")
                        except Exception as e:
                            logger.error(f"Error terminating child {child.pid} for {script_key}: {e}. Trying kill...")
                            try: child.kill(); logger.info(f"Killed child process {child.pid} for {script_key}")
                            except Exception as e2: logger.error(f"Failed to kill child {child.pid} for {script_key}: {e2}")

                    gone, alive = psutil.wait_procs(children, timeout=1)
                    for p in alive:
                        logger.warning(f"Child process {p.pid} for {script_key} still alive. Killing.")
                        try: p.kill()
                        except Exception as e: logger.error(f"Failed to kill child {p.pid} for {script_key} after wait: {e}")

                    try:
                        parent.terminate()
                        logger.info(f"Terminated parent process {pid} for {script_key}")
                        try: parent.wait(timeout=1)
                        except psutil.TimeoutExpired:
                            logger.warning(f"Parent process {pid} for {script_key} did not terminate. Killing.")
                            parent.kill()
                            logger.info(f"Killed parent process {pid} for {script_key}")
                    except psutil.NoSuchProcess:
                        logger.warning(f"Parent process {pid} for {script_key} already gone.")
                    except Exception as e:
                        logger.error(f"Error terminating parent {pid} for {script_key}: {e}. Trying kill...")
                        try: parent.kill(); logger.info(f"Killed parent process {pid} for {script_key}")
                        except Exception as e2: logger.error(f"Failed to kill parent {pid} for {script_key}: {e2}")

                except psutil.NoSuchProcess:
                    logger.warning(f"Process {pid or 'N/A'} for {script_key} not found during kill. Already terminated?")
           else: logger.error(f"Process PID is None for {script_key}.")
        elif log_file_closed: logger.warning(f"Process object missing for {script_key}, but log file closed.")
        else: logger.error(f"Process object missing for {script_key}, and no log file. Cannot kill.")
    except Exception as e:
        logger.error(f"Unexpected error killing process tree for PID {pid or 'N/A'} ({script_key}): {e}", exc_info=True)

TELEGRAM_MODULES = {
    'telebot': 'pyTelegramBotAPI',
    'telegram': 'python-telegram-bot',
    'python_telegram_bot': 'python-telegram-bot',
    'aiogram': 'aiogram',
    'pyrogram': 'pyrogram',
    'telethon': 'telethon',
    'telethon.sync': 'telethon',
    'from telethon.sync import telegramclient': 'telethon',
    'telepot': 'telepot',
    'pytg': 'pytg',
    'tgcrypto': 'tgcrypto',
    'telegram_upload': 'telegram-upload',
    'telegram_send': 'telegram-send',
    'telegram_text': 'telegram-text',
    'tl': 'telethon',
    'telegram_utils': 'telegram-utils',
    'telegram_logger': 'telegram-logger',
    'telegram_handlers': 'python-telegram-handlers',
    'telegram_redis': 'telegram-redis',
    'telegram_sqlalchemy': 'telegram-sqlalchemy',
    'telegram_payment': 'telegram-payment',
    'telegram_shop': 'telegram-shop-sdk',
    'pytest_telegram': 'pytest-telegram',
    'telegram_debug': 'telegram-debug',
    'telegram_scraper': 'telegram-scraper',
    'telegram_analytics': 'telegram-analytics',
    'telegram_nlp': 'telegram-nlp-toolkit',
    'telegram_ai': 'telegram-ai',
    'telegram_api': 'telegram-api-client',
    'telegram_web': 'telegram-web-integration',
    'telegram_games': 'telegram-games',
    'telegram_quiz': 'telegram-quiz-bot',
    'telegram_ffmpeg': 'telegram-ffmpeg',
    'telegram_media': 'telegram-media-utils',
    'telegram_2fa': 'telegram-twofa',
    'telegram_crypto': 'telegram-crypto-bot',
    'telegram_i18n': 'telegram-i18n',
    'telegram_translate': 'telegram-translate',
    'bs4': 'beautifulsoup4',
    'requests': 'requests',
    'pillow': 'Pillow',
    'cv2': 'opencv-python',
    'yaml': 'PyYAML',
    'dotenv': 'python-dotenv',
    'dateutil': 'python-dateutil',
    'pandas': 'pandas',
    'numpy': 'numpy',
    'flask': 'Flask',
    'django': 'Django',
    'sqlalchemy': 'SQLAlchemy',
    'asyncio': None,
    'json': None,
    'datetime': None,
    'os': None,
    'sys': None,
    're': None,
    'time': None,
    'math': None,
    'random': None,
    'logging': None,
    'threading': None,
    'subprocess': None,
    'zipfile': None,
    'tempfile': None,
    'shutil': None,
    'sqlite3': None,
    'psutil': 'psutil',
    'atexit': None
}

def attempt_install_pip(module_name, message):
    package_name = TELEGRAM_MODULES.get(module_name.lower(), module_name)
    if package_name is None:
        logger.info(f"Module '{module_name}' is core. Skipping pip install.")
        return False
    try:
        bot.reply_to(message, f"Module `{module_name}` not found. Installing `{package_name}`...", parse_mode='Markdown')
        command = [sys.executable, '-m', 'pip', 'install', package_name]
        logger.info(f"Running install: {' '.join(command)}")
        result = subprocess.run(command, capture_output=True, text=True, check=False, encoding='utf-8', errors='ignore')
        if result.returncode == 0:
            logger.info(f"Installed {package_name}. Output:\n{result.stdout}")
            bot.reply_to(message, f"Package `{package_name}` (for `{module_name}`) installed.", parse_mode='Markdown')
            return True
        else:
            error_msg = f"Failed to install `{package_name}` for `{module_name}`.\nLog:\n```\n{result.stderr or result.stdout}\n```"
            logger.error(error_msg)
            if len(error_msg) > 4000: error_msg = error_msg[:4000] + "\n... (Log truncated)"
            bot.reply_to(message, f"Failed to install {package_name} for {module_name}.\n\n{result.stderr or result.stdout}\n\n(Troubleshoot: network blocked or package name invalid.)")
            return False
    except Exception as e:
        error_msg = f"Error installing `{package_name}`: {str(e)}"
        logger.error(error_msg, exc_info=True)
        bot.reply_to(message, error_msg)
        return False

def attempt_install_npm(module_name, user_folder, message):
    try:
        bot.reply_to(message, f"Node package `{module_name}` not found. Installing locally...", parse_mode='Markdown')
        command = ['npm', 'install', module_name]
        logger.info(f"Running npm install: {' '.join(command)} in {user_folder}")
        result = subprocess.run(command, capture_output=True, text=True, check=False, cwd=user_folder, encoding='utf-8', errors='ignore')
        if result.returncode == 0:
            logger.info(f"Installed {module_name}. Output:\n{result.stdout}")
            bot.reply_to(message, f"Node package `{module_name}` installed locally.", parse_mode='Markdown')
            return True
        else:
            error_msg = f"Failed to install Node package `{module_name}`.\nLog:\n```\n{result.stderr or result.stdout}\n```"
            logger.error(error_msg)
            if len(error_msg) > 4000: error_msg = error_msg[:4000] + "\n... (Log truncated)"
            bot.reply_to(message, f"Failed to install Node package {module_name}.\n\n{result.stderr or result.stdout}\n\n(Troubleshoot: network blocked or package name invalid.)")
            return False
    except FileNotFoundError:
         error_msg = "Error: 'npm' not found. Ensure Node.js/npm are installed and in PATH."
         logger.error(error_msg)
         bot.reply_to(message, error_msg)
         return False
    except Exception as e:
        error_msg = f"Error installing Node package `{module_name}`: {str(e)}"
        logger.error(error_msg, exc_info=True)
        bot.reply_to(message, error_msg)
        return False

def run_script(script_path, script_owner_id, user_folder, file_name, message_obj_for_reply, attempt=1):
    file_status = get_file_status(script_owner_id, file_name)
    if file_status['status'] != FILE_STATUS_APPROVED:
        bot.reply_to(message_obj_for_reply,
                    f"❌ File `{file_name}` is not approved yet!\n"
                    f"📋 Status: **{file_status['status'].upper()}**\n"
                    f"⏳ Please wait for admin approval.",
                    parse_mode='Markdown')
        return
    
    max_attempts = 2
    if attempt > max_attempts:
        bot.reply_to(message_obj_for_reply, f"Failed to run '{file_name}' after {max_attempts} attempts. Check logs.")
        return

    script_key = f"{script_owner_id}_{file_name}"
    logger.info(f"Attempt {attempt} to run Python script: {script_path} (Key: {script_key}) for user {script_owner_id}")

    try:
        if not os.path.exists(script_path):
             bot.reply_to(message_obj_for_reply, f"Error: Script '{file_name}' not found at '{script_path}'!")
             logger.error(f"Script not found: {script_path} for user {script_owner_id}")
             if script_owner_id in user_files:
                 user_files[script_owner_id] = [f for f in user_files.get(script_owner_id, []) if f[0] != file_name]
             remove_user_file_db(script_owner_id, file_name)
             return

        if attempt == 1:
            check_command = [sys.executable, script_path]
            logger.info(f"Running Python pre-check: {' '.join(check_command)}")
            check_proc = None
            try:
                check_proc = subprocess.Popen(check_command, cwd=user_folder, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='ignore')
                stdout, stderr = check_proc.communicate(timeout=5)
                return_code = check_proc.returncode
                logger.info(f"Python Pre-check early. RC: {return_code}. Stderr: {stderr[:200]}...")
                if return_code != 0 and stderr:
                    match_py = re.search(r"ModuleNotFoundError: No module named '(.+?)'", stderr)
                    if match_py:
                        module_name = match_py.group(1).strip().strip("'\"")
                        logger.info(f"Detected missing Python module: {module_name}")
                        if attempt_install_pip(module_name, message_obj_for_reply):
                            logger.info(f"Install OK for {module_name}. Retrying run_script...")
                            bot.reply_to(message_obj_for_reply, f"Install successful. Retrying '{file_name}'...")
                            time.sleep(2)
                            threading.Thread(target=run_script, args=(script_path, script_owner_id, user_folder, file_name, message_obj_for_reply, attempt + 1)).start()
                            return
                        else:
                            bot.reply_to(message_obj_for_reply, f"Install failed. Cannot run '{file_name}'.")
                            return
                    else:
                         error_summary = stderr[:2000]
                         bot.reply_to(message_obj_for_reply, f"Error in script pre-check for '{file_name}':\n{error_summary}\n\nFix the script.")
                         return
            except subprocess.TimeoutExpired:
                logger.info("Python Pre-check timed out (>5s), imports likely OK. Killing check process.")
                if check_proc and check_proc.poll() is None: check_proc.kill(); check_proc.communicate()
                logger.info("Python Check process killed. Proceeding to long run.")
            except FileNotFoundError:
                 logger.error(f"Python interpreter not found: {sys.executable}")
                 bot.reply_to(message_obj_for_reply, f"Error: Python interpreter '{sys.executable}' not found.")
                 return
            except Exception as e:
                 logger.error(f"Error in Python pre-check for {script_key}: {e}", exc_info=True)
                 bot.reply_to(message_obj_for_reply, f"Unexpected error in script pre-check for '{file_name}': {e}")
                 return
            finally:
                 if check_proc and check_proc.poll() is None:
                     logger.warning(f"Python Check process {check_proc.pid} still running. Killing.")
                     check_proc.kill(); check_proc.communicate()

        logger.info(f"Starting long-running Python process for {script_key}")
        log_file_path = os.path.join(user_folder, f"{os.path.splitext(file_name)[0]}.log")
        log_file = None; process = None
        try: log_file = open(log_file_path, 'w', encoding='utf-8', errors='ignore')
        except Exception as e:
             logger.error(f"Failed to open log file '{log_file_path}' for {script_key}: {e}", exc_info=True)
             bot.reply_to(message_obj_for_reply, f"Failed to open log file '{log_file_path}': {e}")
             return
        try:
            startupinfo = None; creationflags = 0
            if os.name == 'nt':
                 startupinfo = subprocess.STARTUPINFO(); startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                 startupinfo.wShowWindow = subprocess.SW_HIDE
            run_env = os.environ.copy()
            try:
                db_env = _db_get_env(script_owner_id, file_name)
                run_env.update(db_env)
                _db_set_env(script_owner_id, file_name, db_env)
            except Exception as e:
                logger.error(f"Env inject error for {script_key}: {e}")
            process = subprocess.Popen(
                [sys.executable, script_path], cwd=user_folder, stdout=log_file, stderr=log_file,
                stdin=subprocess.PIPE, startupinfo=startupinfo, creationflags=creationflags,
                encoding='utf-8', errors='ignore', env=run_env
            )
            logger.info(f"Started Python process {process.pid} for {script_key}")
            bot_scripts[script_key] = {
                'process': process, 'log_file': log_file, 'file_name': file_name,
                'chat_id': message_obj_for_reply.chat.id,
                'script_owner_id': script_owner_id,
                'start_time': datetime.now(), 'user_folder': user_folder, 'type': 'py', 'script_key': script_key
            }
            bot.reply_to(message_obj_for_reply, f"Python script '{file_name}' started! (PID: {process.pid}) (For User: {script_owner_id})")
        except FileNotFoundError:
             logger.error(f"Python interpreter {sys.executable} not found for long run {script_key}")
             bot.reply_to(message_obj_for_reply, f"Error: Python interpreter '{sys.executable}' not found.")
             if log_file and not log_file.closed: log_file.close()
             if script_key in bot_scripts: del bot_scripts[script_key]
        except Exception as e:
            if log_file and not log_file.closed: log_file.close()
            error_msg = f"Error starting Python script '{file_name}': {str(e)}"
            logger.error(error_msg, exc_info=True)
            bot.reply_to(message_obj_for_reply, error_msg)
            if process and process.poll() is None:
                 logger.warning(f"Killing potentially started Python process {process.pid} for {script_key}")
                 kill_process_tree({'process': process, 'log_file': log_file, 'script_key': script_key})
            if script_key in bot_scripts: del bot_scripts[script_key]
    except Exception as e:
        error_msg = f"Unexpected error running Python script '{file_name}': {str(e)}"
        logger.error(error_msg, exc_info=True)
        bot.reply_to(message_obj_for_reply, error_msg)
        if script_key in bot_scripts:
             logger.warning(f"Cleaning up {script_key} due to error in run_script.")
             kill_process_tree(bot_scripts[script_key])
             del bot_scripts[script_key]

def run_js_script(script_path, script_owner_id, user_folder, file_name, message_obj_for_reply, attempt=1):
    file_status = get_file_status(script_owner_id, file_name)
    if file_status['status'] != FILE_STATUS_APPROVED:
        bot.reply_to(message_obj_for_reply,
                    f"❌ File `{file_name}` is not approved yet!\n"
                    f"📋 Status: **{file_status['status'].upper()}**\n"
                    f"⏳ Please wait for admin approval.",
                    parse_mode='Markdown')
        return
    
    max_attempts = 2
    if attempt > max_attempts:
        bot.reply_to(message_obj_for_reply, f"Failed to run '{file_name}' after {max_attempts} attempts. Check logs.")
        return

    script_key = f"{script_owner_id}_{file_name}"
    logger.info(f"Attempt {attempt} to run JS script: {script_path} (Key: {script_key}) for user {script_owner_id}")

    try:
        if not os.path.exists(script_path):
             bot.reply_to(message_obj_for_reply, f"Error: Script '{file_name}' not found at '{script_path}'!")
             logger.error(f"JS Script not found: {script_path} for user {script_owner_id}")
             if script_owner_id in user_files:
                 user_files[script_owner_id] = [f for f in user_files.get(script_owner_id, []) if f[0] != file_name]
             remove_user_file_db(script_owner_id, file_name)
             return

        if attempt == 1:
            check_command = ['node', script_path]
            logger.info(f"Running JS pre-check: {' '.join(check_command)}")
            check_proc = None
            try:
                check_proc = subprocess.Popen(check_command, cwd=user_folder, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='ignore')
                stdout, stderr = check_proc.communicate(timeout=5)
                return_code = check_proc.returncode
                logger.info(f"JS Pre-check early. RC: {return_code}. Stderr: {stderr[:200]}...")
                if return_code != 0 and stderr:
                    match_js = re.search(r"Cannot find module '(.+?)'", stderr)
                    if match_js:
                        module_name = match_js.group(1).strip().strip("'\"")
                        if not module_name.startswith('.') and not module_name.startswith('/'):
                             logger.info(f"Detected missing Node module: {module_name}")
                             if attempt_install_npm(module_name, user_folder, message_obj_for_reply):
                                 logger.info(f"NPM Install OK for {module_name}. Retrying run_js_script...")
                                 bot.reply_to(message_obj_for_reply, f"NPM Install successful. Retrying '{file_name}'...")
                                 time.sleep(2)
                                 threading.Thread(target=run_js_script, args=(script_path, script_owner_id, user_folder, file_name, message_obj_for_reply, attempt + 1)).start()
                                 return
                             else:
                                 bot.reply_to(message_obj_for_reply, f"NPM Install failed. Cannot run '{file_name}'.")
                                 return
                        else: logger.info(f"Skipping npm install for relative/core: {module_name}")
                    error_summary = stderr[:2000]
                    bot.reply_to(message_obj_for_reply, f"Error in JS script pre-check for '{file_name}':\n{error_summary}\n\nFix script or install manually.")
                    return
            except subprocess.TimeoutExpired:
                logger.info("JS Pre-check timed out (>5s), imports likely OK. Killing check process.")
                if check_proc and check_proc.poll() is None: check_proc.kill(); check_proc.communicate()
                logger.info("JS Check process killed. Proceeding to long run.")
            except FileNotFoundError:
                 error_msg = "Error: 'node' not found. Ensure Node.js is installed for JS files."
                 logger.error(error_msg)
                 bot.reply_to(message_obj_for_reply, error_msg)
                 return
            except Exception as e:
                 logger.error(f"Error in JS pre-check for {script_key}: {e}", exc_info=True)
                 bot.reply_to(message_obj_for_reply, f"Unexpected error in JS pre-check for '{file_name}': {e}")
                 return
            finally:
                 if check_proc and check_proc.poll() is None:
                     logger.warning(f"JS Check process {check_proc.pid} still running. Killing.")
                     check_proc.kill(); check_proc.communicate()

        logger.info(f"Starting long-running JS process for {script_key}")
        log_file_path = os.path.join(user_folder, f"{os.path.splitext(file_name)[0]}.log")
        log_file = None; process = None
        try: log_file = open(log_file_path, 'w', encoding='utf-8', errors='ignore')
        except Exception as e:
            logger.error(f"Failed to open log file '{log_file_path}' for JS script {script_key}: {e}", exc_info=True)
            bot.reply_to(message_obj_for_reply, f"Failed to open log file '{log_file_path}': {e}")
            return
        try:
            startupinfo = None; creationflags = 0
            if os.name == 'nt':
                 startupinfo = subprocess.STARTUPINFO(); startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                 startupinfo.wShowWindow = subprocess.SW_HIDE
            run_env = os.environ.copy()
            try:
                db_env = _db_get_env(script_owner_id, file_name)
                run_env.update(db_env)
                _db_set_env(script_owner_id, file_name, db_env)
            except Exception as e:
                logger.error(f"Env inject error for JS {script_key}: {e}")
            process = subprocess.Popen(
                ['node', script_path], cwd=user_folder, stdout=log_file, stderr=log_file,
                stdin=subprocess.PIPE, startupinfo=startupinfo, creationflags=creationflags,
                encoding='utf-8', errors='ignore', env=run_env
            )
            logger.info(f"Started JS process {process.pid} for {script_key}")
            bot_scripts[script_key] = {
                'process': process, 'log_file': log_file, 'file_name': file_name,
                'chat_id': message_obj_for_reply.chat.id,
                'script_owner_id': script_owner_id,
                'start_time': datetime.now(), 'user_folder': user_folder, 'type': 'js', 'script_key': script_key
            }
            bot.reply_to(message_obj_for_reply, f"JS script '{file_name}' started! (PID: {process.pid}) (For User: {script_owner_id})")
        except FileNotFoundError:
             error_msg = "Error: 'node' not found for long run. Ensure Node.js is installed."
             logger.error(error_msg)
             if log_file and not log_file.closed: log_file.close()
             bot.reply_to(message_obj_for_reply, error_msg)
             if script_key in bot_scripts: del bot_scripts[script_key]
        except Exception as e:
            if log_file and not log_file.closed: log_file.close()
            error_msg = f"Error starting JS script '{file_name}': {str(e)}"
            logger.error(error_msg, exc_info=True)
            bot.reply_to(message_obj_for_reply, error_msg)
            if process and process.poll() is None:
                 logger.warning(f"Killing potentially started JS process {process.pid} for {script_key}")
                 kill_process_tree({'process': process, 'log_file': log_file, 'script_key': script_key})
            if script_key in bot_scripts: del bot_scripts[script_key]
    except Exception as e:
        error_msg = f"Unexpected error running JS script '{file_name}': {str(e)}"
        logger.error(error_msg, exc_info=True)
        bot.reply_to(message_obj_for_reply, error_msg)
        if script_key in bot_scripts:
             logger.warning(f"Cleaning up {script_key} due to error in run_js_script.")
             kill_process_tree(bot_scripts[script_key])
             del bot_scripts[script_key]

def save_user_file(user_id, file_name, file_type='py'):
    try:
        db.user_files.replace_one(
            {'user_id': user_id, 'file_name': file_name},
            {'user_id': user_id, 'file_name': file_name, 'file_type': file_type},
            upsert=True)
        if user_id not in user_files: user_files[user_id] = []
        user_files[user_id] = [(fn, ft) for fn, ft in user_files[user_id] if fn != file_name]
        user_files[user_id].append((file_name, file_type))
        logger.info(f"Saved file '{file_name}' ({file_type}) for user {user_id}")
    except Exception as e: logger.error(f"Unexpected error saving file for {user_id}, {file_name}: {e}", exc_info=True)

def remove_user_file_db(user_id, file_name):
    try:
        db.user_files.delete_one({'user_id': user_id, 'file_name': file_name})
        if user_id in user_files:
            user_files[user_id] = [f for f in user_files[user_id] if f[0] != file_name]
            if not user_files[user_id]: del user_files[user_id]
        logger.info(f"Removed file '{file_name}' for user {user_id} from DB")
    except Exception as e: logger.error(f"Unexpected error removing file for {user_id}, {file_name}: {e}", exc_info=True)

def add_active_user(user_id):
    active_users.add(user_id)
    try:
        db.active_users.replace_one({'user_id': user_id}, {'user_id': user_id}, upsert=True)
        logger.info(f"Added/Confirmed active user {user_id} in DB")
    except Exception as e: logger.error(f"Unexpected error adding active user {user_id}: {e}", exc_info=True)

def save_subscription(user_id, expiry, plan='pro'):
    try:
        expiry_str = expiry.isoformat()
        plan = plan if plan in PLANS else 'pro'
        db.subscriptions.replace_one(
            {'user_id': user_id},
            {'user_id': user_id, 'expiry': expiry_str, 'plan': plan},
            upsert=True)
        user_subscriptions[user_id] = {'expiry': expiry, 'plan': plan}
        logger.info(f"Saved subscription for {user_id}, expiry {expiry_str}, plan {plan}")
    except Exception as e: logger.error(f"Unexpected error saving subscription for {user_id}: {e}", exc_info=True)

def remove_subscription_db(user_id):
    try:
        db.subscriptions.delete_one({'user_id': user_id})
        if user_id in user_subscriptions: del user_subscriptions[user_id]
        logger.info(f"Removed subscription for {user_id} from DB")
    except Exception as e: logger.error(f"Unexpected error removing subscription for {user_id}: {e}", exc_info=True)

def add_admin_db(admin_id):
    try:
        db.admins.replace_one({'user_id': admin_id}, {'user_id': admin_id}, upsert=True)
        admin_ids.add(admin_id)
        logger.info(f"Added admin {admin_id} to DB")
    except Exception as e: logger.error(f"Unexpected error adding admin {admin_id}: {e}", exc_info=True)

def remove_admin_db(admin_id):
    if admin_id == OWNER_ID:
        logger.warning("Attempted to remove OWNER_ID from admins.")
        return False
    try:
        removed = db.admins.delete_one({'user_id': admin_id}).deleted_count > 0
        if removed:
            admin_ids.discard(admin_id)
            logger.info(f"Removed admin {admin_id} from DB")
        else:
            logger.warning(f"Admin {admin_id} not found in DB.")
            admin_ids.discard(admin_id)
        return removed
    except Exception as e: logger.error(f"Unexpected error removing admin {admin_id}: {e}", exc_info=True); return False

def create_main_menu_inline(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = [
        types.InlineKeyboardButton('📢 Updates Channel', url=UPDATE_CHANNEL),
        types.InlineKeyboardButton('📤 Upload File', callback_data='upload'),
        types.InlineKeyboardButton('📂 Check Files', callback_data='check_files'),
        types.InlineKeyboardButton('⚡ Bot Speed', callback_data='speed'),
        types.InlineKeyboardButton('📊 Statistics', callback_data='stats'),
        types.InlineKeyboardButton('📞 Contact Owner', url=f'https://t.me/{YOUR_USERNAME.replace("@", "")}'),
        types.InlineKeyboardButton('🤖 MPX AI', callback_data='mpx_ai')
    ]

    if user_id in admin_ids:
        pending_count = get_pending_files_count()
        pending_text = f"📋 Pending Files ({pending_count})" if pending_count > 0 else "📋 Pending Files"
        
        admin_buttons = [
            types.InlineKeyboardButton(pending_text, callback_data='view_pending'),
            types.InlineKeyboardButton('💳 Subscriptions', callback_data='subscription'),
            types.InlineKeyboardButton('📢 Broadcast', callback_data='broadcast'),
            types.InlineKeyboardButton('🔒 Lock Bot' if not bot_locked else '🔓 Unlock Bot',
                                     callback_data='lock_bot' if not bot_locked else 'unlock_bot'),
            types.InlineKeyboardButton('👑 Admin Panel', callback_data='admin_panel'),
            types.InlineKeyboardButton('🟢 Run All User Scripts', callback_data='run_all_scripts')
        ]
        markup.add(buttons[0])
        markup.add(buttons[1], buttons[2])
        markup.add(buttons[3], admin_buttons[0])
        markup.add(buttons[4], admin_buttons[1])
        markup.add(admin_buttons[2], admin_buttons[4])
        markup.add(admin_buttons[3])
        markup.add(buttons[5], buttons[6])
    else:
        markup.add(buttons[0])
        markup.add(buttons[1], buttons[2])
        markup.add(buttons[3])
        markup.add(buttons[4])
        markup.add(buttons[5], buttons[6])

    markup.add(types.InlineKeyboardButton('⏱ Uptime', callback_data='uptime'))
    return markup

def create_reply_keyboard_main_menu(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    layout_to_use = ADMIN_COMMAND_BUTTONS_LAYOUT_USER_SPEC if user_id in admin_ids else COMMAND_BUTTONS_LAYOUT_USER_SPEC
    for row_buttons_text in layout_to_use:
        markup.add(*[types.KeyboardButton(text) for text in row_buttons_text])
    return markup

def create_control_buttons(script_owner_id, file_name, is_running=True):
    markup = types.InlineKeyboardMarkup(row_width=2)
    
    file_status = get_file_status(script_owner_id, file_name)
    status_text = "✅ Approved" if file_status['status'] == FILE_STATUS_APPROVED else \
                 "⏳ Pending" if file_status['status'] == FILE_STATUS_PENDING else \
                 "❌ Rejected"
    
    if is_running:
        markup.row(
            types.InlineKeyboardButton("🔴 Stop", callback_data=f'stop_{script_owner_id}_{file_name}'),
            types.InlineKeyboardButton("🔄 Restart", callback_data=f'restart_{script_owner_id}_{file_name}')
        )
        markup.row(
            types.InlineKeyboardButton("🗑️ Delete", callback_data=f'delete_{script_owner_id}_{file_name}'),
            types.InlineKeyboardButton("📜 Logs", callback_data=f'logs_{script_owner_id}_{file_name}')
        )
    else:
        markup.row(
            types.InlineKeyboardButton("🟢 Start", callback_data=f'start_{script_owner_id}_{file_name}'),
            types.InlineKeyboardButton("🗑️ Delete", callback_data=f'delete_{script_owner_id}_{file_name}')
        )
        markup.row(
            types.InlineKeyboardButton("📜 View Logs", callback_data=f'logs_{script_owner_id}_{file_name}')
        )
    
    markup.add(types.InlineKeyboardButton(f"Status: {status_text}", callback_data=f'status_{script_owner_id}_{file_name}'))
    markup.add(types.InlineKeyboardButton("🔙 Back to Files", callback_data='check_files'))
    return markup

def create_admin_panel():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.row(
        types.InlineKeyboardButton('➕ Add Admin', callback_data='add_admin'),
        types.InlineKeyboardButton('➖ Remove Admin', callback_data='remove_admin')
    )
    markup.row(
        types.InlineKeyboardButton('📋 List Admins', callback_data='list_admins'),
        types.InlineKeyboardButton('📋 Pending Files', callback_data='view_pending')
    )
    markup.row(types.InlineKeyboardButton('🔙 Back to Main', callback_data='back_to_main'))
    return markup

def create_subscription_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.row(
        types.InlineKeyboardButton('➕ Add Subscription', callback_data='add_subscription'),
        types.InlineKeyboardButton('➖ Remove Subscription', callback_data='remove_subscription')
    )
    markup.row(types.InlineKeyboardButton('🔍 Check Subscription', callback_data='check_subscription'))
    markup.row(types.InlineKeyboardButton('🔙 Back to Main', callback_data='back_to_main'))
    return markup

def create_pending_files_list():
    markup = types.InlineKeyboardMarkup(row_width=1)
    pending_files = get_all_pending_files()
    
    if not pending_files:
        return None
    
    for user_id, file_name, file_type, uploaded_time in pending_files:
        try:
            uploaded_dt = datetime.fromisoformat(uploaded_time)
            time_ago = datetime.now() - uploaded_dt
            minutes = int(time_ago.total_seconds() / 60)
            time_text = f"{minutes}m ago" if minutes < 60 else f"{int(minutes/60)}h ago"
            
            btn_text = f"👤 {user_id} | 📁 {file_name} | ⏰ {time_text}"
            callback_data = f'review_{user_id}_{file_name}'
            markup.add(types.InlineKeyboardButton(btn_text, callback_data=callback_data))
        except:
            btn_text = f"👤 {user_id} | 📁 {file_name}"
            callback_data = f'review_{user_id}_{file_name}'
            markup.add(types.InlineKeyboardButton(btn_text, callback_data=callback_data))
    
    markup.add(types.InlineKeyboardButton("🔄 Refresh", callback_data='view_pending'))
    markup.add(types.InlineKeyboardButton("🔙 Back to Main", callback_data='back_to_main'))
    return markup

def _register_upload(user_id, file_name, file_type, message):
    """Register an uploaded file. Owner/admin uploads are auto-approved;
    other users' uploads go to the admin approval queue."""
    save_user_file(user_id, file_name, file_type)
    if user_id in admin_ids:
        save_file_approval(user_id, file_name, file_type, FILE_STATUS_APPROVED)
        bot.send_message(user_id,
                         f"✅ File `{file_name}` auto-approved (admin)!\n"
                         f"🚀 You can run it now with /checkfiles or the web dashboard.",
                         parse_mode='Markdown')
        logger.info(f"Auto-approved admin upload: {user_id}/{file_name}")
        return FILE_STATUS_APPROVED
    save_file_approval(user_id, file_name, file_type, FILE_STATUS_PENDING)
    send_file_for_approval(message, user_id, file_name, file_type)
    return FILE_STATUS_PENDING

def handle_zip_file(downloaded_file_content, file_name_zip, message):
    user_id = message.from_user.id
    user_folder = get_user_folder(user_id)
    temp_dir = None
    try:
        temp_dir = tempfile.mkdtemp(prefix=f"user_{user_id}_zip_")
        logger.info(f"Temp dir for zip: {temp_dir}")
        zip_path = os.path.join(temp_dir, file_name_zip)
        with open(zip_path, 'wb') as new_file: new_file.write(downloaded_file_content)
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            for member in zip_ref.infolist():
                member_path = os.path.abspath(os.path.join(temp_dir, member.filename))
                if not member_path.startswith(os.path.abspath(temp_dir)):
                    raise zipfile.BadZipFile(f"Zip has unsafe path: {member.filename}")
            zip_ref.extractall(temp_dir)
            logger.info(f"Extracted zip to {temp_dir}")

        # Flatten a single wrapper folder (zips of a project usually wrap in one),
        # even when the zip also has top-level files like .env or requirements.txt.
        top_entries = os.listdir(temp_dir)
        top_dirs = [d for d in top_entries if os.path.isdir(os.path.join(temp_dir, d))]
        if len(top_dirs) == 1:
            inner = os.path.join(temp_dir, top_dirs[0])
            for item in os.listdir(inner):
                dest = os.path.join(temp_dir, item)
                if os.path.exists(dest):
                    if os.path.isdir(dest): shutil.rmtree(dest)
                    else: os.remove(dest)
                shutil.move(os.path.join(inner, item), temp_dir)
            os.rmdir(inner)
            logger.info(f"Flattened wrapper folder '{top_dirs[0]}'")

        # Search recursively so scripts inside subfolders are found too.
        extracted = []
        for root, dirs, files in os.walk(temp_dir):
            for f in files:
                extracted.append(os.path.relpath(os.path.join(root, f), temp_dir))
        py_files = [f for f in extracted if f.endswith('.py')]
        js_files = [f for f in extracted if f.endswith('.js')]
        req_file = 'requirements.txt' if 'requirements.txt' in extracted else None
        pkg_json = 'package.json' if 'package.json' in extracted else None

        # Parse a .env shipped in the zip NOW (before files are moved) so it can
        # be stored in MongoDB and used by the dashboard Env button + runners.
        zip_env_data = {}
        env_rel = None
        if '.env' in extracted:
            env_rel = '.env'
        else:
            for e in extracted:
                if os.path.basename(e) == '.env':
                    env_rel = e
                    break
        if env_rel:
            env_abs = os.path.join(temp_dir, env_rel)
            if os.path.isfile(env_abs):
                try:
                    with open(env_abs, 'r', encoding='utf-8', errors='ignore') as f:
                        zip_env_data = _parse_env_text(f.read())
                    logger.info(f"Parsed .env from zip for {user_id}: {list(zip_env_data.keys())}")
                except Exception as e:
                    logger.error(f"Failed to parse zip .env for {user_id}: {e}")

        if req_file:
            req_path = os.path.join(temp_dir, req_file)
            logger.info(f"requirements.txt found, installing: {req_path}")
            bot.reply_to(message, f"Installing Python deps from `{req_file}`...")
            try:
                command = [sys.executable, '-m', 'pip', 'install', '-r', req_path]
                result = subprocess.run(command, capture_output=True, text=True, check=True, encoding='utf-8', errors='ignore')
                logger.info(f"pip install from requirements.txt OK. Output:\n{result.stdout}")
                bot.reply_to(message, f"Python deps from `{req_file}` installed.")
            except subprocess.CalledProcessError as e:
                error_msg = f"Failed to install Python deps from `{req_file}`.\nLog:\n```\n{e.stderr or e.stdout}\n```"
                logger.error(error_msg)
                if len(error_msg) > 4000: error_msg = error_msg[:4000] + "\n... (Log truncated)"
                bot.reply_to(message, f"Failed to install Python deps from {req_file}.\n\n{e.stderr or e.stdout}\n\n(Troubleshoot: network blocked or package name invalid.)"); return
            except Exception as e:
                 error_msg = f"Unexpected error installing Python deps: {e}"
                 logger.error(error_msg, exc_info=True); bot.reply_to(message, error_msg); return

        if pkg_json:
            logger.info(f"package.json found, npm install in: {temp_dir}")
            bot.reply_to(message, f"Installing Node deps from `{pkg_json}`...")
            try:
                command = ['npm', 'install']
                result = subprocess.run(command, capture_output=True, text=True, check=True, cwd=temp_dir, encoding='utf-8', errors='ignore')
                logger.info(f"npm install OK. Output:\n{result.stdout}")
                bot.reply_to(message, f"Node deps from `{pkg_json}` installed.")
            except FileNotFoundError:
                bot.reply_to(message, "'npm' not found. Cannot install Node deps."); return
            except subprocess.CalledProcessError as e:
                error_msg = f"Failed to install Node deps from `{pkg_json}`.\nLog:\n```\n{e.stderr or e.stdout}\n```"
                logger.error(error_msg)
                if len(error_msg) > 4000: error_msg = error_msg[:4000] + "\n... (Log truncated)"
                bot.reply_to(message, f"Failed to install Node deps from {pkg_json}.\n\n{e.stderr or e.stdout}\n\n(Troubleshoot: network blocked or package name invalid.)"); return
            except Exception as e:
                 error_msg = f"Unexpected error installing Node deps: {e}"
                 logger.error(error_msg, exc_info=True); bot.reply_to(message, error_msg); return

        main_script_name = None; file_type = None
        preferred_py = ['main.py', 'bot.py', 'app.py']; preferred_js = ['index.js', 'main.js', 'bot.js', 'app.js']
        for p in preferred_py:
            if p in py_files: main_script_name = p; file_type = 'py'; break
        if not main_script_name:
             for p in preferred_js:
                 if p in js_files: main_script_name = p; file_type = 'js'; break
        if not main_script_name:
            if py_files: main_script_name = py_files[0]; file_type = 'py'
            elif js_files: main_script_name = js_files[0]; file_type = 'js'
        if not main_script_name:
            bot.reply_to(message, "No `.py` or `.js` script found in archive!"); return

        # Make sure the main script sits at the root of the project (so the
        # runner/start/stop/logs find it by name) - done inside temp_dir since
        # the files are now snapshotted into MongoDB instead of saved to disk.
        if not os.path.exists(os.path.join(temp_dir, main_script_name)):
            for root, dirs, files in os.walk(temp_dir):
                if main_script_name in files:
                    shutil.move(os.path.join(root, main_script_name), os.path.join(temp_dir, main_script_name))
                    break

        # Snapshot the extracted project into MongoDB (files live in the DB,
        # not persistently on the VPS disk). Skip generated node_modules dirs.
        project_files = []
        for sub_root, sub_dirs, sub_files in os.walk(temp_dir):
            sub_dirs[:] = [d for d in sub_dirs if d != 'node_modules']
            for f in sub_files:
                snap_abs = os.path.join(sub_root, f)
                try:
                    with open(snap_abs, 'rb') as sf:
                        project_files.append({'path': os.path.relpath(snap_abs, temp_dir).replace(os.sep, '/'),
                                              'content': sf.read()})
                except Exception as e:
                    logger.error(f"Error reading {snap_abs} for DB snapshot: {e}")
        if not _store_project_data(user_id, main_script_name, file_type, project_files, is_project=True):
            logger.error(f"Failed to store project {main_script_name} for {user_id} in DB")
            bot.reply_to(message, "Failed to store the project in the database. Please try again.")
            return
        logger.info(f"Stored project '{main_script_name}' ({file_type}, {len(project_files)} files) for {user_id} in DB")

        # Store the zip's .env (parsed before) so the dashboard Env button
        # reads/edits it and runners inject it at launch.
        base_name = os.path.basename(main_script_name)
        for old_fn, _old_ft in list(user_files.get(user_id, [])):
            if old_fn != main_script_name and os.path.basename(old_fn) == base_name:
                logger.info(f"Removing stale duplicate '{old_fn}' before registering '{main_script_name}'")
                remove_user_file_db(user_id, old_fn)
                try: _destroy_project(user_id, old_fn)
                except Exception as e: logger.error(f"Failed to destroy stale project files for {old_fn}: {e}")
                try: db.user_file_data.delete_one({'user_id': user_id, 'file_name': old_fn})
                except Exception as e: logger.error(f"Failed to delete stale DB data for {old_fn}: {e}")
                try: db.user_env.delete_one({'user_id': user_id, 'file_name': old_fn})
                except Exception as e: logger.error(f"Failed to delete stale env for {old_fn}: {e}")

        status = _register_upload(user_id, main_script_name, file_type, message)

        if zip_env_data:
            _db_set_env(user_id, main_script_name, zip_env_data)

        logger.info(f"Saved main script '{main_script_name}' ({file_type}) for {user_id} from zip.")
        if status == FILE_STATUS_APPROVED:
            bot.reply_to(message,
                        f"✅ Files extracted successfully!\n"
                        f"📁 Main script: `{main_script_name}`\n"
                        f"📋 Status: **AUTO-APPROVED** (admin)\n"
                        f"🚀 You can run it now with /checkfiles or the web dashboard.",
                        parse_mode='Markdown')
        else:
            bot.reply_to(message,
                        f"✅ Files extracted successfully!\n"
                        f"📁 Main script: `{main_script_name}`\n"
                        f"📋 Status: **PENDING APPROVAL**\n"
                        f"👮‍♂️ Admins have been notified.\n"
                        f"You'll receive a notification when approved.",
                        parse_mode='Markdown')

    except zipfile.BadZipFile as e:
        logger.error(f"Bad zip file from {user_id}: {e}")
        bot.reply_to(message, f"Error: Invalid/corrupted ZIP. {e}")
    except Exception as e:
        logger.error(f"Error processing zip for {user_id}: {e}", exc_info=True)
        bot.reply_to(message, f"Error processing zip: {str(e)}")
    finally:
        if temp_dir and os.path.exists(temp_dir):
            try: shutil.rmtree(temp_dir); logger.info(f"Cleaned temp dir: {temp_dir}")
            except Exception as e: logger.error(f"Failed to clean temp dir {temp_dir}: {e}", exc_info=True)

def handle_js_file(file_path, script_owner_id, user_folder, file_name, message):
    try:
        status = _register_upload(script_owner_id, file_name, 'js', message)
        if status == FILE_STATUS_APPROVED:
            bot.reply_to(message,
                        f"✅ JS file `{file_name}` uploaded successfully!\n"
                        f"📋 Status: **AUTO-APPROVED** (admin)\n"
                        f"🚀 You can run it now with /checkfiles or the web dashboard.",
                        parse_mode='Markdown')
        else:
            bot.reply_to(message,
                        f"✅ JS file `{file_name}` uploaded successfully!\n"
                        f"📋 Status: **PENDING APPROVAL**\n"
                        f"👮‍♂️ Admins have been notified.\n"
                        f"You'll receive a notification when approved.",
                        parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Error processing JS file {file_name} for {script_owner_id}: {e}", exc_info=True)
        bot.reply_to(message, f"Error processing JS file: {str(e)}")

def handle_py_file(file_path, script_owner_id, user_folder, file_name, message):
    try:
        status = _register_upload(script_owner_id, file_name, 'py', message)
        if status == FILE_STATUS_APPROVED:
            bot.reply_to(message,
                        f"✅ Python file `{file_name}` uploaded successfully!\n"
                        f"📋 Status: **AUTO-APPROVED** (admin)\n"
                        f"🚀 You can run it now with /checkfiles or the web dashboard.",
                        parse_mode='Markdown')
        else:
            bot.reply_to(message,
                        f"✅ Python file `{file_name}` uploaded successfully!\n"
                        f"📋 Status: **PENDING APPROVAL**\n"
                        f"👮‍♂️ Admins have been notified.\n"
                        f"You'll receive a notification when approved.",
                        parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Error processing Python file {file_name} for {script_owner_id}: {e}", exc_info=True)
        bot.reply_to(message, f"Error processing Python file: {str(e)}")

def _logic_send_welcome(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    user_name = message.from_user.first_name
    user_username = message.from_user.username

    logger.info(f"Welcome request from user_id: {user_id}, username: @{user_username}")

    if bot_locked and user_id not in admin_ids:
        bot.send_message(chat_id, "Bot locked by admin. Try later.")
        return

    user_bio = "Could not fetch bio"; photo_file_id = None
    try: user_bio = bot.get_chat(user_id).bio or "No bio"
    except Exception: pass
    try:
        user_profile_photos = bot.get_user_profile_photos(user_id, limit=1)
        if user_profile_photos.photos: photo_file_id = user_profile_photos.photos[0][-1].file_id
    except Exception: pass

    if user_id not in active_users:
        add_active_user(user_id)
        try:
            owner_notification = (f"New user!\nName: {user_name}\nUser: @{user_username or 'N/A'}\n"
                                  f"ID: `{user_id}`\nBio: {user_bio}")
            bot.send_message(OWNER_ID, owner_notification, parse_mode='Markdown')
            if photo_file_id: bot.send_photo(OWNER_ID, photo_file_id, caption=f"Pic of new user {user_id}")
        except Exception as e: logger.error(f"Failed to notify owner about new user {user_id}: {e}")

    file_limit = get_user_file_limit(user_id)
    current_files = get_user_file_count(user_id)
    limit_str = str(file_limit) if file_limit != float('inf') else "Unlimited"
    expiry_info = ""
    if user_id == OWNER_ID: user_status = "Owner"
    elif user_id in admin_ids: user_status = "Admin"
    elif user_id in user_subscriptions:
        expiry_date = user_subscriptions[user_id].get('expiry')
        if expiry_date and expiry_date > datetime.now():
            plan_key = user_subscriptions[user_id].get('plan', 'pro')
            plan_label = PLANS.get(plan_key, PLANS['pro'])['name']
            user_status = f"Premium ({plan_label})"
            days_left = (expiry_date - datetime.now()).days
            expiry_info = f"\nSubscription expires in: {days_left} days"
        else: user_status = "Free User (Expired Sub)"; remove_subscription_db(user_id)
    else: user_status = "Free User"

    welcome_msg_text = (f"👋 <b>Welcome, {user_name}!</b>\n\n"
                        f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
                        f"👤 <b>Username:</b> @{user_username or 'Not set'}\n"
                        f"🎖 <b>Status:</b> {user_status}{expiry_info}\n"
                        f"📁 <b>Files:</b> {current_files} / {limit_str}\n\n"
                        f"🚀 <b>Host & run your Python / JS bots</b>\n"
                        f"Upload single <code>.py</code>/<code>.js</code> scripts or "
                        f"a <code>.zip</code> project. That's it!\n\n"
                        f"💡 Use the menu buttons below or type commands like "
                        f"<code>/checkfiles</code> and <code>/web</code>.")
    main_reply_markup = create_reply_keyboard_main_menu(user_id)
    welcome_buttons = types.InlineKeyboardMarkup()
    welcome_buttons.add(types.InlineKeyboardButton("🌐 Open Web Dashboard", url=WEB_URL))
    try:
        if photo_file_id: bot.send_photo(chat_id, photo_file_id)
        bot.send_message(chat_id, welcome_msg_text, reply_markup=main_reply_markup, parse_mode='HTML')
        bot.send_message(chat_id, "Manage everything from your browser 👇", reply_markup=welcome_buttons)
    except Exception as e:
        logger.error(f"Error sending welcome to {user_id}: {e}", exc_info=True)
        try: bot.send_message(chat_id, welcome_msg_text, reply_markup=main_reply_markup, parse_mode='HTML')
        except Exception as fallback_e: logger.error(f"Fallback send_message failed for {user_id}: {fallback_e}")

def _logic_web_dashboard(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🌐 Open Web Dashboard", url=WEB_URL))
    markup.add(types.InlineKeyboardButton("🔐 Login", url=f"{WEB_URL}/login.html"))
    bot.reply_to(message,
                 "🌐 <b>HostBot Web Dashboard</b>\n\n"
                 "Manage all your bots from the browser: start, stop, view logs, "
                 "edit env vars and delete files.\n\n"
                 "🔐 Log in with your username & password (or /register in this bot "
                 "to create an account).",
                 parse_mode='HTML', reply_markup=markup)

def _logic_updates_channel(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton('📢 Updates Channel', url=UPDATE_CHANNEL))
    bot.reply_to(message, "Visit our Updates Channel:", reply_markup=markup)

def _logic_upload_file(message):
    user_id = message.from_user.id
    if bot_locked and user_id not in admin_ids:
        bot.reply_to(message, "Bot locked by admin, cannot accept files.")
        return

    file_limit = get_user_file_limit(user_id)
    current_files = get_user_file_count(user_id)
    if current_files >= file_limit:
        limit_str = str(file_limit) if file_limit != float('inf') else "Unlimited"
        bot.reply_to(message, f"File limit ({current_files}/{limit_str}) reached. Delete files first.")
        return
    bot.reply_to(message, 
                "Send your Python (`.py`), JS (`.js`), or ZIP (`.zip`) file.\n\n"
                "⚠️ **Note:** All files require admin approval before running.")

def _logic_check_files(message):
    user_id = message.from_user.id
    user_files_list = user_files.get(user_id, [])
    if not user_files_list:
        bot.reply_to(message, "Your files:\n\n(No files uploaded yet)")
        return
    
    response = "📁 **Your Files:**\n\n"
    markup = types.InlineKeyboardMarkup(row_width=1)
    
    for file_name, file_type in sorted(user_files_list):
        is_running = is_bot_running(user_id, file_name)
        file_status = get_file_status(user_id, file_name)
        
        status_icon = "🟢" if is_running else "⚪"
        approval_icon = "✅" if file_status['status'] == FILE_STATUS_APPROVED else \
                       "⏳" if file_status['status'] == FILE_STATUS_PENDING else "❌"
        
        btn_text = f"{approval_icon} {file_name} ({file_type}) - {status_icon}"
        markup.add(types.InlineKeyboardButton(btn_text, callback_data=f'file_{user_id}_{file_name}'))
        
        approval_text = "Approved" if file_status['status'] == FILE_STATUS_APPROVED else \
                       "Pending" if file_status['status'] == FILE_STATUS_PENDING else "Rejected"
        response += f"{approval_icon} `{file_name}` - {approval_text}\n"
    
    bot.reply_to(message, response, reply_markup=markup, parse_mode='Markdown')

def _logic_view_pending(message):
    user_id = message.from_user.id
    if user_id not in admin_ids:
        bot.reply_to(message, "Admin permissions required.")
        return
    
    pending_files = get_all_pending_files()
    if not pending_files:
        bot.reply_to(message, "✅ No pending files for approval.")
        return
    
    response = "📋 **Pending Files for Approval:**\n\n"
    markup = types.InlineKeyboardMarkup(row_width=1)
    
    for idx, (user_id_file, file_name, file_type, uploaded_time) in enumerate(pending_files[:20], 1):
        try:
            uploaded_dt = datetime.fromisoformat(uploaded_time)
            time_ago = datetime.now() - uploaded_dt
            minutes = int(time_ago.total_seconds() / 60)
            time_text = f"{minutes}m ago" if minutes < 60 else f"{int(minutes/60)}h ago"
            
            btn_text = f"{idx}. 👤 {user_id_file} | 📁 {file_name} | ⏰ {time_text}"
            response += f"{idx}. `{file_name}` (User: {user_id_file}, Type: {file_type}) - {time_text}\n"
        except:
            btn_text = f"{idx}. 👤 {user_id_file} | 📁 {file_name}"
            response += f"{idx}. `{file_name}` (User: {user_id_file}, Type: {file_type})\n"
        
        callback_data = f'review_{user_id_file}_{file_name}'
        markup.add(types.InlineKeyboardButton(btn_text, callback_data=callback_data))
    
    if len(pending_files) > 20:
        response += f"\n... and {len(pending_files) - 20} more files."
    
    markup.add(types.InlineKeyboardButton("🔄 Refresh", callback_data='view_pending'))
    markup.add(types.InlineKeyboardButton("🔙 Back to Main", callback_data='back_to_main'))
    
    bot.reply_to(message, response, reply_markup=markup, parse_mode='Markdown')

def _logic_bot_speed(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    start_time_ping = time.time()
    wait_msg = bot.reply_to(message, "Testing speed...")
    try:
        bot.send_chat_action(chat_id, 'typing')
        response_time = round((time.time() - start_time_ping) * 1000, 2)
        status = "Unlocked" if not bot_locked else "Locked"
        if user_id == OWNER_ID: user_level = "Owner"
        elif user_id in admin_ids: user_level = "Admin"
        elif user_id in user_subscriptions and user_subscriptions[user_id].get('expiry', datetime.min) > datetime.now(): user_level = "Premium"
        else: user_level = "Free User"
        
        speed_msg = (f"Bot Speed & Status:\n\nAPI Response Time: {response_time} ms\n"
                     f"Bot Status: {status}\n"
                     f"Your Level: {user_level}")
        
        if user_id in admin_ids:
            pending_count = get_pending_files_count()
            speed_msg += f"\n📋 Pending Files: {pending_count}"
            
        bot.edit_message_text(speed_msg, chat_id, wait_msg.message_id)
    except Exception as e:
        logger.error(f"Error during speed test (cmd): {e}", exc_info=True)
        bot.edit_message_text("Error during speed test.", chat_id, wait_msg.message_id)

def _logic_contact_owner(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton('📞 Contact Owner', url=f'https://t.me/{YOUR_USERNAME.replace("@", "")}'))
    bot.reply_to(message, "Click to contact Owner:", reply_markup=markup)

def _logic_uptime(message):
    uptime_str = get_uptime()
    bot.reply_to(message, f"Bot Uptime: `{uptime_str}`", parse_mode='Markdown')

def _logic_subscriptions_panel(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "Admin permissions required.")
        return
    bot.reply_to(message, "Subscription Management\nUse inline buttons from /start or admin command menu.", reply_markup=create_subscription_menu())

def _logic_statistics(message):
    user_id = message.from_user.id
    total_users = len(active_users)
    total_files_records = sum(len(files) for files in user_files.values())

    running_bots_count = 0
    user_running_bots = 0

    for script_key_iter, script_info_iter in list(bot_scripts.items()):
        s_owner_id, _ = script_key_iter.split('_', 1)
        if is_bot_running(int(s_owner_id), script_info_iter['file_name']):
            running_bots_count += 1
            if int(s_owner_id) == user_id:
                user_running_bots +=1

    stats_msg_base = (f"Bot Statistics:\n\n"
                      f"Total Users: {total_users}\n"
                      f"Total File Records: {total_files_records}\n"
                      f"Total Active Bots: {running_bots_count}\n")

    if user_id in admin_ids:
        pending_count = get_pending_files_count()
        approved_count = sum(1 for uid in user_files for fn, _ in user_files[uid] 
                           if get_file_status(uid, fn)['status'] == FILE_STATUS_APPROVED)
        
        stats_msg_admin = (f"Bot Status: {'Locked' if bot_locked else 'Unlocked'}\n"
                           f"Your Running Bots: {user_running_bots}\n"
                           f"📊 **Approval Stats:**\n"
                           f"   ✅ Approved Files: {approved_count}\n"
                           f"   ⏳ Pending Files: {pending_count}")
        stats_msg = stats_msg_base + stats_msg_admin
    else:
        stats_msg = stats_msg_base + f"Your Running Bots: {user_running_bots}"

    bot.reply_to(message, stats_msg)

def _logic_broadcast_init(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "Admin permissions required.")
        return
    msg = bot.reply_to(message, "Send message to broadcast to all active users.\n/cancel to abort.")
    bot.register_next_step_handler(msg, process_broadcast_message)

def _logic_toggle_lock_bot(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "Admin permissions required.")
        return
    global bot_locked
    bot_locked = not bot_locked
    status = "locked" if bot_locked else "unlocked"
    logger.warning(f"Bot {status} by Admin {message.from_user.id} via command/button.")
    bot.reply_to(message, f"Bot has been {status}.")

def _logic_admin_panel(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "Admin permissions required.")
        return
    bot.reply_to(message, "Admin Panel\nManage admins. Use inline buttons from /start or admin menu.",
                 reply_markup=create_admin_panel())

def _logic_run_all_scripts(message_or_call):
    if isinstance(message_or_call, telebot.types.Message):
        admin_user_id = message_or_call.from_user.id
        admin_chat_id = message_or_call.chat.id
        reply_func = lambda text, **kwargs: bot.reply_to(message_or_call, text, **kwargs)
        admin_message_obj_for_script_runner = message_or_call
    elif isinstance(message_or_call, telebot.types.CallbackQuery):
        admin_user_id = message_or_call.from_user.id
        admin_chat_id = message_or_call.message.chat.id
        bot.answer_callback_query(message_or_call.id)
        reply_func = lambda text, **kwargs: bot.send_message(admin_chat_id, text, **kwargs)
        admin_message_obj_for_script_runner = message_or_call.message
    else:
        logger.error("Invalid argument for _logic_run_all_scripts")
        return

    if admin_user_id not in admin_ids:
        reply_func("Admin permissions required.")
        return

    reply_func("Starting process to run all user scripts. This may take a while...")
    logger.info(f"Admin {admin_user_id} initiated 'run all scripts' from chat {admin_chat_id}.")

    started_count = 0; attempted_users = 0; skipped_files = 0; error_files_details = []

    all_user_files_snapshot = dict(user_files)

    for target_user_id, files_for_user in all_user_files_snapshot.items():
        if not files_for_user: continue
        attempted_users += 1
        logger.info(f"Processing scripts for user {target_user_id}...")
        user_folder = get_user_folder(target_user_id)

        for file_name, file_type in files_for_user:
            file_status = get_file_status(target_user_id, file_name)
            if file_status['status'] != FILE_STATUS_APPROVED:
                logger.info(f"Skipping '{file_name}' for user {target_user_id} - Status: {file_status['status']}")
                error_files_details.append(f"`{file_name}` (User {target_user_id}) - Not approved ({file_status['status']})")
                skipped_files += 1
                continue
                
            if not is_bot_running(target_user_id, file_name):
                file_path = os.path.join(user_folder, file_name)
                if _materialize_project(target_user_id, file_name):
                    logger.info(f"Admin {admin_user_id} attempting to start '{file_name}' ({file_type}) for user {target_user_id}.")
                    try:
                        if file_type == 'py':
                            threading.Thread(target=run_script, args=(file_path, target_user_id, user_folder, file_name, admin_message_obj_for_script_runner)).start()
                            started_count += 1
                        elif file_type == 'js':
                            threading.Thread(target=run_js_script, args=(file_path, target_user_id, user_folder, file_name, admin_message_obj_for_script_runner)).start()
                            started_count += 1
                        else:
                            logger.warning(f"Unknown file type '{file_type}' for {file_name} (user {target_user_id}). Skipping.")
                            error_files_details.append(f"`{file_name}` (User {target_user_id}) - Unknown type")
                            skipped_files += 1
                        time.sleep(0.7)
                    except Exception as e:
                        logger.error(f"Error queueing start for '{file_name}' (user {target_user_id}): {e}")
                        error_files_details.append(f"`{file_name}` (User {target_user_id}) - Start error")
                        skipped_files += 1
                else:
                    logger.warning(f"Stored file '{file_name}' for user {target_user_id} not found in DB. Skipping.")
                    error_files_details.append(f"`{file_name}` (User {target_user_id}) - File data not in DB")
                    skipped_files += 1

    summary_msg = (f"All Users' Scripts - Processing Complete:\n\n"
                   f"Attempted to start: {started_count} scripts.\n"
                   f"Users processed: {attempted_users}.\n")
    if skipped_files > 0:
        summary_msg += f"Skipped/Error files: {skipped_files}\n"
        if error_files_details:
             summary_msg += "Details (first 5):\n" + "\n".join([f"  - {err}" for err in error_files_details[:5]])
             if len(error_files_details) > 5: summary_msg += "\n  ... and more (check logs)."

    reply_func(summary_msg, parse_mode='Markdown')
    logger.info(f"Run all scripts finished. Admin: {admin_user_id}. Started: {started_count}. Skipped/Errors: {skipped_files}")

@bot.message_handler(commands=['mpx'])
def handle_mpx_command(message):
    user_id = message.from_user.id
    if bot_locked and user_id not in admin_ids:
        bot.reply_to(message, "Bot is currently locked. Try again later.")
        return

    if not message.text or len(message.text.split()) < 2:
        bot.reply_to(message, "Please provide a query after /mpx command.\nExample: `/mpx What is AI?`", parse_mode='Markdown')
        return

    query = message.text.split(' ', 1)[1]
    bot.send_chat_action(message.chat.id, 'typing')

    try:
        headers = {
            "Authorization": f"Bearer {A4F_API_KEY}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": A4F_MODEL,
            "messages": [{"role": "user", "content": query}],
            "temperature": 0.7
        }

        response = requests.post(A4F_API_URL, headers=headers, json=payload)
        response.raise_for_status()

        result = response.json()
        answer = result.get('choices', [{}])[0].get('message', {}).get('content', 'No response from API')

        if len(answer) > 4000:
            for x in range(0, len(answer), 4000):
                bot.reply_to(message, answer[x:x+4000], parse_mode='Markdown')
        else:
            bot.reply_to(message, answer, parse_mode='Markdown')

    except requests.exceptions.RequestException as e:
        logger.error(f"API request failed: {e}")
        bot.reply_to(message, "Error connecting to the API. Please try again later.")
    except Exception as e:
        logger.error(f"Error in /mpx command: {e}")
        bot.reply_to(message, "An error occurred while processing your request.")

@bot.message_handler(commands=['pending'])
def handle_pending_command(message):
    _logic_view_pending(message)

@bot.message_handler(commands=['start', 'help'])
def command_send_welcome(message): _logic_send_welcome(message)

@bot.message_handler(commands=['status'])
def command_show_status(message): _logic_statistics(message)

@bot.message_handler(commands=['uptime'])
def command_uptime(message):
    _logic_uptime(message)

@bot.message_handler(commands=['ping'])
def ping(message):
    start_ping_time = time.time()
    msg = bot.reply_to(message, "Pong!")
    latency = round((time.time() - start_ping_time) * 1000, 2)
    uptime_str = get_uptime()
    bot.edit_message_text(f"Pong!\nLatency: {latency} ms\nUptime: {uptime_str}",
                          message.chat.id, msg.message_id)

@bot.message_handler(commands=['plans'])
def command_plans(message):
    try:
        bot.send_message(message.chat.id, get_plans_text(message.from_user.id), parse_mode='HTML')
    except Exception as e:
        logger.error(f"Error sending plans: {e}", exc_info=True)
        bot.reply_to(message, get_plans_text(message.from_user.id))

def _logic_plans(message):
    command_plans(message)

# ====================== WEB REGISTRATION (from bot) ======================
def _registration_plan_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = [
        types.InlineKeyboardButton(f"🥉 {PLANS[k]['name']} - {PLANS[k]['limit']} bots",
                                   callback_data=f"regplan_{k}")
        for k in ['free', 'starter', 'pro', 'business']
    ]
    markup.add(*buttons)
    markup.add(types.InlineKeyboardButton("❌ Cancel", callback_data='regplan_cancel'))
    return markup

@bot.message_handler(commands=['register'])
def command_register(message):
    _logic_register(message)

def _logic_register(message):
    user_id = message.from_user.id
    existing = db.web_users.find_one({'telegram_id': user_id})
    if existing:
        bot.reply_to(message,
                     f"You are already registered as `{existing['username']}`.\n"
                     f"Log in at the website dashboard with that username and password.\n"
                     f"Your chosen plan: `{existing['plan']}`",
                     parse_mode='Markdown')
        return
    if user_id in pending_regs:
        bot.reply_to(message, "Registration already in progress. Send `/cancel` to abort or continue.", parse_mode='Markdown')
        return
    msg = bot.reply_to(message,
                       "📝 <b>Web dashboard registration</b>\n\n"
                       "This lets you log in on the website to manage bots, logs and env.\n\n"
                       "Send your desired <b>username</b> (3-24 chars: letters, digits, underscore).\n"
                       "/cancel to abort.",
                       parse_mode='HTML')
    bot.register_next_step_handler(msg, process_reg_username)

def process_reg_username(message):
    user_id = message.from_user.id
    if message.text and message.text.lower() == '/cancel':
        pending_regs.pop(user_id, None)
        bot.reply_to(message, "Registration cancelled.")
        return
    username = (message.text or '').strip()
    if not re.match(r'^[A-Za-z0-9_]{3,24}$', username):
        bot.reply_to(message, "Invalid username. Use 3-24 chars: letters, digits, underscore. Try again or /cancel.")
        msg = bot.send_message(message.chat.id, "Send your desired username, or /cancel.")
        bot.register_next_step_handler(msg, process_reg_username)
        return
    if WEB_USERS.get(username) or db.web_users.find_one({'username': username}):
        bot.reply_to(message, "That username is taken. Pick another, or /cancel.")
        msg = bot.send_message(message.chat.id, "Send your desired username, or /cancel.")
        bot.register_next_step_handler(msg, process_reg_username)
        return
    pending_regs[user_id] = {'username': username}
    bot.reply_to(message, f"Username `{username}` is available. Now send a <b>password</b> (min 6 chars).",
                 parse_mode='Markdown')
    msg = bot.send_message(message.chat.id, "Send your password, or /cancel.")
    bot.register_next_step_handler(msg, process_reg_password)

def process_reg_password(message):
    user_id = message.from_user.id
    if message.text and message.text.lower() == '/cancel':
        pending_regs.pop(user_id, None)
        bot.reply_to(message, "Registration cancelled.")
        return
    password = message.text or ''
    if len(password) < 6:
        bot.reply_to(message, "Password must be at least 6 characters. Try again or /cancel.")
        msg = bot.send_message(message.chat.id, "Send your password, or /cancel.")
        bot.register_next_step_handler(msg, process_reg_password)
        return
    if user_id not in pending_regs:
        bot.reply_to(message, "No registration in progress. Send /register to start.")
        return
    pending_regs[user_id]['password'] = password
    bot.reply_to(message, "Almost done. <b>Choose your hosting plan:</b>", parse_mode='HTML',
                 reply_markup=_registration_plan_keyboard())

@bot.callback_query_handler(func=lambda call: call.data.startswith('regplan_'))
def regplan_callback(call):
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    plan = call.data.split('_', 1)[1]
    if plan == 'cancel':
        pending_regs.pop(user_id, None)
        try: bot.edit_message_text("Registration cancelled.", call.message.chat.id, call.message.message_id)
        except Exception: pass
        return
    reg = pending_regs.pop(user_id, None)
    if not reg:
        try: bot.edit_message_text("Registration expired. Send /register to start again.", call.message.chat.id, call.message.message_id)
        except Exception: pass
        return
    ok, message = register_web_user(reg['username'], reg['password'], user_id, plan)
    if ok:
        text = (f"✅ <b>Registered!</b>\n\nUsername: <code>{reg['username']}</code>\n"
                f"Plan: <b>{PLANS[plan]['name']}</b> ({PLANS[plan]['limit']} bots)\n\n"
                f"Log in on the website dashboard to manage your bots.")
        if plan != 'free':
            text += "\n\nNote: your paid plan will be active once an admin activates the subscription."
    else:
        text = f"❌ Registration failed: {message}"
    try: bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML')
    except Exception:
        bot.send_message(call.message.chat.id, text, parse_mode='HTML')

BUTTON_TEXT_TO_LOGIC = {
    "📢 Updates Channel": _logic_updates_channel,
    "🌐 Web Dashboard": _logic_web_dashboard,
    "📤 Upload File": _logic_upload_file,
    "📂 Check Files": _logic_check_files,
    "⚡ Bot Speed": _logic_bot_speed,
    "📞 Contact Owner": _logic_contact_owner,
    "📊 Statistics": _logic_statistics,
    "⏱ Uptime": _logic_uptime,
    "💠 Plans": _logic_plans,
    "📝 Register": _logic_register,
    "💳 Subscriptions": _logic_subscriptions_panel,
    "📢 Broadcast": _logic_broadcast_init,
    "🔒 Lock Bot": _logic_toggle_lock_bot,
    "🟢 Running All Code": _logic_run_all_scripts,
    "👑 Admin Panel": _logic_admin_panel,
    "🤖 MPX AI": lambda m: handle_mpx_command(m)
}

@bot.message_handler(func=lambda message: message.text in BUTTON_TEXT_TO_LOGIC)
def handle_button_text(message):
    logic_func = BUTTON_TEXT_TO_LOGIC.get(message.text)
    if logic_func: logic_func(message)
    else: logger.warning(f"Button text '{message.text}' matched but no logic func.")

@bot.message_handler(commands=['updateschannel', 'web', 'dashboard'])
def command_updates_channel(message):
    if message.text.startswith('/web') or message.text.startswith('/dashboard'):
        _logic_web_dashboard(message)
    else:
        _logic_updates_channel(message)
@bot.message_handler(commands=['uploadfile'])
def command_upload_file(message): _logic_upload_file(message)
@bot.message_handler(commands=['checkfiles'])
def command_check_files(message): _logic_check_files(message)
@bot.message_handler(commands=['botspeed'])
def command_bot_speed(message): _logic_bot_speed(message)
@bot.message_handler(commands=['contactowner'])
def command_contact_owner(message): _logic_contact_owner(message)
@bot.message_handler(commands=['subscriptions'])
def command_subscriptions(message): _logic_subscriptions_panel(message)
@bot.message_handler(commands=['statistics'])
def command_statistics(message): _logic_statistics(message)
@bot.message_handler(commands=['broadcast'])
def command_broadcast(message): _logic_broadcast_init(message)
@bot.message_handler(commands=['lockbot', 'maintenance'])
def command_lock_bot(message): _logic_toggle_lock_bot(message)
@bot.message_handler(commands=['adminpanel'])
def command_admin_panel(message): _logic_admin_panel(message)
@bot.message_handler(commands=['runningallcode'])
def command_run_all_code(message): _logic_run_all_scripts(message)

@bot.message_handler(content_types=['document'])
def handle_file_upload_doc(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    doc = message.document
    logger.info(f"Doc from {user_id}: {doc.file_name} ({doc.mime_type}), Size: {doc.file_size}")

    if bot_locked and user_id not in admin_ids:
        bot.reply_to(message, "Bot locked, cannot accept files.")
        return

    file_limit = get_user_file_limit(user_id)
    current_files = get_user_file_count(user_id)
    if current_files >= file_limit:
        limit_str = str(file_limit) if file_limit != float('inf') else "Unlimited"
        bot.reply_to(message, f"File limit ({current_files}/{limit_str}) reached. Delete files via /checkfiles.")
        return

    file_name = doc.file_name
    if not file_name: bot.reply_to(message, "No file name. Ensure file has a name."); return
    file_ext = os.path.splitext(file_name)[1].lower()
    if file_ext not in ['.py', '.js', '.zip']:
        bot.reply_to(message, "Unsupported type! Only `.py`, `.js`, `.zip` allowed.")
        return
    max_file_size = 20 * 1024 * 1024
    if doc.file_size > max_file_size:
        bot.reply_to(message, f"File too large (Max: {max_file_size // 1024 // 1024} MB)."); return

    try:
        try:
            bot.forward_message(OWNER_ID, chat_id, message.message_id)
            bot.send_message(OWNER_ID, f"File '{file_name}' from {message.from_user.first_name} (`{user_id}`)", parse_mode='Markdown')
        except Exception as e: logger.error(f"Failed to forward uploaded file to OWNER_ID {OWNER_ID}: {e}")

        download_wait_msg = bot.reply_to(message, f"Downloading `{file_name}`...")
        file_info_tg_doc = bot.get_file(doc.file_id)
        downloaded_file_content = bot.download_file(file_info_tg_doc.file_path)
        bot.edit_message_text(f"Downloaded `{file_name}`. Processing...", chat_id, download_wait_msg.message_id)
        logger.info(f"Downloaded {file_name} for user {user_id}")
        user_folder = get_user_folder(user_id)

        if file_ext == '.zip':
            handle_zip_file(downloaded_file_content, file_name, message)
        else:
            file_type = 'py' if file_ext == '.py' else 'js'
            if not _store_single_file_data(user_id, file_name, file_type, downloaded_file_content):
                logger.error(f"Failed to store single {file_type} {file_name} for {user_id} in DB")
                bot.reply_to(message, "Failed to store the file in the database. Please try again.")
                return
            logger.info(f"Stored single {file_type} {file_name} for user {user_id} in DB (not on disk until started)")
            file_path = os.path.join(user_folder, file_name)
            if file_type == 'js': handle_js_file(file_path, user_id, user_folder, file_name, message)
            else: handle_py_file(file_path, user_id, user_folder, file_name, message)
    except telebot.apihelper.ApiTelegramException as e:
         logger.error(f"Telegram API Error handling file for {user_id}: {e}", exc_info=True)
         if "file is too big" in str(e).lower():
              bot.reply_to(message, f"Telegram API Error: File too large to download (~20MB limit).")
         else: bot.reply_to(message, f"Telegram API Error: {str(e)}. Try later.")
    except Exception as e:
        logger.error(f"General error handling file for {user_id}: {e}", exc_info=True)
        bot.reply_to(message, f"Unexpected error: {str(e)}")

# Approval callback handlers
def handle_approve_callback(call):
    try:
        admin_id = call.from_user.id
        if admin_id not in admin_ids:
            bot.answer_callback_query(call.id, "Admin permissions required.", show_alert=True)
            return
        
        _, user_id_str, file_name = call.data.split('_', 2)
        user_id = int(user_id_str)
        
        if update_file_status(user_id, file_name, FILE_STATUS_APPROVED, admin_id):
            try:
                bot.send_message(user_id,
                               f"✅ **File Approved!**\n\n"
                               f"📁 File: `{file_name}`\n"
                               f"👮‍♂️ Approved by: Admin\n"
                               f"⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                               f"You can now run this file using /checkfiles",
                               parse_mode='Markdown')
            except Exception as e:
                logger.error(f"Failed to notify user {user_id} about approval: {e}")
            
            bot.answer_callback_query(call.id, f"✅ File approved!")
            
            try:
                bot.edit_message_text(f"✅ **APPROVED**\n\nFile: `{file_name}`\nUser: `{user_id}`\nBy: Admin `{admin_id}`",
                                    call.message.chat.id, call.message.message_id,
                                    parse_mode='Markdown')
            except:
                pass
                
            logger.info(f"File approved: {user_id}/{file_name} by {admin_id}")
        else:
            bot.answer_callback_query(call.id, "Error updating file status.", show_alert=True)
            
    except Exception as e:
        logger.error(f"Error in handle_approve_callback: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error processing approval.", show_alert=True)

def handle_reject_callback(call):
    try:
        admin_id = call.from_user.id
        if admin_id not in admin_ids:
            bot.answer_callback_query(call.id, "Admin permissions required.", show_alert=True)
            return
        
        _, user_id_str, file_name = call.data.split('_', 2)
        user_id = int(user_id_str)
        
        if update_file_status(user_id, file_name, FILE_STATUS_REJECTED, admin_id):
            try:
                bot.send_message(user_id,
                               f"❌ **File Rejected!**\n\n"
                               f"📁 File: `{file_name}`\n"
                               f"👮‍♂️ Rejected by: Admin\n"
                               f"⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                               f"Reason: File rejected by admin. Please upload a valid file.",
                               parse_mode='Markdown')
            except Exception as e:
                logger.error(f"Failed to notify user {user_id} about rejection: {e}")
            
            bot.answer_callback_query(call.id, f"❌ File rejected!")
            
            try:
                bot.edit_message_text(f"❌ **REJECTED**\n\nFile: `{file_name}`\nUser: `{user_id}`\nBy: Admin `{admin_id}`",
                                    call.message.chat.id, call.message.message_id,
                                    parse_mode='Markdown')
            except:
                pass
                
            logger.info(f"File rejected: {user_id}/{file_name} by {admin_id}")
        else:
            bot.answer_callback_query(call.id, "Error updating file status.", show_alert=True)
            
    except Exception as e:
        logger.error(f"Error in handle_reject_callback: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error processing rejection.", show_alert=True)

def handle_review_callback(call):
    try:
        admin_id = call.from_user.id
        if admin_id not in admin_ids:
            bot.answer_callback_query(call.id, "Admin permissions required.", show_alert=True)
            return
        
        _, user_id_str, file_name = call.data.split('_', 2)
        user_id = int(user_id_str)
        
        file_status = get_file_status(user_id, file_name)
        file_type = file_status.get('file_type', 'unknown')
        
        review_text = (
            f"📋 **File Review**\n\n"
            f"👤 **User ID:** `{user_id}`\n"
            f"📁 **File:** `{file_name}`\n"
            f"📊 **Type:** {file_type}\n"
            f"📋 **Status:** {file_status['status'].upper()}\n\n"
            f"**Choose action:**"
        )
        
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("✅ Approve", callback_data=f'approve_{user_id}_{file_name}'),
            types.InlineKeyboardButton("❌ Reject", callback_data=f'reject_{user_id}_{file_name}')
        )
        markup.add(types.InlineKeyboardButton("🔙 Back to Pending", callback_data='view_pending'))
        
        bot.answer_callback_query(call.id)
        bot.edit_message_text(review_text, call.message.chat.id, call.message.message_id,
                            reply_markup=markup, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error in handle_review_callback: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error loading file review.", show_alert=True)

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    user_id = call.from_user.id
    data = call.data
    logger.info(f"Callback: User={user_id}, Data='{data}'")

    if bot_locked and user_id not in admin_ids and data not in ['back_to_main', 'speed', 'stats', 'mpx_ai', 'uptime']:
        bot.answer_callback_query(call.id, "Bot locked by admin.", show_alert=True)
        return
    try:
        if data == 'upload':
            upload_callback(call)
        elif data == 'check_files':
            check_files_callback(call)
        elif data.startswith('file_'):
            file_control_callback(call)
        elif data.startswith('start_'):
            start_bot_callback(call)
        elif data.startswith('stop_'):
            stop_bot_callback(call)
        elif data.startswith('restart_'):
            restart_bot_callback(call)
        elif data.startswith('delete_'):
            delete_bot_callback(call)
        elif data.startswith('logs_'):
            logs_bot_callback(call)
        elif data.startswith('status_'):
            _, script_owner_id_str, file_name = data.split('_', 2)
            script_owner_id = int(script_owner_id_str)
            file_status = get_file_status(script_owner_id, file_name)
            
            status_text = "✅ **APPROVED**" if file_status['status'] == FILE_STATUS_APPROVED else \
                         "⏳ **PENDING**" if file_status['status'] == FILE_STATUS_PENDING else "❌ **REJECTED**"
            
            response = f"📋 **File Status:** {status_text}\n\n"
            response += f"📁 File: `{file_name}`\n"
            response += f"👤 User: `{script_owner_id}`\n"
            response += f"📊 Type: {file_status.get('file_type', 'unknown')}\n"
            
            if file_status['reviewed_by']:
                response += f"👮‍♂️ Reviewed by: Admin `{file_status['reviewed_by']}`\n"
                if file_status['review_time']:
                    try:
                        review_dt = datetime.fromisoformat(file_status['review_time'])
                        response += f"⏰ Review time: {review_dt.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    except:
                        response += f"⏰ Review time: {file_status['review_time']}\n"
            
            bot.answer_callback_query(call.id, response, show_alert=True)
            
        elif data == 'speed':
            speed_callback(call)
        elif data == 'back_to_main':
            back_to_main_callback(call)
        elif data.startswith('confirm_broadcast_'):
            handle_confirm_broadcast(call)
        elif data == 'cancel_broadcast':
            handle_cancel_broadcast(call)
        elif data == 'subscription':
            admin_required_callback(call, subscription_management_callback)
        elif data == 'stats':
            stats_callback(call)
        elif data == 'lock_bot':
            admin_required_callback(call, lock_bot_callback)
        elif data == 'unlock_bot':
            admin_required_callback(call, unlock_bot_callback)
        elif data == 'run_all_scripts':
            admin_required_callback(call, run_all_scripts_callback)
        elif data == 'broadcast':
            admin_required_callback(call, broadcast_init_callback)
        elif data == 'admin_panel':
            admin_required_callback(call, admin_panel_callback)
        elif data == 'add_admin':
            owner_required_callback(call, add_admin_init_callback)
        elif data == 'remove_admin':
            owner_required_callback(call, remove_admin_init_callback)
        elif data == 'list_admins':
            admin_required_callback(call, list_admins_callback)
        elif data == 'add_subscription':
            admin_required_callback(call, add_subscription_init_callback)
        elif data == 'remove_subscription':
            admin_required_callback(call, remove_subscription_init_callback)
        elif data == 'check_subscription':
            admin_required_callback(call, check_subscription_init_callback)
        elif data == 'mpx_ai':
            bot.answer_callback_query(call.id)
            bot.send_message(call.message.chat.id, "Please send your query using the /mpx command followed by your question.\nExample: `/mpx What is AI?`", parse_mode='Markdown')
        elif data == 'uptime':
            bot.answer_callback_query(call.id)
            uptime_str = get_uptime()
            bot.send_message(call.message.chat.id, f"Bot Uptime: `{uptime_str}`", parse_mode='Markdown')
        elif data.startswith('approve_'):
            handle_approve_callback(call)
        elif data.startswith('reject_'):
            handle_reject_callback(call)
        elif data.startswith('review_'):
            handle_review_callback(call)
        elif data == 'view_pending':
            if user_id in admin_ids:
                _logic_view_pending(call.message)
                bot.answer_callback_query(call.id)
            else:
                bot.answer_callback_query(call.id, "Admin permissions required.", show_alert=True)
            
        else:
            bot.answer_callback_query(call.id, "Unknown action.")
            logger.warning(f"Unhandled callback data: {data} from user {user_id}")
    except Exception as e:
        logger.error(f"Error handling callback '{data}' for {user_id}: {e}", exc_info=True)
        try:
            bot.answer_callback_query(call.id, "Error processing request.", show_alert=True)
        except Exception as e_ans:
            logger.error(f"Failed to answer callback after error: {e_ans}")

def admin_required_callback(call, func_to_run):
    if call.from_user.id not in admin_ids:
        bot.answer_callback_query(call.id, "Admin permissions required.", show_alert=True)
        return
    func_to_run(call)

def owner_required_callback(call, func_to_run):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, "Owner permissions required.", show_alert=True)
        return
    func_to_run(call)

def upload_callback(call):
    user_id = call.from_user.id
    file_limit = get_user_file_limit(user_id)
    current_files = get_user_file_count(user_id)
    if current_files >= file_limit:
        limit_str = str(file_limit) if file_limit != float('inf') else "Unlimited"
        bot.answer_callback_query(call.id, f"File limit ({current_files}/{limit_str}) reached.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, 
                    "Send your Python (`.py`), JS (`.js`), or ZIP (`.zip`) file.\n\n"
                    "⚠️ **Note:** All files require admin approval before running.")

def check_files_callback(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    user_files_list = user_files.get(user_id, [])
    if not user_files_list:
        bot.answer_callback_query(call.id, "No files uploaded.", show_alert=True)
        try:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("Back to Main", callback_data='back_to_main'))
            bot.edit_message_text("Your files:\n\n(No files uploaded)", chat_id, call.message.message_id, reply_markup=markup)
        except Exception as e: logger.error(f"Error editing msg for empty file list: {e}")
        return
    bot.answer_callback_query(call.id)
    
    response = "📁 **Your Files:**\n\n"
    markup = types.InlineKeyboardMarkup(row_width=1)
    
    for file_name, file_type in sorted(user_files_list):
        is_running = is_bot_running(user_id, file_name)
        file_status = get_file_status(user_id, file_name)
        
        status_icon = "🟢" if is_running else "⚪"
        approval_icon = "✅" if file_status['status'] == FILE_STATUS_APPROVED else \
                       "⏳" if file_status['status'] == FILE_STATUS_PENDING else "❌"
        
        btn_text = f"{approval_icon} {file_name} ({file_type}) - {status_icon}"
        markup.add(types.InlineKeyboardButton(btn_text, callback_data=f'file_{user_id}_{file_name}'))
        
        approval_text = "Approved" if file_status['status'] == FILE_STATUS_APPROVED else \
                       "Pending" if file_status['status'] == FILE_STATUS_PENDING else "Rejected"
        response += f"{approval_icon} `{file_name}` - {approval_text}\n"
    
    markup.add(types.InlineKeyboardButton("Back to Main", callback_data='back_to_main'))
    try:
        bot.edit_message_text(response, chat_id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')
    except telebot.apihelper.ApiTelegramException as e:
         if "message is not modified" in str(e): logger.warning("Msg not modified (files).")
         else: logger.error(f"Error editing msg for file list: {e}")
    except Exception as e: logger.error(f"Unexpected error editing msg for file list: {e}", exc_info=True)

def file_control_callback(call):
    try:
        _, script_owner_id_str, file_name = call.data.split('_', 2)
        script_owner_id = int(script_owner_id_str)
        requesting_user_id = call.from_user.id

        if not (requesting_user_id == script_owner_id or requesting_user_id in admin_ids):
            logger.warning(f"User {requesting_user_id} tried to access file '{file_name}' of user {script_owner_id} without permission.")
            bot.answer_callback_query(call.id, "You can only manage your own files.", show_alert=True)
            check_files_callback(call)
            return

        user_files_list = user_files.get(script_owner_id, [])
        if not any(f[0] == file_name for f in user_files_list):
            bot.answer_callback_query(call.id, "File not found.", show_alert=True)
            check_files_callback(call)
            return

        bot.answer_callback_query(call.id)
        is_running = is_bot_running(script_owner_id, file_name)
        file_status = get_file_status(script_owner_id, file_name)
        status_text = 'Running' if is_running else 'Stopped'
        file_type = next((f[1] for f in user_files_list if f[0] == file_name), '?')
        
        approval_status = f"✅ Approved" if file_status['status'] == FILE_STATUS_APPROVED else \
                         f"⏳ Pending" if file_status['status'] == FILE_STATUS_PENDING else f"❌ Rejected"
        
        try:
            bot.edit_message_text(
                f"📋 **File Controls**\n\n"
                f"📁 File: `{file_name}`\n"
                f"📊 Type: {file_type}\n"
                f"👤 User: `{script_owner_id}`\n"
                f"🔄 Status: {status_text}\n"
                f"📝 Approval: {approval_status}\n",
                call.message.chat.id, call.message.message_id,
                reply_markup=create_control_buttons(script_owner_id, file_name, is_running),
                parse_mode='Markdown'
            )
        except telebot.apihelper.ApiTelegramException as e:
             if "message is not modified" in str(e): logger.warning(f"Msg not modified (controls for {file_name})")
             else: raise
    except (ValueError, IndexError) as ve:
        logger.error(f"Error parsing file control callback: {ve}. Data: '{call.data}'")
        bot.answer_callback_query(call.id, "Error: Invalid action data.", show_alert=True)
    except Exception as e:
        logger.error(f"Error in file_control_callback for data '{call.data}': {e}", exc_info=True)
        bot.answer_callback_query(call.id, "An error occurred.", show_alert=True)

def start_bot_callback(call):
    try:
        _, script_owner_id_str, file_name = call.data.split('_', 2)
        script_owner_id = int(script_owner_id_str)
        requesting_user_id = call.from_user.id
        chat_id_for_reply = call.message.chat.id

        logger.info(f"Start request: Requester={requesting_user_id}, Owner={script_owner_id}, File='{file_name}'")

        if not (requesting_user_id == script_owner_id or requesting_user_id in admin_ids):
            bot.answer_callback_query(call.id, "Permission denied to start this script.", show_alert=True); return

        user_files_list = user_files.get(script_owner_id, [])
        file_info = next((f for f in user_files_list if f[0] == file_name), None)
        if not file_info:
            bot.answer_callback_query(call.id, "File not found.", show_alert=True); check_files_callback(call); return

        file_type = file_info[1]
        user_folder = get_user_folder(script_owner_id)
        file_path = os.path.join(user_folder, file_name)

        # Files live in MongoDB and are only written to the VPS disk here,
        # at Start time.
        if not _materialize_project(script_owner_id, file_name):
            bot.answer_callback_query(call.id, f"Error: Stored file `{file_name}` not found in the database! Re-upload.", show_alert=True)
            check_files_callback(call); return
        
        file_status = get_file_status(script_owner_id, file_name)
        if file_status['status'] != FILE_STATUS_APPROVED:
            bot.answer_callback_query(call.id, 
                                    f"File not approved yet! Status: {file_status['status']}", 
                                    show_alert=True)
            return

        if is_bot_running(script_owner_id, file_name):
            bot.answer_callback_query(call.id, f"Script '{file_name}' already running.", show_alert=True)
            try: bot.edit_message_reply_markup(chat_id_for_reply, call.message.message_id, reply_markup=create_control_buttons(script_owner_id, file_name, True))
            except Exception as e: logger.error(f"Error updating buttons (already running): {e}")
            return

        bot.answer_callback_query(call.id, f"Attempting to start {file_name} for user {script_owner_id}...")

        if file_type == 'py':
            threading.Thread(target=run_script, args=(file_path, script_owner_id, user_folder, file_name, call.message)).start()
        elif file_type == 'js':
            threading.Thread(target=run_js_script, args=(file_path, script_owner_id, user_folder, file_name, call.message)).start()
        else:
             bot.send_message(chat_id_for_reply, f"Error: Unknown file type '{file_type}' for '{file_name}'."); return

        time.sleep(1.5)
        is_now_running = is_bot_running(script_owner_id, file_name)
        status_text = 'Running' if is_now_running else 'Starting (or failed, check logs/replies)'
        try:
            bot.edit_message_text(
                f"Controls for: `{file_name}` ({file_type}) of User `{script_owner_id}`\nStatus: {status_text}",
                chat_id_for_reply, call.message.message_id,
                reply_markup=create_control_buttons(script_owner_id, file_name, is_now_running), parse_mode='Markdown'
            )
        except telebot.apihelper.ApiTelegramException as e:
             if "message is not modified" in str(e): logger.warning(f"Msg not modified after starting {file_name}")
             else: raise
    except (ValueError, IndexError) as e:
        logger.error(f"Error parsing start callback '{call.data}': {e}")
        bot.answer_callback_query(call.id, "Error: Invalid start command.", show_alert=True)
    except Exception as e:
        logger.error(f"Error in start_bot_callback for '{call.data}': {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error starting script.", show_alert=True)
        try:
            _, script_owner_id_err_str, file_name_err = call.data.split('_', 2)
            script_owner_id_err = int(script_owner_id_err_str)
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=create_control_buttons(script_owner_id_err, file_name_err, False))
        except Exception as e_btn: logger.error(f"Failed to update buttons after start error: {e_btn}")

def stop_bot_callback(call):
    try:
        _, script_owner_id_str, file_name = call.data.split('_', 2)
        script_owner_id = int(script_owner_id_str)
        requesting_user_id = call.from_user.id
        chat_id_for_reply = call.message.chat.id

        logger.info(f"Stop request: Requester={requesting_user_id}, Owner={script_owner_id}, File='{file_name}'")
        if not (requesting_user_id == script_owner_id or requesting_user_id in admin_ids):
            bot.answer_callback_query(call.id, "Permission denied.", show_alert=True); return

        user_files_list = user_files.get(script_owner_id, [])
        file_info = next((f for f in user_files_list if f[0] == file_name), None)
        if not file_info:
            bot.answer_callback_query(call.id, "File not found.", show_alert=True); check_files_callback(call); return

        file_type = file_info[1]
        script_key = f"{script_owner_id}_{file_name}"

        if not is_bot_running(script_owner_id, file_name):
            bot.answer_callback_query(call.id, f"Script '{file_name}' already stopped.", show_alert=True)
            try:
                 bot.edit_message_text(
                     f"Controls for: `{file_name}` ({file_type}) of User `{script_owner_id}`\nStatus: Stopped",
                     chat_id_for_reply, call.message.message_id,
                     reply_markup=create_control_buttons(script_owner_id, file_name, False), parse_mode='Markdown')
            except Exception as e: logger.error(f"Error updating buttons (already stopped): {e}")
            # Remove any leftover VPS files; the copy remains in MongoDB.
            try: _destroy_project(script_owner_id, file_name)
            except Exception as e: logger.error(f"Failed to destroy leftover files (already stopped): {e}")
            return

        bot.answer_callback_query(call.id, f"Stopping {file_name} for user {script_owner_id}...")
        process_info = bot_scripts.get(script_key)
        if process_info:
            kill_process_tree(process_info)
            if script_key in bot_scripts: del bot_scripts[script_key]; logger.info(f"Removed {script_key} from running after stop.")
        else: logger.warning(f"Script {script_key} running by psutil but not in bot_scripts dict.")

        # Destroy the bot's files from the VPS disk; the copy stays in MongoDB.
        try: _destroy_project(script_owner_id, file_name)
        except Exception as e: logger.error(f"Failed to destroy files on stop for {script_key}: {e}")

        try:
            bot.edit_message_text(
                f"Controls for: `{file_name}` ({file_type}) of User `{script_owner_id}`\nStatus: Stopped",
                chat_id_for_reply, call.message.message_id,
                reply_markup=create_control_buttons(script_owner_id, file_name, False), parse_mode='Markdown'
            )
        except telebot.apihelper.ApiTelegramException as e:
             if "message is not modified" in str(e): logger.warning(f"Msg not modified after stopping {file_name}")
             else: raise
    except (ValueError, IndexError) as e:
        logger.error(f"Error parsing stop callback '{call.data}': {e}")
        bot.answer_callback_query(call.id, "Error: Invalid stop command.", show_alert=True)
    except Exception as e:
        logger.error(f"Error in stop_bot_callback for '{call.data}': {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error stopping script.", show_alert=True)

def restart_bot_callback(call):
    try:
        _, script_owner_id_str, file_name = call.data.split('_', 2)
        script_owner_id = int(script_owner_id_str)
        requesting_user_id = call.from_user.id
        chat_id_for_reply = call.message.chat.id

        logger.info(f"Restart: Requester={requesting_user_id}, Owner={script_owner_id}, File='{file_name}'")
        if not (requesting_user_id == script_owner_id or requesting_user_id in admin_ids):
            bot.answer_callback_query(call.id, "Permission denied.", show_alert=True); return

        user_files_list = user_files.get(script_owner_id, [])
        file_info = next((f for f in user_files_list if f[0] == file_name), None)
        if not file_info:
            bot.answer_callback_query(call.id, "File not found.", show_alert=True); check_files_callback(call); return

        file_type = file_info[1]; user_folder = get_user_folder(script_owner_id)
        file_path = os.path.join(user_folder, file_name); script_key = f"{script_owner_id}_{file_name}"

        # Re-materialize from MongoDB (a previous Stop may have removed the
        # files from the VPS disk).
        if not _materialize_project(script_owner_id, file_name):
            bot.answer_callback_query(call.id, f"Error: Stored file `{file_name}` not found in the database! Re-upload.", show_alert=True)
            check_files_callback(call); return
        
        file_status = get_file_status(script_owner_id, file_name)
        if file_status['status'] != FILE_STATUS_APPROVED:
            bot.answer_callback_query(call.id, 
                                    f"File not approved yet! Status: {file_status['status']}", 
                                    show_alert=True)
            return

        bot.answer_callback_query(call.id, f"Restarting {file_name} for user {script_owner_id}...")
        if is_bot_running(script_owner_id, file_name):
            logger.info(f"Restart: Stopping existing {script_key}...")
            process_info = bot_scripts.get(script_key)
            if process_info: kill_process_tree(process_info)
            if script_key in bot_scripts: del bot_scripts[script_key]
            time.sleep(1.5)

        logger.info(f"Restart: Starting script {script_key}...")
        if file_type == 'py':
            threading.Thread(target=run_script, args=(file_path, script_owner_id, user_folder, file_name, call.message)).start()
        elif file_type == 'js':
            threading.Thread(target=run_js_script, args=(file_path, script_owner_id, user_folder, file_name, call.message)).start()
        else:
             bot.send_message(chat_id_for_reply, f"Unknown type '{file_type}' for '{file_name}'."); return

        time.sleep(1.5)
        is_now_running = is_bot_running(script_owner_id, file_name)
        status_text = 'Running' if is_now_running else 'Starting (or failed)'
        try:
            bot.edit_message_text(
                f"Controls for: `{file_name}` ({file_type}) of User `{script_owner_id}`\nStatus: {status_text}",
                chat_id_for_reply, call.message.message_id,
                reply_markup=create_control_buttons(script_owner_id, file_name, is_now_running), parse_mode='Markdown'
            )
        except telebot.apihelper.ApiTelegramException as e:
             if "message is not modified" in str(e): logger.warning(f"Msg not modified (restart {file_name})")
             else: raise
    except (ValueError, IndexError) as e:
        logger.error(f"Error parsing restart callback '{call.data}': {e}")
        bot.answer_callback_query(call.id, "Error: Invalid restart command.", show_alert=True)
    except Exception as e:
        logger.error(f"Error in restart_bot_callback for '{call.data}': {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error restarting.", show_alert=True)
        try:
            _, script_owner_id_err_str, file_name_err = call.data.split('_', 2)
            script_owner_id_err = int(script_owner_id_err_str)
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=create_control_buttons(script_owner_id_err, file_name_err, False))
        except Exception as e_btn: logger.error(f"Failed to update buttons after restart error: {e_btn}")

def _delete_user_file(user_id, file_name):
    """Stop (if running) and delete a user's file + log + DB records."""
    user_files_list = user_files.get(user_id, [])
    if not any(f[0] == file_name for f in user_files_list):
        return {'ok': False, 'error': 'File not found in your account'}
    script_key = f"{user_id}_{file_name}"
    if is_bot_running(user_id, file_name):
        logger.info(f"Delete: Stopping {script_key}...")
        process_info = bot_scripts.get(script_key)
        if process_info: kill_process_tree(process_info)
        if script_key in bot_scripts: del bot_scripts[script_key]
        time.sleep(0.5)
    user_folder = get_user_folder(user_id)
    file_path = os.path.join(user_folder, file_name)
    log_path = os.path.join(user_folder, f"{os.path.splitext(file_name)[0]}.log")
    deleted_disk = []
    if os.path.exists(file_path):
        try: os.remove(file_path); deleted_disk.append(file_name); logger.info(f"Deleted file: {file_path}")
        except OSError as e: logger.error(f"Error deleting {file_path}: {e}")
    if os.path.exists(log_path):
        try: os.remove(log_path); deleted_disk.append(os.path.basename(log_path)); logger.info(f"Deleted log: {log_path}")
        except OSError as e: logger.error(f"Error deleting log {log_path}: {e}")
    remove_user_file_db(user_id, file_name)
    try:
        db.file_approvals.delete_one({'user_id': user_id, 'file_name': file_name})
        logger.info(f"Removed file approval record: {user_id}/{file_name}")
    except Exception as e:
        logger.error(f"Error removing file approval: {e}")
    try:
        db.user_file_data.delete_one({'user_id': user_id, 'file_name': file_name})
        logger.info(f"Removed file data record: {user_id}/{file_name}")
    except Exception as e:
        logger.error(f"Error removing file data: {e}")
    # Remove any materialized copies left on the VPS disk.
    try: _destroy_project(user_id, file_name)
    except Exception as e: logger.error(f"Error destroying project files on delete: {e}")
    return {'ok': True, 'message': f"'{file_name}' deleted.", 'deleted': deleted_disk}

def _clear_all_files(user_id):
    """Stop all of a user's bots and delete every uploaded file, log, folder
    and DB record (user_files, file_approvals, user_env)."""
    for key, info in list(bot_scripts.items()):
        if info.get('script_owner_id') == user_id:
            logger.info(f"Clear-all: Stopping {key}")
            try: kill_process_tree(info)
            except Exception as e: logger.error(f"Clear-all kill error for {key}: {e}")
            bot_scripts.pop(key, None)
    user_folder = get_user_folder(user_id)
    if os.path.exists(user_folder):
        try:
            shutil.rmtree(user_folder, ignore_errors=True)
            logger.info(f"Clear-all: deleted folder {user_folder}")
        except Exception as e:
            logger.error(f"Clear-all folder delete error: {e}")
    for coll in ('user_files', 'user_file_data', 'file_approvals', 'user_env'):
        try:
            deleted = db[coll].delete_many({'user_id': user_id}).deleted_count
            logger.info(f"Clear-all: removed {deleted} {coll} record(s) for {user_id}")
        except Exception as e:
            logger.error(f"Clear-all DB error in {coll} for {user_id}: {e}")
    if user_id in user_files: del user_files[user_id]
    return {'ok': True}

def delete_bot_callback(call):
    try:
        _, script_owner_id_str, file_name = call.data.split('_', 2)
        script_owner_id = int(script_owner_id_str)
        requesting_user_id = call.from_user.id
        chat_id_for_reply = call.message.chat.id

        logger.info(f"Delete: Requester={requesting_user_id}, Owner={script_owner_id}, File='{file_name}'")
        if not (requesting_user_id == script_owner_id or requesting_user_id in admin_ids):
            bot.answer_callback_query(call.id, "Permission denied.", show_alert=True); return

        if not any(f[0] == file_name for f in user_files.get(script_owner_id, [])):
            bot.answer_callback_query(call.id, "File not found.", show_alert=True); check_files_callback(call); return

        bot.answer_callback_query(call.id, f"Deleting {file_name} for user {script_owner_id}...")
        result = _delete_user_file(script_owner_id, file_name)

        deleted_str = ", ".join(f"`{f}`" for f in result.get('deleted', [])) if result.get('deleted') else "associated files"
        try:
            bot.edit_message_text(
                f"Record `{file_name}` (User `{script_owner_id}`) and {deleted_str} deleted!",
                chat_id_for_reply, call.message.message_id, reply_markup=None, parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Error editing msg after delete: {e}")
            bot.send_message(chat_id_for_reply, f"Record `{file_name}` deleted.", parse_mode='Markdown')
    except (ValueError, IndexError) as e:
        logger.error(f"Error parsing delete callback '{call.data}': {e}")
        bot.answer_callback_query(call.id, "Error: Invalid delete command.", show_alert=True)
    except Exception as e:
        logger.error(f"Error in delete_bot_callback for '{call.data}': {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error deleting.", show_alert=True)

def logs_bot_callback(call):
    try:
        _, script_owner_id_str, file_name = call.data.split('_', 2)
        script_owner_id = int(script_owner_id_str)
        requesting_user_id = call.from_user.id
        chat_id_for_reply = call.message.chat.id

        logger.info(f"Logs: Requester={requesting_user_id}, Owner={script_owner_id}, File='{file_name}'")
        if not (requesting_user_id == script_owner_id or requesting_user_id in admin_ids):
            bot.answer_callback_query(call.id, "Permission denied.", show_alert=True); return

        user_files_list = user_files.get(script_owner_id, [])
        if not any(f[0] == file_name for f in user_files_list):
            bot.answer_callback_query(call.id, "File not found.", show_alert=True); check_files_callback(call); return

        user_folder = get_user_folder(script_owner_id)
        log_path = os.path.join(user_folder, f"{os.path.splitext(file_name)[0]}.log")
        if not os.path.exists(log_path):
            bot.answer_callback_query(call.id, f"No logs for '{file_name}'.", show_alert=True); return

        bot.answer_callback_query(call.id)
        try:
            log_content = ""; file_size = os.path.getsize(log_path)
            max_log_kb = 100; max_tg_msg = 4096
            if file_size == 0: log_content = "(Log empty)"
            elif file_size > max_log_kb * 1024:
                 with open(log_path, 'rb') as f: f.seek(-max_log_kb * 1024, os.SEEK_END); log_bytes = f.read()
                 log_content = log_bytes.decode('utf-8', errors='ignore')
                 log_content = f"(Last {max_log_kb} KB)\n...\n" + log_content
            else:
                 with open(log_path, 'r', encoding='utf-8', errors='ignore') as f: log_content = f.read()

            if len(log_content) > max_tg_msg:
                log_content = log_content[-max_tg_msg:]
                first_nl = log_content.find('\n')
                if first_nl != -1: log_content = "...\n" + log_content[first_nl+1:]
                else: log_content = "...\n" + log_content
            if not log_content.strip(): log_content = "(No visible content)"

            bot.send_message(chat_id_for_reply, f"Logs for `{file_name}` (User `{script_owner_id}`):\n```\n{log_content}\n```", parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Error reading/sending log {log_path}: {e}", exc_info=True)
            bot.send_message(chat_id_for_reply, f"Logs for {file_name} (User {script_owner_id}):\n\n{log_content}\n\n(Sent as plain text because Markdown parsing failed.)")
    except (ValueError, IndexError) as e:
        logger.error(f"Error parsing logs callback '{call.data}': {e}")
        bot.answer_callback_query(call.id, "Error: Invalid logs command.", show_alert=True)
    except Exception as e:
        logger.error(f"Error in logs_bot_callback for '{call.data}': {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error fetching logs.", show_alert=True)

def speed_callback(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    start_cb_ping_time = time.time()
    try:
        bot.edit_message_text("Testing speed...", chat_id, call.message.message_id)
        bot.send_chat_action(chat_id, 'typing')
        response_time = round((time.time() - start_cb_ping_time) * 1000, 2)
        status = "Unlocked" if not bot_locked else "Locked"
        if user_id == OWNER_ID: user_level = "Owner"
        elif user_id in admin_ids: user_level = "Admin"
        elif user_id in user_subscriptions and user_subscriptions[user_id].get('expiry', datetime.min) > datetime.now(): user_level = "Premium"
        else: user_level = "Free User"
        
        speed_msg = (f"Bot Speed & Status:\n\nAPI Response Time: {response_time} ms\n"
                     f"Bot Status: {status}\n"
                     f"Your Level: {user_level}")
        
        if user_id in admin_ids:
            pending_count = get_pending_files_count()
            speed_msg += f"\n📋 Pending Files: {pending_count}"
            
        bot.answer_callback_query(call.id)
        bot.edit_message_text(speed_msg, chat_id, call.message.message_id, reply_markup=create_main_menu_inline(user_id))
    except Exception as e:
         logger.error(f"Error during speed test (cb): {e}", exc_info=True)
         bot.answer_callback_query(call.id, "Error in speed test.", show_alert=True)
         try: bot.edit_message_text("Main Menu", chat_id, call.message.message_id, reply_markup=create_main_menu_inline(user_id))
         except Exception: pass

def back_to_main_callback(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    file_limit = get_user_file_limit(user_id)
    current_files = get_user_file_count(user_id)
    limit_str = str(file_limit) if file_limit != float('inf') else "Unlimited"
    expiry_info = ""
    if user_id == OWNER_ID: user_status = "Owner"
    elif user_id in admin_ids: user_status = "Admin"
    elif user_id in user_subscriptions:
        expiry_date = user_subscriptions[user_id].get('expiry')
        if expiry_date and expiry_date > datetime.now():
            user_status = "Premium"; days_left = (expiry_date - datetime.now()).days
            expiry_info = f"\nSubscription expires in: {days_left} days"
        else: user_status = "Free User (Expired Sub)"
    else: user_status = "Free User"
    
    admin_info = ""
    if user_id in admin_ids:
        pending_count = get_pending_files_count()
        if pending_count > 0:
            admin_info = f"\n📋 Pending Files: {pending_count}"
    
    main_menu_text = (f"Welcome back, {call.from_user.first_name}!\n\nID: `{user_id}`\n"
                      f"Status: {user_status}{expiry_info}{admin_info}\nFiles: {current_files} / {limit_str}\n\n"
                      f"Use buttons or type commands.")
    try:
        bot.answer_callback_query(call.id)
        bot.edit_message_text(main_menu_text, chat_id, call.message.message_id,
                              reply_markup=create_main_menu_inline(user_id), parse_mode='Markdown')
    except telebot.apihelper.ApiTelegramException as e:
         if "message is not modified" in str(e): logger.warning("Msg not modified (back_to_main).")
         else: logger.error(f"API error on back_to_main: {e}")
    except Exception as e: logger.error(f"Error handling back_to_main: {e}", exc_info=True)

def subscription_management_callback(call):
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text("Subscription Management\nSelect action:",
                              call.message.chat.id, call.message.message_id, reply_markup=create_subscription_menu())
    except Exception as e: logger.error(f"Error showing sub menu: {e}")

def stats_callback(call):
    bot.answer_callback_query(call.id)
    _logic_statistics(call.message)
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                      reply_markup=create_main_menu_inline(call.from_user.id))
    except Exception as e:
        logger.error(f"Error updating menu after stats_callback: {e}")

def lock_bot_callback(call):
    global bot_locked; bot_locked = True
    logger.warning(f"Bot locked by Admin {call.from_user.id}")
    bot.answer_callback_query(call.id, "Bot locked.")
    try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=create_main_menu_inline(call.from_user.id))
    except Exception as e: logger.error(f"Error updating menu (lock): {e}")

def unlock_bot_callback(call):
    global bot_locked; bot_locked = False
    logger.warning(f"Bot unlocked by Admin {call.from_user.id}")
    bot.answer_callback_query(call.id, "Bot unlocked.")
    try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=create_main_menu_inline(call.from_user.id))
    except Exception as e: logger.error(f"Error updating menu (unlock): {e}")

def run_all_scripts_callback(call):
    _logic_run_all_scripts(call)

def broadcast_init_callback(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "Send message to broadcast.\n/cancel to abort.")
    bot.register_next_step_handler(msg, process_broadcast_message)

def process_broadcast_message(message):
    user_id = message.from_user.id
    if user_id not in admin_ids: bot.reply_to(message, "Not authorized."); return
    if message.text and message.text.lower() == '/cancel': bot.reply_to(message, "Broadcast cancelled."); return

    broadcast_content = message.text
    if not broadcast_content and not (message.photo or message.video or message.document or message.sticker or message.voice or message.audio):
         bot.reply_to(message, "Cannot broadcast empty message. Send text or media, or /cancel.")
         msg = bot.send_message(message.chat.id, "Send broadcast message or /cancel.")
         bot.register_next_step_handler(msg, process_broadcast_message)
         return

    target_count = len(active_users)
    markup = types.InlineKeyboardMarkup()
    markup.row(types.InlineKeyboardButton("Confirm & Send", callback_data=f"confirm_broadcast_{message.message_id}"),
               types.InlineKeyboardButton("Cancel", callback_data="cancel_broadcast"))

    preview_text = broadcast_content[:1000].strip() if broadcast_content else "(Media message)"
    bot.reply_to(message, f"Confirm Broadcast:\n\n```\n{preview_text}\n```\n"
                          f"To **{target_count}** users. Sure?", reply_markup=markup, parse_mode='Markdown')

def handle_confirm_broadcast(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    if user_id not in admin_ids: bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
    try:
        original_message = call.message.reply_to_message
        if not original_message: raise ValueError("Could not retrieve original message.")

        broadcast_text = None
        broadcast_photo_id = None
        broadcast_video_id = None

        if original_message.text:
            broadcast_text = original_message.text
        elif original_message.photo:
            broadcast_photo_id = original_message.photo[-1].file_id
        elif original_message.video:
            broadcast_video_id = original_message.video.file_id
        else:
            raise ValueError("Message has no text or supported media for broadcast.")

        bot.answer_callback_query(call.id, "Starting broadcast...")
        bot.edit_message_text(f"Broadcasting to {len(active_users)} users...",
                              chat_id, call.message.message_id, reply_markup=None)
        thread = threading.Thread(target=execute_broadcast, args=(
            broadcast_text, broadcast_photo_id, broadcast_video_id,
            original_message.caption if (broadcast_photo_id or broadcast_video_id) else None,
            chat_id))
        thread.start()
    except ValueError as ve:
        logger.error(f"Error retrieving msg for broadcast confirm: {ve}")
        bot.edit_message_text(f"Error starting broadcast: {ve}", chat_id, call.message.message_id, reply_markup=None)
    except Exception as e:
        logger.error(f"Error in handle_confirm_broadcast: {e}", exc_info=True)
        bot.edit_message_text("Unexpected error during broadcast confirm.", chat_id, call.message.message_id, reply_markup=None)

def handle_cancel_broadcast(call):
    bot.answer_callback_query(call.id, "Broadcast cancelled.")
    bot.delete_message(call.message.chat.id, call.message.message_id)
    if call.message.reply_to_message:
        try: bot.delete_message(call.message.chat.id, call.message.reply_to_message.message_id)
        except: pass

def execute_broadcast(broadcast_text, photo_id, video_id, caption, admin_chat_id):
    sent_count = 0; failed_count = 0; blocked_count = 0
    start_exec_time = time.time()
    users_to_broadcast = list(active_users); total_users = len(users_to_broadcast)
    logger.info(f"Executing broadcast to {total_users} users.")
    batch_size = 25; delay_batches = 1.5

    for i, user_id_bc in enumerate(users_to_broadcast):
        try:
            if broadcast_text:
                bot.send_message(user_id_bc, broadcast_text, parse_mode='Markdown')
            elif photo_id:
                bot.send_photo(user_id_bc, photo_id, caption=caption, parse_mode='Markdown' if caption else None)
            elif video_id:
                bot.send_video(user_id_bc, video_id, caption=caption, parse_mode='Markdown' if caption else None)
            sent_count += 1
        except telebot.apihelper.ApiTelegramException as e:
            err_desc = str(e).lower()
            if any(s in err_desc for s in ["bot was blocked", "user is deactivated", "chat not found", "kicked from", "restricted"]):
                logger.warning(f"Broadcast failed to {user_id_bc}: User blocked/inactive.")
                blocked_count += 1
            elif "flood control" in err_desc or "too many requests" in err_desc:
                retry_after = 5; match = re.search(r"retry after (\d+)", err_desc)
                if match: retry_after = int(match.group(1)) + 1
                logger.warning(f"Flood control. Sleeping {retry_after}s...")
                time.sleep(retry_after)
                try:
                    if broadcast_text: bot.send_message(user_id_bc, broadcast_text, parse_mode='Markdown')
                    elif photo_id: bot.send_photo(user_id_bc, photo_id, caption=caption, parse_mode='Markdown' if caption else None)
                    elif video_id: bot.send_video(user_id_bc, video_id, caption=caption, parse_mode='Markdown' if caption else None)
                    sent_count += 1
                except Exception as e_retry: logger.error(f"Broadcast retry failed to {user_id_bc}: {e_retry}"); failed_count +=1
            else: logger.error(f"Broadcast failed to {user_id_bc}: {e}"); failed_count += 1
        except Exception as e: logger.error(f"Unexpected error broadcasting to {user_id_bc}: {e}"); failed_count += 1

        if (i + 1) % batch_size == 0 and i < total_users - 1:
            logger.info(f"Broadcast batch {i//batch_size + 1} sent. Sleeping {delay_batches}s...")
            time.sleep(delay_batches)
        elif i % 5 == 0: time.sleep(0.2)

    duration = round(time.time() - start_exec_time, 2)
    result_msg = (f"Broadcast Complete!\n\nSent: {sent_count}\nFailed: {failed_count}\n"
                  f"Blocked/Inactive: {blocked_count}\nTargets: {total_users}\nDuration: {duration}s")
    logger.info(result_msg)
    try: bot.send_message(admin_chat_id, result_msg)
    except Exception as e: logger.error(f"Failed to send broadcast result to admin {admin_chat_id}: {e}")

def admin_panel_callback(call):
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text("Admin Panel\nManage admins (Owner actions may be restricted).",
                              call.message.chat.id, call.message.message_id, reply_markup=create_admin_panel())
    except Exception as e: logger.error(f"Error showing admin panel: {e}")

def add_admin_init_callback(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "Enter User ID to promote to Admin.\n/cancel to abort.")
    bot.register_next_step_handler(msg, process_add_admin_id)

def process_add_admin_id(message):
    owner_id_check = message.from_user.id
    if owner_id_check != OWNER_ID: bot.reply_to(message, "Owner only."); return
    if message.text.lower() == '/cancel': bot.reply_to(message, "Admin promotion cancelled."); return
    try:
        new_admin_id = int(message.text.strip())
        if new_admin_id <= 0: raise ValueError("ID must be positive")
        if new_admin_id == OWNER_ID: bot.reply_to(message, "Owner is already Owner."); return
        if new_admin_id in admin_ids: bot.reply_to(message, f"User `{new_admin_id}` already Admin."); return
        add_admin_db(new_admin_id)
        logger.warning(f"Admin {new_admin_id} added by Owner {owner_id_check}.")
        bot.reply_to(message, f"User `{new_admin_id}` promoted to Admin.")
        try: bot.send_message(new_admin_id, "Congrats! You are now an Admin.")
        except Exception as e: logger.error(f"Failed to notify new admin {new_admin_id}: {e}")
    except ValueError:
        bot.reply_to(message, "Invalid ID. Send numerical ID or /cancel.")
        msg = bot.send_message(message.chat.id, "Enter User ID to promote or /cancel.")
        bot.register_next_step_handler(msg, process_add_admin_id)
    except Exception as e: logger.error(f"Error processing add admin: {e}", exc_info=True); bot.reply_to(message, "Error.")

def remove_admin_init_callback(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "Enter User ID of Admin to remove.\n/cancel to abort.")
    bot.register_next_step_handler(msg, process_remove_admin_id)

def process_remove_admin_id(message):
    owner_id_check = message.from_user.id
    if owner_id_check != OWNER_ID: bot.reply_to(message, "Owner only."); return
    if message.text.lower() == '/cancel': bot.reply_to(message, "Admin removal cancelled."); return
    try:
        admin_id_remove = int(message.text.strip())
        if admin_id_remove <= 0: raise ValueError("ID must be positive")
        if admin_id_remove == OWNER_ID: bot.reply_to(message, "Owner cannot remove self."); return
        if admin_id_remove not in admin_ids: bot.reply_to(message, f"User `{admin_id_remove}` not Admin."); return
        if remove_admin_db(admin_id_remove):
            logger.warning(f"Admin {admin_id_remove} removed by Owner {owner_id_check}.")
            bot.reply_to(message, f"Admin `{admin_id_remove}` removed.")
            try: bot.send_message(admin_id_remove, "You are no longer an Admin.")
            except Exception as e: logger.error(f"Failed to notify removed admin {admin_id_remove}: {e}")
        else: bot.reply_to(message, f"Failed to remove admin `{admin_id_remove}`. Check logs.")
    except ValueError:
        bot.reply_to(message, "Invalid ID. Send numerical ID or /cancel.")
        msg = bot.send_message(message.chat.id, "Enter Admin ID to remove or /cancel.")
        bot.register_next_step_handler(msg, process_remove_admin_id)
    except Exception as e: logger.error(f"Error processing remove admin: {e}", exc_info=True); bot.reply_to(message, "Error.")

def list_admins_callback(call):
    bot.answer_callback_query(call.id)
    try:
        admin_list_str = "\n".join(f"- `{aid}` {'(Owner)' if aid == OWNER_ID else ''}" for aid in sorted(list(admin_ids)))
        if not admin_list_str: admin_list_str = "(No Owner/Admins configured!)"
        bot.edit_message_text(f"Current Admins:\n\n{admin_list_str}", call.message.chat.id,
                              call.message.message_id, reply_markup=create_admin_panel(), parse_mode='Markdown')
    except Exception as e: logger.error(f"Error listing admins: {e}")

def add_subscription_init_callback(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "Enter User ID & days (e.g., `12345678 30`).\n/cancel to abort.")
    bot.register_next_step_handler(msg, process_add_subscription_details)

def process_add_subscription_details(message):
    admin_id_check = message.from_user.id
    if admin_id_check not in admin_ids: bot.reply_to(message, "Not authorized."); return
    if message.text.lower() == '/cancel': bot.reply_to(message, "Sub add cancelled."); return
    try:
        parts = message.text.split();
        if len(parts) < 2 or len(parts) > 3: raise ValueError("Incorrect format")
        sub_user_id = int(parts[0].strip()); days = int(parts[1].strip())
        plan = parts[2].strip().lower() if len(parts) == 3 else 'pro'
        if sub_user_id <= 0 or days <= 0: raise ValueError("User ID/days must be positive")
        if plan not in PLANS: raise ValueError(f"Plan must be one of: {', '.join(PLANS.keys())}")

        current_expiry = user_subscriptions.get(sub_user_id, {}).get('expiry')
        start_date_new_sub = datetime.now()
        if current_expiry and current_expiry > start_date_new_sub: start_date_new_sub = current_expiry
        new_expiry = start_date_new_sub + timedelta(days=days)
        save_subscription(sub_user_id, new_expiry, plan)

        logger.info(f"Sub for {sub_user_id} by admin {admin_id_check}. Plan: {plan}. Expiry: {new_expiry:%Y-%m-%d}")
        bot.reply_to(message, f"Sub for `{sub_user_id}` by {days} days.\nPlan: `{plan}`\nNew expiry: {new_expiry:%Y-%m-%d}")
        try: bot.send_message(sub_user_id, f"Sub activated/extended by {days} days! Plan: {plan.title()}\nExpires: {new_expiry:%Y-%m-%d}.")
        except Exception as e: logger.error(f"Failed to notify {sub_user_id} of new sub: {e}")
    except ValueError as e:
        bot.reply_to(message, f"Invalid: {e}. Format: `ID days [plan]` or /cancel.")
        msg = bot.send_message(message.chat.id, "Enter User ID, days and optional plan (starter/pro/business), or /cancel.")
        bot.register_next_step_handler(msg, process_add_subscription_details)
    except Exception as e: logger.error(f"Error processing add sub: {e}", exc_info=True); bot.reply_to(message, "Error.")

def remove_subscription_init_callback(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "Enter User ID to remove sub.\n/cancel to abort.")
    bot.register_next_step_handler(msg, process_remove_subscription_id)

def process_remove_subscription_id(message):
    admin_id_check = message.from_user.id
    if admin_id_check not in admin_ids: bot.reply_to(message, "Not authorized."); return
    if message.text.lower() == '/cancel': bot.reply_to(message, "Sub removal cancelled."); return
    try:
        sub_user_id_remove = int(message.text.strip())
        if sub_user_id_remove <= 0: raise ValueError("ID must be positive")
        if sub_user_id_remove not in user_subscriptions:
            bot.reply_to(message, f"User `{sub_user_id_remove}` no active sub in memory."); return
        remove_subscription_db(sub_user_id_remove)
        logger.warning(f"Sub removed for {sub_user_id_remove} by admin {admin_id_check}.")
        bot.reply_to(message, f"Sub for `{sub_user_id_remove}` removed.")
        try: bot.send_message(sub_user_id_remove, "Your subscription removed by admin.")
        except Exception as e: logger.error(f"Failed to notify {sub_user_id_remove} of sub removal: {e}")
    except ValueError:
        bot.reply_to(message, "Invalid ID. Send numerical ID or /cancel.")
        msg = bot.send_message(message.chat.id, "Enter User ID to remove sub from, or /cancel.")
        bot.register_next_step_handler(msg, process_remove_subscription_id)
    except Exception as e: logger.error(f"Error processing remove sub: {e}", exc_info=True); bot.reply_to(message, "Error.")

def check_subscription_init_callback(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "Enter User ID to check sub.\n/cancel to abort.")
    bot.register_next_step_handler(msg, process_check_subscription_id)

def process_check_subscription_id(message):
    admin_id_check = message.from_user.id
    if admin_id_check not in admin_ids: bot.reply_to(message, "Not authorized."); return
    if message.text.lower() == '/cancel': bot.reply_to(message, "Sub check cancelled."); return
    try:
        sub_user_id_check = int(message.text.strip())
        if sub_user_id_check <= 0: raise ValueError("ID must be positive")
        if sub_user_id_check in user_subscriptions:
            expiry_dt = user_subscriptions[sub_user_id_check].get('expiry')
            if expiry_dt:
                if expiry_dt > datetime.now():
                    days_left = (expiry_dt - datetime.now()).days
                    bot.reply_to(message, f"User `{sub_user_id_check}` active sub.\nExpires: {expiry_dt:%Y-%m-%d %H:%M:%S} ({days_left} days left).")
                else:
                    bot.reply_to(message, f"User `{sub_user_id_check}` expired sub (On: {expiry_dt:%Y-%m-%d %H:%M:%S}).")
                    remove_subscription_db(sub_user_id_check)
            else: bot.reply_to(message, f"User `{sub_user_id_check}` in sub list, but expiry missing. Re-add if needed.")
        else: bot.reply_to(message, f"User `{sub_user_id_check}` no active sub record.")
    except ValueError:
        bot.reply_to(message, "Invalid ID. Send numerical ID or /cancel.")
        msg = bot.send_message(message.chat.id, "Enter User ID to check, or /cancel.")
        bot.register_next_step_handler(msg, process_check_subscription_id)
    except Exception as e: logger.error(f"Error processing check sub: {e}", exc_info=True); bot.reply_to(message, "Error.")

def cleanup():
    logger.warning("Shutdown. Cleaning up processes...")
    script_keys_to_stop = list(bot_scripts.keys())
    if not script_keys_to_stop: logger.info("No scripts running. Exiting."); return
    logger.info(f"Stopping {len(script_keys_to_stop)} scripts...")
    for key in script_keys_to_stop:
        if key in bot_scripts: logger.info(f"Stopping: {key}"); kill_process_tree(bot_scripts[key])
        else: logger.info(f"Script {key} already removed.")
    logger.warning("Cleanup finished.")
atexit.register(cleanup)

# Graceful shutdown: systemd and Docker send SIGTERM on stop/restart.
# atexit does NOT run on signals, so handle them explicitly to kill all
# child (user-uploaded) processes before exiting.
def _handle_shutdown_signal(signum, frame):
    logger.warning(f"Received signal {signum}. Cleaning up child processes...")
    cleanup()
    sys.exit(0)

signal.signal(signal.SIGTERM, _handle_shutdown_signal)
if hasattr(signal, 'SIGINT'):
    signal.signal(signal.SIGINT, _handle_shutdown_signal)

def _locate_script_dir(user_id, file_name):
    """Return the directory that actually contains the script file for a user.
    Falls back to the user's root folder when the file cannot be found."""
    folder = get_user_folder(user_id)
    direct = os.path.join(folder, file_name)
    if os.path.exists(direct):
        return os.path.dirname(direct) or folder
    for root, dirs, files in os.walk(folder):
        if file_name in files:
            return root
    return folder

def _db_get_env(user_id, file_name):
    """Get a user's env vars for a file from MongoDB. Falls back to / migrates
    the per-script .env file on disk."""
    try:
        doc = db.user_env.find_one({'user_id': user_id, 'file_name': file_name})
        if doc and isinstance(doc.get('env'), dict):
            return dict(doc['env'])
    except Exception as e:
        logger.error(f"DB env read error for {user_id}/{file_name}: {e}")
    env_data = {}
    env_path = os.path.join(_locate_script_dir(user_id, file_name), '.env')
    if os.path.exists(env_path):
        try:
            with open(env_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    k, _, v = line.partition('=')
                    env_data[k.strip()] = v.strip().strip('"').strip("'")
            if env_data:
                try:
                    db.user_env.update_one(
                        {'user_id': user_id, 'file_name': file_name},
                        {'$set': {'env': env_data}}, upsert=True)
                except Exception as e:
                    logger.error(f"DB env migrate error for {user_id}: {e}")
        except Exception as e:
            logger.error(f"Legacy .env read error for {user_id}: {e}")
    return env_data

def _db_set_env(user_id, file_name, env_data):
    """Store a user's env vars for a file in MongoDB and mirror them to the
    local .env file (so scripts that read .env themselves still work)."""
    env_data = dict(env_data or {})
    try:
        db.user_env.update_one(
            {'user_id': user_id, 'file_name': file_name},
            {'$set': {'env': env_data}}, upsert=True)
    except Exception as e:
        logger.error(f"DB env write error for {user_id}/{file_name}: {e}")
    env_path = os.path.join(_locate_script_dir(user_id, file_name), '.env')
    try:
        with open(env_path, 'w', encoding='utf-8') as f:
            for k, v in env_data.items():
                v_str = str(v)
                if ' ' in v_str or '#' in v_str:
                    v_str = f'"{v_str}"'
                f.write(f"{k}={v_str}\n")
    except Exception as e:
        logger.error(f"Local .env mirror write error for {user_id}: {e}")
    return env_path

def _parse_env_text(text):
    """Parse a .env file string into a dict."""
    env_data = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, _, v = line.partition('=')
        env_data[k.strip()] = v.strip().strip('"').strip("'")
    return env_data

# ====================== DB-BACKED FILE STORAGE ======================
# Files are kept in MongoDB (the "user_file_data" collection) between runs.
# They are only written to the VPS disk once a user hits Start, and are
# destroyed from the disk again when the user stops the bot. The DB copy
# stays until the user (or admin) permanently deletes the file.
# MongoDB documents are limited to 16 MB, so uploads approaching the 20 MB
# cap may fail to store - an accepted trade-off for this DB-first model.

def _store_project_data(user_id, file_name, file_type, files, is_project=False):
    """Upsert a user's file/project content into MongoDB (user_file_data)."""
    try:
        db.user_file_data.replace_one(
            {'user_id': user_id, 'file_name': file_name},
            {'user_id': user_id, 'file_name': file_name, 'file_type': file_type,
             'is_project': bool(is_project), 'files': files,
             'updated_at': datetime.now().isoformat()},
            upsert=True)
        logger.info(f"Stored {len(files)} file(s) for {user_id}/{file_name} in DB (project={bool(is_project)})")
        return True
    except Exception as e:
        logger.error(f"Error storing {user_id}/{file_name} in DB: {e}", exc_info=True)
        return False


def _store_single_file_data(user_id, file_name, file_type, content):
    """Store a single uploaded file's bytes into MongoDB (not onto disk)."""
    try:
        content = bytes(content) if content is not None else b''
    except Exception:
        content = (content or '').encode('utf-8', errors='ignore')
    return _store_project_data(user_id, file_name, file_type,
                               [{'path': file_name, 'content': content}], is_project=False)


def _materialize_project(user_id, file_name):
    """Write a user's project files from MongoDB to the VPS disk so the bot can run."""
    user_folder = get_user_folder(user_id)
    try:
        doc = db.user_file_data.find_one({'user_id': user_id, 'file_name': file_name})
        if not doc or not doc.get('files'):
            logger.warning(f"No DB content found for {user_id}/{file_name}. File was not materialized.")
            return False
        folder_abs = os.path.abspath(user_folder)
        written = 0
        for f in doc['files']:
            rel = (f.get('path') or '').replace('\\', '/')
            if not rel or os.path.isabs(rel):
                continue
            dest = os.path.abspath(os.path.join(user_folder, rel))
            if not dest.startswith(folder_abs + os.sep) and dest != folder_abs:
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            content = f.get('content')
            if isinstance(content, bytes):
                with open(dest, 'wb') as fh:
                    fh.write(content)
            else:
                with open(dest, 'w', encoding='utf-8', errors='ignore') as fh:
                    fh.write(str(content or ''))
            written += 1
        logger.info(f"Materialized {written} file(s) for {user_id}/{file_name} to {user_folder}")
        return True
    except Exception as e:
        logger.error(f"Error materializing {user_id}/{file_name}: {e}", exc_info=True)
        return False


def _destroy_project(user_id, file_name):
    """Remove a user's project files from the VPS disk. The DB copy is kept."""
    user_folder = get_user_folder(user_id)
    removed = []
    try:
        doc = db.user_file_data.find_one({'user_id': user_id, 'file_name': file_name})
        if doc and doc.get('files'):
            targets = [f.get('path') for f in doc['files']]
        else:
            targets = [file_name]
        folder_abs = os.path.abspath(user_folder)
        for rel in targets:
            rel = (rel or '').replace('\\', '/')
            if not rel or os.path.isabs(rel):
                continue
            dest = os.path.abspath(os.path.join(user_folder, rel))
            if not dest.startswith(folder_abs + os.sep) and dest != folder_abs:
                continue
            try:
                if os.path.exists(dest):
                    os.remove(dest)
                    removed.append(os.path.basename(dest))
            except OSError as e:
                logger.error(f"Error destroying {dest}: {e}")
        if removed:
            logger.info(f"Destroyed VPS files for {user_id}/{file_name}: {removed}")
        return removed
    except Exception as e:
        logger.error(f"Error destroying project {user_id}/{file_name}: {e}", exc_info=True)
        return removed


def _resolve_display_name(user_id, fallback):
    """Best-effort Telegram display name with a bounded timeout so a slow or
    unreachable Telegram API call can never block (and hang) the web dashboard
    HTTP handler in this same process."""
    if not user_id:
        return fallback
    result = {}
    def _lookup():
        try:
            result['c'] = bot.get_chat(user_id)
        except Exception:
            result['c'] = None
    th = threading.Thread(target=_lookup, daemon=True)
    th.start()
    th.join(timeout=2.5)
    if th.is_alive():
        logger.warning(f"get_chat({user_id}) timed out; using fallback display name")
        return fallback
    c = result.get('c')
    if c is None:
        return fallback
    try:
        name = c.first_name or fallback
    except Exception:
        name = fallback
    try:
        if getattr(c, 'username', None):
            name = f"@{c.username}"
    except Exception:
        pass
    return name


def start_status_server():
    """HTTP server: public /health + /stats for the landing page, plus the
    authenticated web dashboard API (/api/...) backed by WEB_USERS logins."""
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import urlparse, parse_qs

    MAX_LOG_BYTES = 64 * 1024

    def _stats():
        running = 0
        for key, info in list(bot_scripts.items()):
            try:
                owner_id = int(key.split('_', 1)[0])
                if is_bot_running(owner_id, info['file_name']):
                    running += 1
            except Exception:
                continue
        return {
            'status': 'ok',
            'bot': 'HostBot',
            'uptime': get_uptime(),
            'locked': bot_locked,
            'total_users': len(active_users),
            'running_bots': running,
            'pending_files': get_pending_files_count(),
            'plans': {k: v['limit'] for k, v in PLANS.items()},
        }

    def _get_session(token):
        if not token:
            return None
        sess = web_sessions.get(token)
        if not sess:
            return None
        if sess['expires'] < datetime.now():
            web_sessions.pop(token, None)
            return None
        return sess

    def _make_fake_message(user_id):
        class _User:
            def __init__(self):
                self.id = user_id
                self.first_name = 'Web Dashboard'
                self.username = None
                self.is_bot = False
        class _Chat:
            def __init__(self):
                self.id = user_id
                self.type = 'private'
        class _FakeMessage:
            def __init__(self):
                self.from_user = _User()
                self.chat = _Chat()
                self.message_id = 0
                self.text = '/start from web dashboard'
        return _FakeMessage()

    def _dashboard_for(sess):
        user_id = sess['telegram_id']
        username = sess['username']
        plan_key = get_user_plan(user_id)
        # Keep owner/admin role a secret from the web UI - display as "Pro".
        if plan_key in ('owner', 'admin'):
            plan_label = PLANS['pro']['name']
            limit = PLANS['pro']['limit']
        else:
            plan_label = PLANS.get(plan_key, PLANS['free'])['name']
            limit = PLANS.get(plan_key, PLANS['free'])['limit']
        limit_display = "Unlimited" if limit == float('inf') else limit

        registered_plan = sess.get('plan')
        plan_note = ""
        if registered_plan and registered_plan != 'free' and plan_key == 'free':
            plan_note = (f"Paid plan '{PLANS.get(registered_plan, {}).get('name', registered_plan)}' "
                         f"requested - waiting for admin activation.")

        display_name = _resolve_display_name(user_id, username)

        sub = user_subscriptions.get(user_id)
        expires = sub['expiry'].isoformat() if sub else None

        files = []
        for fn, ft in user_files.get(user_id, []):
            status = get_file_status(user_id, fn)['status']
            running = is_bot_running(user_id, fn)
            key = f"{user_id}_{fn}"
            info = bot_scripts.get(key)
            log_path = os.path.join(get_user_folder(user_id), f"{os.path.splitext(fn)[0]}.log")
            files.append({
                'file_name': fn,
                'file_type': ft,
                'status': status,
                'running': running,
                'pid': info['process'].pid if (info and running and info.get('process')) else None,
                'start_time': info['start_time'].isoformat() if (info and info.get('start_time')) else None,
                'log_exists': os.path.exists(log_path),
            })
        return {
            'username': username,
            'display_name': display_name,
            'telegram_id': user_id,
            'plan': 'pro' if plan_key in ('owner', 'admin') else plan_key,
            'plan_label': plan_label,
            'registered_plan': registered_plan,
            'plan_note': plan_note,
            'limit': limit_display,
            'expires': expires,
            'locked': bot_locked,
            'files_count': len(files),
            'running_count': sum(1 for f in files if f['running']),
            'pending_count': sum(1 for f in files if f['status'] == FILE_STATUS_PENDING),
            'files': files,
        }

    def _read_env(user_id, file_name):
        env_data = dict(_db_get_env(user_id, file_name))
        env_path = os.path.join(_locate_script_dir(user_id, file_name), '.env')
        if os.path.exists(env_path):
            try:
                with open(env_path, 'r', encoding='utf-8', errors='ignore') as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith('#') or '=' not in line:
                            continue
                        k, _, v = line.partition('=')
                        env_data.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            except Exception as e:
                logger.error(f"Local .env read error for {user_id}/{file_name}: {e}")
        return env_path, env_data

    def _write_env(user_id, file_name, env_data):
        return _db_set_env(user_id, file_name, env_data)

    def _file_owned(user_id, file_name):
        return any(fn == file_name for fn, _ in user_files.get(user_id, []))

    def _bot_action(user_id, file_name, action):
        if not _file_owned(user_id, file_name):
            return {'ok': False, 'error': 'File not found in your account'}
        ft = next((t for fn, t in user_files.get(user_id, []) if fn == file_name), None)
        key = f"{user_id}_{file_name}"
        running = is_bot_running(user_id, file_name)
        user_folder = get_user_folder(user_id)
        script_path = os.path.join(user_folder, file_name)

        if action == 'start':
            if running:
                return {'ok': False, 'error': f"'{file_name}' is already running"}
            status = get_file_status(user_id, file_name)['status']
            if status != FILE_STATUS_APPROVED:
                return {'ok': False, 'error': f"'{file_name}' is {status}. It must be approved first."}
            if not _materialize_project(user_id, file_name):
                return {'ok': False, 'error': f"Stored file '{file_name}' not found in the database."}
            fake = _make_fake_message(user_id)
            if ft == 'js':
                threading.Thread(target=run_js_script, args=(script_path, user_id, user_folder, file_name, fake)).start()
            else:
                threading.Thread(target=run_script, args=(script_path, user_id, user_folder, file_name, fake)).start()
            return {'ok': True, 'message': f"'{file_name}' is starting..."}
        elif action == 'stop':
            if not running:
                return {'ok': False, 'error': f"'{file_name}' is not running"}
            info = bot_scripts.get(key)
            if info:
                kill_process_tree(info)
                bot_scripts.pop(key, None)
            # Destroy the VPS files; the DB copy stays until the user deletes it.
            try: _destroy_project(user_id, file_name)
            except Exception as e: logger.error(f"Web stop destroy error for {key}: {e}")
            return {'ok': True, 'message': f"'{file_name}' stopped"}
        elif action == 'restart':
            info = bot_scripts.get(key)
            if info and running:
                kill_process_tree(info)
                bot_scripts.pop(key, None)
            status = get_file_status(user_id, file_name)['status']
            if status != FILE_STATUS_APPROVED:
                return {'ok': False, 'error': f"'{file_name}' is {status}. It must be approved first."}
            if not _materialize_project(user_id, file_name):
                return {'ok': False, 'error': f"Stored file '{file_name}' not found in the database."}
            fake = _make_fake_message(user_id)
            if ft == 'js':
                threading.Thread(target=run_js_script, args=(script_path, user_id, user_folder, file_name, fake)).start()
            else:
                threading.Thread(target=run_script, args=(script_path, user_id, user_folder, file_name, fake)).start()
            return {'ok': True, 'message': f"'{file_name}' restarted"}
        return {'ok': False, 'error': 'Unknown action'}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def _send(self, payload, code=200):
            body = json.dumps(payload).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self):
            te = (self.headers.get('Transfer-Encoding') or '').lower()
            length = int(self.headers.get('Content-Length', 0) or 0)
            try:
                if length > 0:
                    raw = self.rfile.read(length)
                elif 'chunked' in te:
                    chunks = []
                    while True:
                        line = self.rfile.readline().strip()
                        if not line:
                            break
                        try:
                            size = int(line.split(b';')[0], 16)
                        except ValueError:
                            break
                        if size == 0:
                            self.rfile.readline()  # trailing CRLF
                            break
                        chunks.append(self.rfile.read(size))
                        self.rfile.readline()  # CRLF after chunk
                    raw = b''.join(chunks)
                else:
                    raw = self.rfile.read()
                return json.loads(raw.decode('utf-8', errors='ignore')) if raw.strip() else {}
            except Exception:
                return {}

        def _token_from(self, body=None):
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            token = (qs.get('token') or [None])[0]
            if not token and body:
                token = body.get('token')
            if not token:
                auth = self.headers.get('Authorization') or ''
                if auth.startswith('Bearer '):
                    token = auth[7:].strip()
            return token

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Content-Length', '0')
            self.end_headers()

        def do_GET(self):
            path = urlparse(self.path).path
            qs = parse_qs(urlparse(self.path).query)

            if path in ('/', '/health', '/stats'):
                if STATUS_TOKEN and self.headers.get('Authorization') != f'Bearer {STATUS_TOKEN}':
                    return self._send({'error': 'unauthorized'}, 401)
                return self._send(_stats())

            if path == '/api/plans':
                return self._send({
                    'plans': [
                        {'key': k, 'name': v['name'], 'limit': v['limit']}
                        for k, v in PLANS.items()
                    ],
                    'status': 'ok',
                })

            if path == '/api/dashboard':
                sess = _get_session(self._token_from())
                if not sess:
                    return self._send({'error': 'invalid or expired session'}, 401)
                return self._send(_dashboard_for(sess))

            if path == '/api/logs':
                sess = _get_session(self._token_from())
                if not sess:
                    return self._send({'error': 'invalid or expired session'}, 401)
                file_name = (qs.get('file') or [None])[0]
                if not file_name or not _file_owned(sess['telegram_id'], file_name):
                    return self._send({'error': 'file not found'}, 404)
                user_folder = get_user_folder(sess['telegram_id'])
                log_path = os.path.join(user_folder, f"{os.path.splitext(file_name)[0]}.log")
                if not os.path.exists(log_path):
                    return self._send({'error': 'no logs yet', 'logs': ''})
                try:
                    size = os.path.getsize(log_path)
                    if size > MAX_LOG_BYTES:
                        with open(log_path, 'rb') as f:
                            f.seek(-MAX_LOG_BYTES, os.SEEK_END)
                            logs = f.read().decode('utf-8', errors='ignore')
                        truncated = True
                    else:
                        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                            logs = f.read()
                        truncated = False
                    return self._send({'ok': True, 'logs': logs, 'truncated': truncated})
                except Exception as e:
                    logger.error(f"Web logs error for {file_name}: {e}", exc_info=True)
                    return self._send({'error': 'failed to read logs'}, 500)

            if path == '/api/env':
                sess = _get_session(self._token_from())
                if not sess:
                    return self._send({'error': 'invalid or expired session'}, 401)
                file_name = (qs.get('file') or [None])[0]
                if not file_name or not _file_owned(sess['telegram_id'], file_name):
                    return self._send({'error': 'file not found'}, 404)
                try:
                    _, env_data = _read_env(sess['telegram_id'], file_name)
                    return self._send({'ok': True, 'env': env_data})
                except Exception as e:
                    logger.error(f"Web env read error for {file_name}: {e}", exc_info=True)
                    return self._send({'error': 'failed to read env'}, 500)

            self._send({'error': 'not found'}, 404)

        def do_POST(self):
            path = urlparse(self.path).path
            body = self._read_body()

            if path == '/api/login':
                username = (body.get('username') or '').strip()
                password = body.get('password') or ''
                telegram_id, account = verify_web_login(username, password)
                if not telegram_id:
                    return self._send({'error': 'invalid username or password'}, 401)
                token = __import__('secrets').token_urlsafe(32)
                web_sessions[token] = {
                    'telegram_id': telegram_id,
                    'username': username,
                    'plan': account.get('plan') if account else None,
                    'expires': datetime.now() + timedelta(seconds=WEB_SESSION_TTL),
                }
                sess = web_sessions[token]
                return self._send({'ok': True, 'token': token, 'dashboard': _dashboard_for(sess)})

            if path == '/api/register':
                username = (body.get('username') or '').strip()
                password = body.get('password') or ''
                plan = (body.get('plan') or 'free').lower()
                try:
                    telegram_id = int(body.get('telegram_id'))
                except (TypeError, ValueError):
                    telegram_id = 0
                ok, message = register_web_user(username, password, telegram_id, plan)
                if not ok:
                    return self._send({'error': message}, 400)
                return self._send({'ok': True, 'message': message})

            if path == '/api/logout':
                token = self._token_from(body)
                if token:
                    web_sessions.pop(token, None)
                return self._send({'ok': True})

            if path == '/api/env':
                sess = _get_session(self._token_from(body))
                if not sess:
                    return self._send({'error': 'invalid or expired session'}, 401)
                file_name = body.get('file')
                new_env = body.get('env')
                if not file_name or not _file_owned(sess['telegram_id'], file_name):
                    return self._send({'error': 'file not found'}, 404)
                if isinstance(new_env, str):
                    new_env = _parse_env_text(new_env)
                if not isinstance(new_env, dict):
                    return self._send({'error': 'env must be an object of key/value pairs or raw .env text'}, 400)
                env_path, env_data = _read_env(sess['telegram_id'], file_name)
                env_key_re = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
                for k in new_env:
                    if not env_key_re.match(k):
                        return self._send({'error': f"Invalid env key: {k}. Use letters, digits and underscore."}, 400)
                for k, v in new_env.items():
                    env_data[k] = str(v).replace('\n', ' ').replace('\r', ' ').strip()
                try:
                    _write_env(sess['telegram_id'], file_name, env_data)
                    # If the bot is already running, restart it so the new env
                    # takes effect immediately (env is loaded at process start).
                    restarted = False
                    if is_bot_running(sess['telegram_id'], file_name):
                        _bot_action(sess['telegram_id'], file_name, 'restart')
                        restarted = True
                    return self._send({'ok': True, 'message': f"Environment updated for '{file_name}'" + (" and bot restarted with new env" if restarted else "")})
                except Exception as e:
                    logger.error(f"Web env write error for {file_name}: {e}", exc_info=True)
                    return self._send({'error': 'failed to write env'}, 500)

            if path == '/api/bot':
                sess = _get_session(self._token_from(body))
                if not sess:
                    return self._send({'error': 'invalid or expired session'}, 401)
                file_name = body.get('file')
                action = body.get('action')
                if not file_name or action not in ('start', 'stop', 'restart'):
                    return self._send({'error': 'file and action (start/stop/restart) required'}, 400)
                return self._send(_bot_action(sess['telegram_id'], file_name, action))

            if path == '/api/clear':
                sess = _get_session(self._token_from(body))
                if not sess:
                    return self._send({'error': 'invalid or expired session'}, 401)
                try:
                    _clear_all_files(sess['telegram_id'])
                    return self._send({'ok': True, 'message': 'All uploaded files cleared'})
                except Exception as e:
                    logger.error(f"Web clear-all error for {sess['telegram_id']}: {e}", exc_info=True)
                    return self._send({'error': 'failed to clear files'}, 500)

            if path == '/api/delete':
                sess = _get_session(self._token_from(body))
                if not sess:
                    return self._send({'error': 'invalid or expired session'}, 401)
                file_name = body.get('file')
                if not file_name:
                    return self._send({'error': 'file required'}, 400)
                return self._send(_delete_user_file(sess['telegram_id'], file_name))

            self._send({'error': 'not found'}, 404)

        def log_message(self, *args):
            pass

    try:
        server = ThreadingHTTPServer(('0.0.0.0', STATUS_SERVER_PORT), Handler)
        logger.info(f"Status server listening on http://0.0.0.0:{STATUS_SERVER_PORT}")
        server.serve_forever()
    except Exception as e:
        logger.error(f"Status server failed to start: {e}", exc_info=True)


if __name__ == '__main__':
    logger.info("=" * 40 + "\nHostBot Starting Up...\n" + f"Python: {sys.version.split()[0]}\n" +
                f"Base Dir: {BASE_DIR}\nData Dir: {DATA_DIR}\nUpload Dir: {UPLOAD_BOTS_DIR}\n" +
                f"Owner ID: {OWNER_ID}\nAdmins: {admin_ids}\n" +
                f"Start Time: {BOT_START_TIME}" + "=" * 40)

    if STATUS_SERVER_ENABLED:
        threading.Thread(target=start_status_server, daemon=True).start()

    logger.info("Starting bot polling...")

    # VPS / long-running compatible polling loop
    while True:
        try:
            bot.infinity_polling(logger_level=logging.INFO, timeout=60, long_polling_timeout=30)
        except requests.exceptions.ReadTimeout:
            logger.warning("Polling ReadTimeout. Restarting in 5s...")
            time.sleep(5)
        except requests.exceptions.ConnectionError as ce:
            logger.error(f"Polling ConnectionError: {ce}. Retrying in 15s...")
            time.sleep(15)
        except Exception as e:
            logger.critical(f"Unrecoverable polling error: {e}", exc_info=True)
            logger.info("Restarting polling in 30s due to critical error...")
            time.sleep(30)
        finally:
            logger.warning("Polling attempt finished. Will restart if in loop.")
            time.sleep(1)
