FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --create-home --uid 1000 bot && mkdir -p /app/data && chown bot /app/data

USER bot
# config.yaml and .env are mounted at runtime; data/ holds SQLite + the Telethon session
VOLUME ["/app/data"]
ENTRYPOINT ["python", "-m", "signal_copier"]
CMD ["run"]
