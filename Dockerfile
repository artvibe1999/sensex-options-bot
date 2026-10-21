FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Kolkata
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /app/data /app/logs && chown -R bot:bot /app
USER bot

CMD ["python", "main.py", "--paper"]
