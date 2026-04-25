# 📸 InstaGrab Telegram Bot

A Telegram bot that downloads Instagram **posts**, **reels**, **IGTV**, and **stories** (including carousels) and sends them back with the original caption.

Built with `python-telegram-bot` + `yt-dlp`. Deployable on Koyeb with one push.

---

## ✨ Features

| Feature | Details |
|---|---|
| 📸 Posts | Single photos, carousels (up to 10 per group) |
| 🎬 Reels | Full video with caption |
| 📺 IGTV | Long-form video |
| 📖 Stories | Photos & videos |
| 📝 Caption | Original Instagram caption included |
| 🔒 Private content | Supported via cookies file |
| ☁️ Deploy-ready | Auto-deploys on Koyeb via GitHub push |

---

## 🚀 Quick Start (Local)

### 1. Prerequisites

- Python 3.12+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- `ffmpeg` installed (`brew install ffmpeg` / `apt install ffmpeg`)

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
# Edit .env — at minimum set BOT_TOKEN
```

### 4. Run

```bash
export $(grep -v '^#' .env | xargs)
python bot.py
```

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

1. Log in to [koyeb.com](https://www.koyeb.com) → **Create Service**
2. Choose **GitHub** as source → select your repo
3. Koyeb auto-detects the `Dockerfile`
4. Set **Service type = Web Service** (NOT Worker) so the health check on port 8000 passes
5. Set **Port = 8000**
6. Under **Environment variables**, add:

| Name | Value | Secret? |
|---|---|---|
| `BOT_TOKEN` | your bot token | ✅ Yes |
| `IG_COOKIES_FILE` | `/app/cookies.txt` | No (if using cookies) |

7. Click **Deploy** — every `git push` to `main` auto-redeploys ✅

---

## 🍪 Instagram Cookies (for Private / Rate-limited Content)

Instagram blocks anonymous requests aggressively. For reliable downloads — especially for private posts or when you hit rate limits — provide a cookies file:

### How to export cookies

1. Install the **"Get cookies.txt LOCALLY"** extension in Chrome/Firefox
2. Log in to Instagram in your browser
3. Visit `https://www.instagram.com`
4. Click the extension → **Export** → save as `cookies.txt`
5. Add `cookies.txt` to your project root (it is gitignored)

### Using cookies on Koyeb

Since Koyeb doesn't have persistent storage on the free tier, the easiest approach is to embed the cookies content as an environment variable and write it to disk at startup.

Add this to `bot.py` right after the config section:

```python
# Write cookies from env var to file (optional, for Koyeb)
IG_COOKIES_CONTENT = os.getenv("IG_COOKIES_CONTENT", "")
if IG_COOKIES_CONTENT and not os.path.isfile("/app/cookies.txt"):
    with open("/app/cookies.txt", "w") as f:
        f.write(IG_COOKIES_CONTENT)
    os.environ["IG_COOKIES_FILE"] = "/app/cookies.txt"
```

Then set `IG_COOKIES_CONTENT` as a Secret env var in Koyeb containing the full contents of your `cookies.txt`.

> **Tip:** Use a secondary Instagram account for cookies to protect your main account.

---

## 🤖 Bot Commands

| Command / Input | Action |
|---|---|
| `/start` | Welcome message & usage instructions |
| Any Instagram URL | Download and send the media + caption |

### Supported URL formats

```
https://www.instagram.com/p/<shortcode>/
https://www.instagram.com/reel/<shortcode>/
https://www.instagram.com/reels/<shortcode>/
https://www.instagram.com/tv/<shortcode>/
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
├── .gitignore        # Excludes .env, cookies, pycache
└── README.md
```

---

## ⚙️ Environment Variables

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | ✅ | Telegram bot token from @BotFather |
| `PORT` | ❌ | Health-check HTTP port (default: `8000`) |
| `IG_COOKIES_FILE` | ❌ | Path to Netscape cookies file for private/authenticated content |

---

## 📦 Dependencies

| Package | Purpose |
|---|---|
| `python-telegram-bot` | Async Telegram Bot API wrapper |
| `yt-dlp` | Robust Instagram (and 1000+ sites) media downloader |

---

## 📄 License

MIT — see [LICENSE](LICENSE) for details.
