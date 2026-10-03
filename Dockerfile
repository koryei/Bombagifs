FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends libcairo2 libffi8 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py status.config ./
RUN useradd --system --uid 10001 --create-home bot \
    && chown -R bot:bot /app \
    && chmod 750 /app \
    && chmod 640 /app/main.py /app/status.config /app/requirements.txt
USER bot

CMD ["python", "main.py"]
