FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Kolkata
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY sensexpro ./sensexpro
COPY config ./config
COPY tests ./tests
RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /app/data /app/logs && chown -R bot:bot /app/data /app/logs
USER bot

# PAPER unless data/mode.json says LIVE (Telegram /mode live + /confirm) AND LIVE_TRADING_CONFIRM=YES
CMD ["python", "main.py", "--auto"]
