FROM python:3.10-slim
 
WORKDIR /app
 
# Install system dependencies including webdriver-manager requirements
RUN apt-get update && apt-get install -y \
    cron \
    gcc \
    postgresql-client \
    python3-dev \
    libpq-dev \
    wget \
    gnupg \
    ca-certificates \
    fonts-liberation \
    fonts-unifont \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libxss1 \
    libxtst6 \
    xdg-utils \
    libu2f-udev \
    libvulkan1 \
    && rm -rf /var/lib/apt/lists/*
 
# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
 
# Copy project files
COPY . .
 
EXPOSE 8001
 
# Dev: use runserver
CMD ["./entrypoint.sh"]