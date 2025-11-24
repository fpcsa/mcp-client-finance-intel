FROM python:3.12-slim

WORKDIR /app

# Install dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy only the bot entrypoint (and any runtime resources it imports)
COPY bot.py .

# Default command runs the Telegram bot
CMD ["python", "bot.py"]
