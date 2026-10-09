FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/data/digest.db

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY *.py .

RUN useradd --create-home bot && mkdir -p /data && chown bot /data
USER bot
VOLUME ["/data"]

CMD ["python", "bot.py"]
