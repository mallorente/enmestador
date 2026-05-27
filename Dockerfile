FROM python:3.11-slim-bookworm

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install Playwright/Patchright Chromium system dependencies + VNC tools for auth
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libx11-6 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    libwayland-client0 \
    xvfb \
    tigervnc-standalone-server \
    novnc \
    websockify \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Patchright Chromium browser
RUN patchright install chromium

# Copy application code
COPY *.py ./
COPY scrapers/ scrapers/
COPY extractors/ extractors/
COPY auth/ auth/
COPY pipeline/ pipeline/
COPY scripts/ scripts/

# Create volume mount points
RUN mkdir -p /app/volumes/user_data /app/volumes/state /app/volumes/llm_wiki_seed/Bookmarks/bookmarks

# Default command for one-shot runs. Override via docker-compose `command:`.
CMD ["python", "main.py"]
