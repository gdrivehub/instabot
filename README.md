# 📸 InstaGrab Telegram Bot

A Telegram bot that downloads Instagram **posts**, **reels**, and **stories** (including carousels) and sends them back to the user along with the original caption.

---

## ✨ Features

| Feature | Details |
|---|---|
| 📸 Posts | Single photos, carousels (up to 10 per group) |
| 🎬 Reels | Full video with caption |
| 📖 Stories | Photos & videos |
| 📝 Caption | Original Instagram caption included |
| 🔒 Private content | Supported when IG credentials are provided |
| ☁️ Deploy-ready | One-command deploy on Koyeb |

---

## 🚀 Quick Start (Local)

### 1. Prerequisites

- Python 3.12+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

### 2. Clone & install

```bash
git clone https://github.com/<YOUR_USERNAME>/<YOUR_REPO>.git
cd <YOUR_REPO>

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env and fill in BOT_TOKEN (and optionally IG_USERNAME / IG_PASSWORD)
```

### 4. Run

```bash
export $(grep -v '^#' .env | xargs)   # load env vars
python bot.py
```

Open Telegram, find your bot, and send `/start`.

---

## ☁️ Deploy on Koyeb

### Step 1 — Push to GitHub

```bash
git init
git add .
git commit -m "feat: initial bot"
git remote add origin https://github.com/<YOUR_USERNAME>/<YOUR_REPO>.git
git push -u origin main
```

### Step 2 — Create a Koyeb service

1. Log in to [koyeb.com](https://www.koyeb.com) and click **Create Service**.
2. Choose **GitHub** as the source and select your repo.
3. Koyeb auto-detects the `Dockerfile`. Make sure **Service type** is set to **Worker** (no HTTP port).
4. Under **Environment variables**, add:
   | Name | Value | Secret? |
   |---|---|---|
   | `BOT_TOKEN` | your bot token | ✅ Yes |
   | `IG_USERNAME` | your IG username | ✅ Yes (optional) |
   | `IG_PASSWORD` | your IG password | ✅ Yes (optional) |
5. Click **Deploy**.

Koyeb will build the Docker image and start the bot. Every `git push` to `main` triggers an automatic redeploy.

---

## 🤖 Bot Commands

| Command | Description |
|---|---|
| `/start` | Welcome message & usage instructions |
| *(any Instagram URL)* | Download and send the media |

### Supported URL formats

```
https://www.instagram.com/p/<shortcode>/
https://www.instagram.com/reel/<shortcode>/
https://www.instagram.com/reels/<shortcode>/
https://www.instagram.com/stories/<username>/<story_id>/
```

---

## 🏗️ Project Structure

```
.
├── bot.py            # Main bot logic
├── requirements.txt  # Python dependencies
├── Dockerfile        # Container definition
├── koyeb.yaml        # Koyeb declarative config (optional)
├── .env.example      # Environment variable template
└── .gitignore
```

---

## ⚙️ Environment Variables

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | ✅ | Telegram bot token from @BotFather |
| `IG_USERNAME` | ❌ | Instagram username (for private/rate-limited content) |
| `IG_PASSWORD` | ❌ | Instagram password |

---

## 🔒 Instagram Rate Limits & Private Content

Instagram's public API is rate-limited for anonymous access. To reliably download:

- **Public content** — works without credentials for low-to-moderate usage.
- **Private content** — requires `IG_USERNAME` + `IG_PASSWORD` set in environment variables.
- **Stories** — requires credentials because stories require a logged-in session.

> **Tip:** Use a secondary Instagram account for the bot credentials to protect your main account.

---

## 📦 Dependencies

| Package | Purpose |
|---|---|
| `python-telegram-bot` | Async Telegram Bot API wrapper |
| `instaloader` | Instagram media downloader |

---

## 📄 License

MIT — see [LICENSE](LICENSE) for details.
