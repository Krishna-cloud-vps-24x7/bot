# Dockerfile for the Discord VPS Manager bot
FROM python:3.11-slim

LABEL maintainer="you@example.com"

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    git \
    curl \
    ca-certificates \
    libffi-dev \
    libssl-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy app
COPY bot.py /app/bot.py
COPY .env.template /app/.env.template
COPY README.md /app/README.md

# Create data dir
RUN mkdir -p /app/data
VOLUME ["/app/data"]

ENV PYTHONUNBUFFERED=1

# Default command
CMD ["python", "bot.py"]
