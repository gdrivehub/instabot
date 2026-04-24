# ── Build stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim AS base

# System deps (ffmpeg lets python-telegram-bot handle large video files)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY bot.py .

# ── Runtime ──────────────────────────────────────────────────────────────────
# Non-root user for security
RUN useradd -m botuser
USER botuser

CMD ["python", "-u", "bot.py"]
