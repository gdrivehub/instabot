FROM python:3.12-slim

# ffmpeg for video post-processing
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Non-root user for security
RUN useradd -m botuser
USER botuser

# Health-check port
EXPOSE 8000

CMD ["python", "-u", "bot.py"]
