# Link Provider Telegram Bot (Python 3.11+ & MongoDB)

An advanced, single-file Telegram link-provider bot written in Python 3.11+ using `python-telegram-bot` (v20+) and MongoDB (`motor`).

---

## Technical Specifications & Features

- **Single-File Architecture (`bot.py`)**: Asynchronous, clean, and scalable architecture.
- **Dynamic Welcome & Pages**: Custom `/start` handler supporting configurable welcome photo, HTML start message, and inline `ABOUT` / `HELP` buttons on a single row.
- **Admin Configuration (`/setstart`)**: Custom photo, start text, about page, help page, and button labels configurable directly from Telegram.
- **Centralized ForceSub**: Multi-channel public and private ForceSub support with user verification checks. Private join request access is tracked bot-side without auto-approving ForceSub.
- **One-Time Invite Links**: Generates links with 2-minute expiration (`expire_date`) and single-use restriction (`member_limit=1` or `creates_join_request=True`).
- **Private Join Requests & Auto-Approval (`/togglereq`)**: Optional join-request mode where destination join requests are automatically approved for generated one-time links.
- **Background Cleanup Worker**: A single background task running periodically to query and revoke expired invite links from Telegram and MongoDB.
- **Admin Panel (`/stark`)**: Paginated inline keyboard UI for channel management, ForceSub management, system toggles, stats, and broadcast.
- **Advanced Statistics & Analytics (`/stats`, `/toplinks`)**: Live metrics for total/active links, registered users, channels, bans, and link usage click tracking.
- **Broadcast System (`/broadcast`)**: Asynchronous broadcast tool with built-in rate-limiting delay, `RetryAfter` exception handling, and automatic removal of blocked/deactivated users.
- **Security & Authorization**: Strict admin checks, HTML escaping, MongoDB indexing, error handling, and environment-based configuration.

---

## Environment Variables

Configure the following environment variables (e.g. in `.env` file or VPS systemd service):

| Variable Name | Required | Description | Example |
|---|---|---|---|
| `BOT_TOKEN` | **Yes** | Telegram Bot API token from `@BotFather` | `123456789:ABCdefGHIjklMNOpqrsTUVwxyZ` |
| `MONGO_URI` | **Yes** | Connection string for MongoDB database | `mongodb://localhost:27017` or `mongodb+srv://...` |
| `MONGO_DB_NAME` | **Yes** | MongoDB Database name | `link_provider_bot` |
| `ADMIN_IDS` | **Yes** | Comma-separated Telegram User IDs of authorized admins | `123456789,987654321` |
| `LOG_CHANNEL_ID` | Optional | Telegram Channel ID for administrative logs | `-1001234567890` |

---

## Telegram Bot & Channel Permissions Setup

1. **Add Bot as Administrator** in all target **Destination Channels** and **ForceSub Channels**.
2. Grant the bot the following Telegram channel administrator rights:
   - **Add Members** (Required for `createChatInviteLink` and `approveChatJoinRequest`)
   - **Manage Chat / Invite Links** (Required to generate and revoke invite links)
3. Ensure the bot is running before sending `/start` or admin setup commands.

---

## Installation & Deployment Guide (Debian / Ubuntu VPS)

### Step 1: Update System Packages & Install Dependencies
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install python3 python3-pip python3-venv mongodb -y
```

### Step 2: Clone or Setup Repository
```bash
mkdir -p /opt/linkprovider
cd /opt/linkprovider
# Paste bot.py and README.md into this directory
```

### Step 3: Create Virtual Environment & Install Requirements
```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install python-telegram-bot pymongo motor dnspython
```

### Step 4: Configure Systemd Service
Create a systemd unit file `/etc/systemd/system/linkprovider.service`:
```ini
[Unit]
Description=Link Provider Telegram Bot
After=network.target mongodb.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/linkprovider
ExecStart=/opt/linkprovider/venv/bin/python3 /opt/linkprovider/bot.py
Restart=always
RestartSec=5

Environment="BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyZ"
Environment="MONGO_URI=mongodb://localhost:27017"
Environment="MONGO_DB_NAME=link_provider_bot"
Environment="ADMIN_IDS=123456789"
Environment="LOG_CHANNEL_ID=-1001234567890"

[Install]
WantedBy=multi-user.target
```

### Step 5: Start & Enable Service
```bash
sudo systemctl daemon-reload
sudo systemctl start linkprovider
sudo systemctl enable linkprovider
sudo systemctl status linkprovider
```

---

## Bot Commands Reference

| Command | Permission | Description |
|---|---|---|
| `/start` | Public | Bot welcome menu, deep-link processor, and ForceSub check |
| `/stark` | Admin | Main Admin Control Panel with interactive inline GUI |
| `/setstart` | Admin | Set welcome photo, start text, about text, and help text |
| `/addchannel` | Admin | Add destination channel for invite link generation |
| `/removechannel` | Admin | Remove destination channel |
| `/listchannel` | Admin | List all registered destination channels |
| `/addfs` | Admin | Add ForceSub channel |
| `/rmfs` | Admin | Remove ForceSub channel |
| `/listfs` | Admin | List all registered ForceSub channels |
| `/ban` | Admin | Ban user from using the bot |
| `/unban` | Admin | Unban user |
| `/stats` | Admin | View system statistics and user metrics |
| `/toplinks` | Admin | View top generated links sorted by clicks/joins |
| `/togglereq` | Admin | Toggle private join request mode on/off |
| `/autodelete` | Admin | Toggle automatic link revocation background task |
| `/broadcast` | Admin | Broadcast message or forwarded post to all users |
