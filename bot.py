import asyncio
import logging
import os
import sys
import html
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any, Union

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import PyMongoError

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatMember,
    ChatInviteLink,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.error import TelegramError, RetryAfter, Forbidden, BadRequest

# -----------------------------------------------------------------------------
# Logging Configuration
# -----------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("LinkProviderBot")

# -----------------------------------------------------------------------------
# Environment Configuration
# -----------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017").strip()
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "link_provider_bot").strip()

LOG_CHANNEL_ID_RAW = os.getenv("LOG_CHANNEL_ID", "").strip()
LOG_CHANNEL_ID = int(LOG_CHANNEL_ID_RAW) if LOG_CHANNEL_ID_RAW.lstrip("-").isdigit() else None

ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "").strip()
ADMIN_IDS: List[int] = []
if ADMIN_IDS_RAW:
    for aid in ADMIN_IDS_RAW.split(","):
        aid = aid.strip()
        if aid.lstrip("-").isdigit():
            ADMIN_IDS.append(int(aid))

if not BOT_TOKEN:
    logger.critical("BOT_TOKEN environment variable missing! Exiting...")

# Global DB client variable
db_client: Optional[AsyncIOMotorClient] = None
db = None

# Default Fallback Settings
DEFAULT_START_TEXT = (
    "<b>Welcome to Link Provider Bot!</b>\n\n"
    "I generate temporary, secure one-time invite links for authorized channels."
)
DEFAULT_ABOUT_TEXT = (
    "<b>About Link Provider Bot</b>\n\n"
    "• Single-use high-security invite links.\n"
    "• Automatic 2-minute expiration.\n"
    "• Private join-request integration.\n"
    "• Built with python-telegram-bot & MongoDB."
)
DEFAULT_HELP_TEXT = (
    "<b>How to use this bot:</b>\n\n"
    "1. Click a channel link provided to you.\n"
    "2. Complete required channel subscriptions (ForceSub) if prompted.\n"
    "3. Receive your custom single-use invite link.\n\n"
    "<i>Note: Links auto-expire after 2 minutes!</i>"
)

# -----------------------------------------------------------------------------
# Database Setup & Helper Functions
# -----------------------------------------------------------------------------
async def init_db():
    global db_client, db
    try:
        db_client = AsyncIOMotorClient(MONGO_URI)
        db = db_client[MONGO_DB_NAME]
        # Test connection
        await db.command("ping")
        logger.info(f"Successfully connected to MongoDB database: {MONGO_DB_NAME}")

        # Ensure indexes
        await db.users.create_index("user_id", unique=True)
        await db.bans.create_index("user_id", unique=True)
        await db.channels.create_index("chat_id", unique=True)
        await db.forcesub.create_index("chat_id", unique=True)
        await db.links.create_index("link", unique=True)
        await db.links.create_index([("expires_at", 1), ("is_revoked", 1)])
        await db.forcesub_access.create_index([("user_id", 1), ("chat_id", 1)], unique=True)

        # Initialize settings if missing
        settings = await db.settings.find_one({"_id": "global"})
        if not settings:
            await db.settings.insert_one({
                "_id": "global",
                "start_photo": "",
                "start_text": DEFAULT_START_TEXT,
                "about_text": DEFAULT_ABOUT_TEXT,
                "help_text": DEFAULT_HELP_TEXT,
                "about_button": "ℹ️ ABOUT",
                "help_button": "❓ HELP",
                "req_toggle": False,
                "autodelete_enabled": True
            })
    except Exception as e:
        logger.critical(f"MongoDB connection failed: {e}")
        sys.exit(1)

async def post_init(app: Application):
    """
    Called by Application inside the running event loop prior to polling.
    """
    await init_db()
    # Spawn background task in the running event loop
    asyncio.create_task(background_link_cleanup_loop(app))
    logger.info("Database initialized and background link cleanup loop started.")

async def get_settings() -> Dict[str, Any]:
    settings = await db.settings.find_one({"_id": "global"})
    if not settings:
        return {
            "start_photo": "",
            "start_text": DEFAULT_START_TEXT,
            "about_text": DEFAULT_ABOUT_TEXT,
            "help_text": DEFAULT_HELP_TEXT,
            "about_button": "ℹ️ ABOUT",
            "help_button": "❓ HELP",
            "req_toggle": False,
            "autodelete_enabled": True
        }
    return settings

async def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def is_banned(user_id: int) -> bool:
    ban_doc = await db.bans.find_one({"user_id": user_id})
    return ban_doc is not None

async def register_user(user) -> None:
    if not user or user.is_bot:
        return
    await db.users.update_one(
        {"user_id": user.id},
        {
            "$set": {
                "username": user.username or "",
                "first_name": user.first_name or "",
                "last_seen": datetime.now(timezone.utc),
            },
            "$setOnInsert": {
                "joined_at": datetime.now(timezone.utc),
            }
        },
        upsert=True
    )

async def log_channel_msg(bot, message: str):
    if LOG_CHANNEL_ID:
        try:
            await bot.send_message(chat_id=LOG_CHANNEL_ID, text=message, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Failed to send log to channel {LOG_CHANNEL_ID}: {e}")

# -----------------------------------------------------------------------------
# ForceSub Verification Core Logic
# -----------------------------------------------------------------------------
async def check_forcesub_member(bot, chat_id: Union[int, str], user_id: int) -> bool:
    """
    Checks whether a user is a member of a ForceSub channel or has granted bot-side access.
    """
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        if member.status in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
            return True
        if member.status == ChatMember.RESTRICTED and getattr(member, "is_member", False):
            return True
    except TelegramError:
        pass

    # Check if private forcesub join request access was recorded policy-wise
    access_doc = await db.forcesub_access.find_one({"user_id": user_id, "chat_id": chat_id})
    if access_doc and access_doc.get("granted", False):
        return True

    return False

async def get_unsubscribed_forcesub(bot, user_id: int) -> List[Dict[str, Any]]:
    unsub = []
    forcesubs = await db.forcesub.find().to_list(length=100)
    for fs in forcesubs:
        chat_id = fs["chat_id"]
        is_member = await check_forcesub_member(bot, chat_id, user_id)
        if not is_member:
            unsub.append(fs)
    return unsub

# -----------------------------------------------------------------------------
# Bot Command Handlers
# -----------------------------------------------------------------------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    await register_user(user)

    if await is_banned(user.id):
        await update.message.reply_text("❌ You are banned from using this bot.")
        return

    args = context.args
    settings = await get_settings()

    # Handling Deep-Link Request (Payload present)
    if args and len(args) > 0:
        payload = args[0].strip()

        # Verify ForceSub
        unsub_list = await get_unsubscribed_forcesub(context.bot, user.id)
        if unsub_list:
            keyboard = []
            for idx, fs in enumerate(unsub_list, start=1):
                invite_url = fs.get("invite_link") or f"https://t.me/{str(fs['chat_id']).replace('@', '')}"
                keyboard.append([InlineKeyboardButton(f"📢 Join {fs.get('title', f'Channel {idx}')}", url=invite_url)])

            retry_url = f"https://t.me/{context.bot.username}?start={payload}"
            keyboard.append([InlineKeyboardButton("🔄 Verify / Try Again", url=retry_url)])

            msg_text = (
                "⚠️ <b>Must Join ForceSub Channels First!</b>\n\n"
                "To access your link, please join all required channels listed below and click <b>Verify</b>:"
            )
            await update.message.reply_text(
                msg_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
            return

        # Target Channel Deep Link Logic
        # Payload can be ch_<chat_id> or direct target identifier
        target_chat_id = None
        if payload.startswith("ch_"):
            raw_id = payload[3:]
            target_chat_id = int(raw_id) if raw_id.lstrip("-").isdigit() else raw_id
        else:
            # Check if payload matches a channel in database
            target_chan = await db.channels.find_one({"payload_token": payload})
            if target_chan:
                target_chat_id = target_chan["chat_id"]
            else:
                # Try finding default or specified channel
                try_id = int(payload) if payload.lstrip("-").isdigit() else payload
                target_chan = await db.channels.find_one({"chat_id": try_id})
                if target_chan:
                    target_chat_id = target_chan["chat_id"]

        if not target_chat_id:
            # Fallback to first available channel if only single channel
            chans = await db.channels.find().to_list(length=1)
            if chans:
                target_chat_id = chans[0]["chat_id"]

        if not target_chat_id:
            await update.message.reply_text("❌ Channel invalid or no destination channel configured.")
            return

        # Generate One-Time Invite Link
        try:
            req_toggle = settings.get("req_toggle", False)
            expire_date = datetime.now(timezone.utc) + timedelta(minutes=2)
            expire_timestamp = int(expire_date.timestamp())

            if req_toggle:
                # Private Join Request Link
                invite: ChatInviteLink = await context.bot.create_chat_invite_link(
                    chat_id=target_chat_id,
                    expire_date=expire_timestamp,
                    creates_join_request=True,
                    name=f"ReqLink_{user.id}"
                )
            else:
                # Single Use Direct Link
                invite: ChatInviteLink = await context.bot.create_chat_invite_link(
                    chat_id=target_chat_id,
                    expire_date=expire_timestamp,
                    member_limit=1,
                    creates_join_request=False,
                    name=f"OneTime_{user.id}"
                )

            # Store link in Database
            link_doc = {
                "link": invite.invite_link,
                "chat_id": target_chat_id,
                "user_id": user.id,
                "payload_token": payload,
                "created_at": datetime.now(timezone.utc),
                "expires_at": expire_date,
                "creates_join_request": req_toggle,
                "is_revoked": False,
                "clicks": 0
            }
            await db.links.insert_one(link_doc)

            link_msg = (
                "✅ <b>Your One-Time Invite Link is Ready!</b>\n\n"
                f"🔗 <b>Invite Link:</b> {invite.invite_link}\n\n"
                "⏱️ <i>This link is valid for 1 use and auto-expires in 2 minutes!</i>"
            )
            await update.message.reply_text(link_msg, parse_mode="HTML")

            await log_channel_msg(
                context.bot,
                f"🔑 <b>Link Generated</b>\nUser: <a href='tg://user?id={user.id}'>{user.first_name}</a> (<code>{user.id}</code>)\nTarget: <code>{target_chat_id}</code>\nType: {'Join Request' if req_toggle else 'Direct One-Time'}"
            )
            return

        except Exception as e:
            logger.error(f"Failed to generate invite link for {target_chat_id}: {e}")
            await update.message.reply_text(f"❌ Failed to generate invite link: {html.escape(str(e))}")
            return

    # Standard /start Command (No payload)
    start_text = settings.get("start_text", DEFAULT_START_TEXT)
    start_photo = settings.get("start_photo", "")
    about_label = settings.get("about_button", "ℹ️ ABOUT")
    help_label = settings.get("help_button", "❓ HELP")

    buttons = [[
        InlineKeyboardButton(about_label, callback_data="page_about"),
        InlineKeyboardButton(help_label, callback_data="page_help")
    ]]
    reply_markup = InlineKeyboardMarkup(buttons)

    if start_photo:
        try:
            await update.message.reply_photo(
                photo=start_photo,
                caption=start_text,
                reply_markup=reply_markup,
                parse_mode="HTML"
            )
            return
        except Exception as e:
            logger.warning(f"Failed sending start photo {start_photo}: {e}")

    await update.message.reply_text(
        start_text,
        reply_markup=reply_markup,
        parse_mode="HTML"
    )

# -----------------------------------------------------------------------------
# Callback Query Handler (About, Help, Back Navigation & Admin UI)
# -----------------------------------------------------------------------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    user = query.from_user
    data = query.data
    await query.answer()

    settings = await get_settings()

    if data == "page_about":
        about_text = settings.get("about_text", DEFAULT_ABOUT_TEXT)
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="page_home")]])
        if query.message.caption:
            await query.edit_message_caption(caption=about_text, reply_markup=markup, parse_mode="HTML")
        else:
            await query.edit_message_text(text=about_text, reply_markup=markup, parse_mode="HTML")

    elif data == "page_help":
        help_text = settings.get("help_text", DEFAULT_HELP_TEXT)
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="page_home")]])
        if query.message.caption:
            await query.edit_message_caption(caption=help_text, reply_markup=markup, parse_mode="HTML")
        else:
            await query.edit_message_text(text=help_text, reply_markup=markup, parse_mode="HTML")

    elif data == "page_home":
        start_text = settings.get("start_text", DEFAULT_START_TEXT)
        about_label = settings.get("about_button", "ℹ️ ABOUT")
        help_label = settings.get("help_button", "❓ HELP")
        buttons = [[
            InlineKeyboardButton(about_label, callback_data="page_about"),
            InlineKeyboardButton(help_label, callback_data="page_help")
        ]]
        markup = InlineKeyboardMarkup(buttons)
        if query.message.caption:
            await query.edit_message_caption(caption=start_text, reply_markup=markup, parse_mode="HTML")
        else:
            await query.edit_message_text(text=start_text, reply_markup=markup, parse_mode="HTML")

    elif data.startswith("admin_"):
        if not await is_admin(user.id):
            await query.answer("❌ Admin Unauthorized", show_alert=True)
            return
        await handle_admin_callback(query, context, data)

# -----------------------------------------------------------------------------
# Chat Join Request Handler
# -----------------------------------------------------------------------------
async def join_request_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req = update.chat_join_request
    if not req:
        return

    chat_id = req.chat.id
    user_id = req.from_user.id

    # Check if chat is a ForceSub channel
    fs_doc = await db.forcesub.find_one({"chat_id": chat_id})
    if fs_doc:
        # Strictly NEVER auto-approve ForceSub join requests!
        # Save request access status in forcesub_access
        await db.forcesub_access.update_one(
            {"user_id": user_id, "chat_id": chat_id},
            {
                "$set": {
                    "granted": True,
                    "requested_at": datetime.now(timezone.utc)
                }
            },
            upsert=True
        )
        logger.info(f"Recorded private ForceSub join request for User {user_id} in Chat {chat_id}. Not auto-approved.")
        return

    # Check if chat is a Destination Channel with active join request link
    link_doc = await db.links.find_one({
        "chat_id": chat_id,
        "user_id": user_id,
        "creates_join_request": True,
        "is_revoked": False
    })

    if link_doc:
        try:
            await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
            await db.links.update_one(
                {"_id": link_doc["_id"]},
                {
                    "$inc": {"clicks": 1},
                    "$set": {"is_revoked": True}
                }
            )
            logger.info(f"Auto-approved destination join request for User {user_id} in Chat {chat_id}.")

            # Revoke link for security
            try:
                await context.bot.revoke_chat_invite_link(chat_id=chat_id, invite_link=link_doc["link"])
            except Exception:
                pass

            await log_channel_msg(
                context.bot,
                f"🎉 <b>Join Request Auto-Approved</b>\nUser: {user_id}\nChat: {chat_id}"
            )
        except Exception as e:
            logger.error(f"Failed to approve join request for user {user_id}: {e}")

# -----------------------------------------------------------------------------
# Periodic Background Task: Link Expiration & Revocation
# -----------------------------------------------------------------------------
async def background_link_cleanup_loop(app: Application):
    logger.info("Starting background link cleanup task loop...")
    while True:
        try:
            now = datetime.now(timezone.utc)
            settings = await get_settings()
            if settings.get("autodelete_enabled", True):
                # Query expired links that are not yet revoked
                expired_links = await db.links.find({
                    "is_revoked": False,
                    "expires_at": {"$lte": now}
                }).to_list(length=100)

                for doc in expired_links:
                    try:
                        await app.bot.revoke_chat_invite_link(
                            chat_id=doc["chat_id"],
                            invite_link=doc["link"]
                        )
                    except Exception as e:
                        logger.debug(f"Revoke link exception for {doc['link']}: {e}")

                    await db.links.update_one(
                        {"_id": doc["_id"]},
                        {"$set": {"is_revoked": True}}
                    )

        except Exception as e:
            logger.error(f"Error in background link cleanup: {e}")

        await asyncio.sleep(30)

# -----------------------------------------------------------------------------
# Admin Dashboard (/stark) & Admin Commands
# -----------------------------------------------------------------------------
async def stark_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not await is_admin(user.id):
        await update.message.reply_text("❌ Unauthorized.")
        return

    text = "⚡ <b>STARK ADMIN CONTROL PANEL</b> ⚡\n\nSelect a management section below:"
    markup = get_admin_main_keyboard()
    await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")

def get_admin_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Destination Channels", callback_data="admin_channels_1")],
        [InlineKeyboardButton("🔒 ForceSub Channels", callback_data="admin_forcesub_1")],
        [InlineKeyboardButton("⚙️ Bot Settings & Customization", callback_data="admin_settings")],
        [InlineKeyboardButton("📊 Stats & Top Links", callback_data="admin_stats")],
        [InlineKeyboardButton("📣 Broadcast Message", callback_data="admin_broadcast_info")]
    ])

async def handle_admin_callback(query, context: ContextTypes.DEFAULT_TYPE, data: str):
    if data == "admin_home":
        text = "⚡ <b>STARK ADMIN CONTROL PANEL</b> ⚡\n\nSelect a management section below:"
        await query.edit_message_text(text, reply_markup=get_admin_main_keyboard(), parse_mode="HTML")

    elif data.startswith("admin_channels_"):
        page = int(data.split("_")[2])
        channels = await db.channels.find().to_list(length=100)

        per_page = 5
        total_pages = max(1, (len(channels) + per_page - 1) // per_page)
        start_idx = (page - 1) * per_page
        paged_chans = channels[start_idx:start_idx + per_page]

        msg = f"📢 <b>Destination Channels (Page {page}/{total_pages})</b>\n\n"
        if not channels:
            msg += "<i>No destination channels added yet. Use /addchannel to add one.</i>"
        else:
            for ch in paged_chans:
                msg += f"• <b>Title:</b> {html.escape(ch.get('title', 'Unknown'))}\n  <b>ID:</b> <code>{ch['chat_id']}</code>\n"

        buttons = []
        nav_row = []
        if page > 1:
            nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"admin_channels_{page-1}"))
        if page < total_pages:
            nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"admin_channels_{page+1}"))
        if nav_row:
            buttons.append(nav_row)
        buttons.append([InlineKeyboardButton("🔙 Back Panel", callback_data="admin_home")])

        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")

    elif data.startswith("admin_forcesub_"):
        page = int(data.split("_")[2])
        forcesubs = await db.forcesub.find().to_list(length=100)

        per_page = 5
        total_pages = max(1, (len(forcesubs) + per_page - 1) // per_page)
        start_idx = (page - 1) * per_page
        paged_fs = forcesubs[start_idx:start_idx + per_page]

        msg = f"🔒 <b>ForceSub Channels (Page {page}/{total_pages})</b>\n\n"
        if not forcesubs:
            msg += "<i>No ForceSub channels added yet. Use /addfs to add one.</i>"
        else:
            for fs in paged_fs:
                msg += f"• <b>Title:</b> {html.escape(fs.get('title', 'Unknown'))}\n  <b>ID:</b> <code>{fs['chat_id']}</code>\n"

        buttons = []
        nav_row = []
        if page > 1:
            nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"admin_forcesub_{page-1}"))
        if page < total_pages:
            nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"admin_forcesub_{page+1}"))
        if nav_row:
            buttons.append(nav_row)
        buttons.append([InlineKeyboardButton("🔙 Back Panel", callback_data="admin_home")])

        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")

    elif data == "admin_settings":
        s = await get_settings()
        msg = (
            "⚙️ <b>Bot Settings Configuration</b>\n\n"
            f"<b>Join Request Mode:</b> {'🟢 ENABLED' if s.get('req_toggle') else '🔴 DISABLED'}\n"
            f"<b>Auto-Delete Revocation:</b> {'🟢 ENABLED' if s.get('autodelete_enabled') else '🔴 DISABLED'}\n"
            f"<b>Start Photo:</b> {s.get('start_photo') or 'None'}\n"
            f"<b>About Button:</b> {s.get('about_button', 'ℹ️ ABOUT')}\n"
            f"<b>Help Button:</b> {s.get('help_button', '❓ HELP')}\n\n"
            "<i>Commands to modify settings:</i>\n"
            "• /setstart - Set photo, welcome text, about, help & button labels\n"
            "• /togglereq - Toggle join request mode\n"
            "• /autodelete - Toggle auto-delete revocation"
        )
        buttons = [
            [InlineKeyboardButton("🔀 Toggle Join Req", callback_data="admin_toggle_req"),
             InlineKeyboardButton("🗑️ Toggle AutoDelete", callback_data="admin_toggle_autodelete")],
            [InlineKeyboardButton("🔙 Back Panel", callback_data="admin_home")]
        ]
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")

    elif data == "admin_toggle_req":
        s = await get_settings()
        new_val = not s.get("req_toggle", False)
        await db.settings.update_one({"_id": "global"}, {"$set": {"req_toggle": new_val}})
        await handle_admin_callback(query, context, "admin_settings")

    elif data == "admin_toggle_autodelete":
        s = await get_settings()
        new_val = not s.get("autodelete_enabled", True)
        await db.settings.update_one({"_id": "global"}, {"$set": {"autodelete_enabled": new_val}})
        await handle_admin_callback(query, context, "admin_settings")

    elif data == "admin_stats":
        stats_text = await generate_stats_text()
        buttons = [[InlineKeyboardButton("🔙 Back Panel", callback_data="admin_home")]]
        await query.edit_message_text(stats_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")

    elif data == "admin_broadcast_info":
        msg = (
            "📣 <b>Broadcast Manager</b>\n\n"
            "Reply to any message with <code>/broadcast</code> or use:\n"
            "<code>/broadcast &lt;your text message&gt;</code>\n\n"
            "Features:\n"
            "• Automatic rate-limiting\n"
            "• RetryAfter handling\n"
            "• Automatic cleanup of blocked users"
        )
        buttons = [[InlineKeyboardButton("🔙 Back Panel", callback_data="admin_home")]]
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")

# -----------------------------------------------------------------------------
# Channel & ForceSub Commands
# -----------------------------------------------------------------------------
async def addchannel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("Usage: <code>/addchannel &lt;chat_id or @username&gt;</code>", parse_mode="HTML")
        return

    raw_chat = context.args[0].strip()
    chat_id = int(raw_chat) if raw_chat.lstrip("-").isdigit() else raw_chat

    try:
        chat = await context.bot.get_chat(chat_id)
        payload_token = f"ch_{chat.id}"
        doc = {
            "chat_id": chat.id,
            "title": chat.title or str(chat.id),
            "payload_token": payload_token,
            "added_at": datetime.now(timezone.utc)
        }
        await db.channels.update_one({"chat_id": chat.id}, {"$set": doc}, upsert=True)
        await update.message.reply_text(
            f"✅ Destination Channel Added!\n\n"
            f"• <b>Title:</b> {html.escape(chat.title or '')}\n"
            f"• <b>ID:</b> <code>{chat.id}</code>\n"
            f"• <b>Payload:</b> <code>{payload_token}</code>\n"
            f"• <b>Bot Start Link:</b> <code>https://t.me/{context.bot.username}?start={payload_token}</code>",
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to fetch/add channel: {html.escape(str(e))}")

async def removechannel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("Usage: <code>/removechannel &lt;chat_id&gt;</code>", parse_mode="HTML")
        return

    raw_id = context.args[0].strip()
    chat_id = int(raw_id) if raw_id.lstrip("-").isdigit() else raw_id

    res = await db.channels.delete_one({"chat_id": chat_id})
    if res.deleted_count > 0:
        await update.message.reply_text(f"✅ Removed channel <code>{chat_id}</code>.", parse_mode="HTML")
    else:
        await update.message.reply_text(f"❌ Channel <code>{chat_id}</code> not found.", parse_mode="HTML")

async def listchannel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return

    channels = await db.channels.find().to_list(length=100)
    if not channels:
        await update.message.reply_text("No destination channels registered.")
        return

    msg = "📢 <b>Destination Channels:</b>\n\n"
    for ch in channels:
        msg += f"• <b>Title:</b> {html.escape(ch.get('title', ''))}\n  <b>ID:</b> <code>{ch['chat_id']}</code>\n"
    await update.message.reply_text(msg, parse_mode="HTML")

async def addfs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("Usage: <code>/addfs &lt;chat_id or @username&gt; [invite_link]</code>", parse_mode="HTML")
        return

    raw_chat = context.args[0].strip()
    chat_id = int(raw_chat) if raw_chat.lstrip("-").isdigit() else raw_chat
    custom_link = context.args[1].strip() if len(context.args) > 1 else ""

    try:
        chat = await context.bot.get_chat(chat_id)
        invite_link = custom_link or chat.invite_link or f"https://t.me/{str(chat.username)}" if chat.username else custom_link

        doc = {
            "chat_id": chat.id,
            "title": chat.title or str(chat.id),
            "invite_link": invite_link,
            "added_at": datetime.now(timezone.utc)
        }
        await db.forcesub.update_one({"chat_id": chat.id}, {"$set": doc}, upsert=True)
        await update.message.reply_text(
            f"✅ ForceSub Channel Added!\n\n"
            f"• <b>Title:</b> {html.escape(chat.title or '')}\n"
            f"• <b>ID:</b> <code>{chat.id}</code>\n"
            f"• <b>Invite Link:</b> {invite_link or 'None'}",
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to fetch/add ForceSub channel: {html.escape(str(e))}")

async def rmfs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("Usage: <code>/rmfs &lt;chat_id&gt;</code>", parse_mode="HTML")
        return

    raw_id = context.args[0].strip()
    chat_id = int(raw_id) if raw_id.lstrip("-").isdigit() else raw_id

    res = await db.forcesub.delete_one({"chat_id": chat_id})
    if res.deleted_count > 0:
        await update.message.reply_text(f"✅ Removed ForceSub channel <code>{chat_id}</code>.", parse_mode="HTML")
    else:
        await update.message.reply_text(f"❌ ForceSub channel <code>{chat_id}</code> not found.", parse_mode="HTML")

async def listfs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return

    forcesubs = await db.forcesub.find().to_list(length=100)
    if not forcesubs:
        await update.message.reply_text("No ForceSub channels registered.")
        return

    msg = "🔒 <b>ForceSub Channels:</b>\n\n"
    for fs in forcesubs:
        msg += f"• <b>Title:</b> {html.escape(fs.get('title', ''))}\n  <b>ID:</b> <code>{fs['chat_id']}</code>\n"
    await update.message.reply_text(msg, parse_mode="HTML")

# -----------------------------------------------------------------------------
# User Ban / Unban Commands
# -----------------------------------------------------------------------------
async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("Usage: <code>/ban &lt;user_id&gt; [reason]</code>", parse_mode="HTML")
        return

    user_id = int(context.args[0]) if context.args[0].isdigit() else None
    if not user_id:
        await update.message.reply_text("❌ Invalid user_id.")
        return

    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "No reason specified"
    await db.bans.update_one(
        {"user_id": user_id},
        {"$set": {"reason": reason, "banned_at": datetime.now(timezone.utc)}},
        upsert=True
    )
    await update.message.reply_text(f"🚫 User <code>{user_id}</code> has been banned.", parse_mode="HTML")

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("Usage: <code>/unban &lt;user_id&gt;</code>", parse_mode="HTML")
        return

    user_id = int(context.args[0]) if context.args[0].isdigit() else None
    if not user_id:
        await update.message.reply_text("❌ Invalid user_id.")
        return

    res = await db.bans.delete_one({"user_id": user_id})
    if res.deleted_count > 0:
        await update.message.reply_text(f"✅ User <code>{user_id}</code> unbanned.", parse_mode="HTML")
    else:
        await update.message.reply_text(f"❌ User <code>{user_id}</code> was not banned.", parse_mode="HTML")

# -----------------------------------------------------------------------------
# Statistics, Settings & Top Links Commands
# -----------------------------------------------------------------------------
async def generate_stats_text() -> str:
    total_users = await db.users.count_documents({})
    total_links = await db.links.count_documents({})
    active_links = await db.links.count_documents({"is_revoked": False})
    total_channels = await db.channels.count_documents({})
    total_forcesub = await db.forcesub.count_documents({})
    total_bans = await db.bans.count_documents({})

    return (
        "📊 <b>Advanced System Statistics</b>\n\n"
        f"• <b>Total Registered Users:</b> {total_users}\n"
        f"• <b>Total Generated Links:</b> {total_links}\n"
        f"• <b>Active Non-Expired Links:</b> {active_links}\n"
        f"• <b>Destination Channels:</b> {total_channels}\n"
        f"• <b>ForceSub Channels:</b> {total_forcesub}\n"
        f"• <b>Banned Users:</b> {total_bans}"
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return
    text = await generate_stats_text()
    await update.message.reply_text(text, parse_mode="HTML")

async def toplinks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return

    top_docs = await db.links.find().sort("clicks", -1).limit(10).to_list(length=10)
    if not top_docs:
        await update.message.reply_text("No link statistics available yet.")
        return

    msg = "🏆 <b>Top Generated Links by Usage/Clicks:</b>\n\n"
    for idx, doc in enumerate(top_docs, start=1):
        msg += (
            f"<b>{idx}.</b> <code>{doc['link']}</code>\n"
            f"   • User: <code>{doc['user_id']}</code> | Clicks/Joins: <b>{doc.get('clicks', 0)}</b> | Revoked: {doc.get('is_revoked')}\n"
        )
    await update.message.reply_text(msg, parse_mode="HTML")

async def togglereq_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return

    s = await get_settings()
    new_val = not s.get("req_toggle", False)
    await db.settings.update_one({"_id": "global"}, {"$set": {"req_toggle": new_val}})
    state = "ENABLED (Private Join Request links)" if new_val else "DISABLED (Direct one-time invite links)"
    await update.message.reply_text(f"🔀 Join Request mode is now <b>{state}</b>.", parse_mode="HTML")

async def autodelete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return

    s = await get_settings()
    new_val = not s.get("autodelete_enabled", True)
    await db.settings.update_one({"_id": "global"}, {"$set": {"autodelete_enabled": new_val}})
    state = "ENABLED" if new_val else "DISABLED"
    await update.message.reply_text(f"🗑️ Automatic Link Revocation is now <b>{state}</b>.", parse_mode="HTML")

async def setstart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return

    # /setstart <photo_url> | <start_text> | <about_text> | <help_text> | [about_btn] | [help_btn]
    args_text = " ".join(context.args) if context.args else ""
    if not args_text or "|" not in args_text:
        msg = (
            "⚙️ <b>Set Start Configuration Usage:</b>\n\n"
            "<code>/setstart photo_url | Welcome Text | About Text | Help Text | [About Btn] | [Help Btn]</code>\n\n"
            "<i>Leave photo_url blank if no photo desired. Optional 5th & 6th parameters set button labels.</i>\n\n"
            "Example:\n"
            "<code>/setstart https://example.com/pic.jpg | Welcome! | About us | Help info | ℹ️ ABOUT | ❓ HELP</code>"
        )
        await update.message.reply_text(msg, parse_mode="HTML")
        return

    parts = [p.strip() for p in args_text.split("|")]
    photo_url = parts[0] if len(parts) > 0 else ""
    start_text = parts[1] if len(parts) > 1 else DEFAULT_START_TEXT
    about_text = parts[2] if len(parts) > 2 else DEFAULT_ABOUT_TEXT
    help_text = parts[3] if len(parts) > 3 else DEFAULT_HELP_TEXT
    about_btn = parts[4] if len(parts) > 4 and parts[4] else "ℹ️ ABOUT"
    help_btn = parts[5] if len(parts) > 5 and parts[5] else "❓ HELP"

    await db.settings.update_one(
        {"_id": "global"},
        {
            "$set": {
                "start_photo": photo_url,
                "start_text": start_text,
                "about_text": about_text,
                "help_text": help_text,
                "about_button": about_btn,
                "help_button": help_btn
            }
        },
        upsert=True
    )
    await update.message.reply_text("✅ Start configuration and button labels updated successfully!", parse_mode="HTML")

# -----------------------------------------------------------------------------
# Broadcast Command
# -----------------------------------------------------------------------------
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return

    target_msg = update.message.reply_to_message
    broadcast_text = " ".join(context.args) if context.args else ""

    if not target_msg and not broadcast_text:
        await update.message.reply_text("Please reply to a message with /broadcast or specify text to broadcast.")
        return

    users = await db.users.find().to_list(length=10000)
    total = len(users)
    status_msg = await update.message.reply_text(f"⏳ Broadcasting to {total} users...")

    successful = 0
    blocked = 0
    failed = 0

    for idx, u in enumerate(users):
        uid = u["user_id"]
        try:
            if target_msg:
                await target_msg.copy(chat_id=uid)
            else:
                await context.bot.send_message(chat_id=uid, text=broadcast_text, parse_mode="HTML")
            successful += 1
        except Forbidden:
            blocked += 1
            # Cleanup blocked user
            await db.users.delete_one({"user_id": uid})
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                if target_msg:
                    await target_msg.copy(chat_id=uid)
                else:
                    await context.bot.send_message(chat_id=uid, text=broadcast_text, parse_mode="HTML")
                successful += 1
            except Exception:
                failed += 1
        except Exception as e:
            logger.warning(f"Broadcast error for {uid}: {e}")
            failed += 1

        # Rate limiting sleep
        await asyncio.sleep(0.05)

    await status_msg.edit_text(
        f"✅ <b>Broadcast Complete!</b>\n\n"
        f"• Total Users: {total}\n"
        f"• Delivered: {successful}\n"
        f"• Blocked & Cleaned: {blocked}\n"
        f"• Failed: {failed}",
        parse_mode="HTML"
    )

# -----------------------------------------------------------------------------
# Bot Initialization & Main Entrypoint
# -----------------------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        print("Error: BOT_TOKEN environment variable not set.")
        sys.exit(1)

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Handlers Registration
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("stark", stark_command))
    app.add_handler(CommandHandler("addchannel", addchannel_command))
    app.add_handler(CommandHandler("removechannel", removechannel_command))
    app.add_handler(CommandHandler("listchannel", listchannel_command))
    app.add_handler(CommandHandler("addfs", addfs_command))
    app.add_handler(CommandHandler("rmfs", rmfs_command))
    app.add_handler(CommandHandler("listfs", listfs_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("toplinks", toplinks_command))
    app.add_handler(CommandHandler("togglereq", togglereq_command))
    app.add_handler(CommandHandler("autodelete", autodelete_command))
    app.add_handler(CommandHandler("setstart", setstart_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(ChatJoinRequestHandler(join_request_handler))

    logger.info("Bot starting polling...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
