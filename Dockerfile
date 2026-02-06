FROM python:3.11-slim

# Chrome for Selenium (modern GPG key approach — apt-key is removed in newer Debian)
RUN apt-get update && apt-get install -y \
    wget \
    gnupg2 \
    unzip \
    curl \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    xdg-utils \
    && mkdir -p /etc/apt/keyrings \
    && wget -q -O /etc/apt/keyrings/google-chrome.gpg https://dl.google.com/linux/linux_signing_key.pub \
    && gpg --dearmor < /etc/apt/keyrings/google-chrome.gpg > /etc/apt/keyrings/google-chrome-keyring.gpg 2>/dev/null \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome-keyring.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# ChromeDriver — match exact Chrome version
RUN CHROME_VERSION=$(google-chrome --version | grep -oP '\d+\.\d+\.\d+\.\d+') \
    && echo "Chrome version: $CHROME_VERSION" \
    && CHROME_MAJOR=$(echo $CHROME_VERSION | cut -d. -f1) \
    && echo "Chrome major: $CHROME_MAJOR" \
    && CHROMEDRIVER_URL=$(curl -s "https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json" \
       | python3 -c "import sys,json; d=json.load(sys.stdin); v='$CHROME_VERSION'; matches=[ver for ver in d['versions'] if ver['version'].startswith('$CHROME_MAJOR.')]; ver=next((m for m in matches if m['version']==v), matches[-1] if matches else None); print([dl['url'] for dl in ver['downloads'].get('chromedriver',[]) if dl['platform']=='linux64'][0]) if ver else exit(1)") \
    && echo "ChromeDriver URL: $CHROMEDRIVER_URL" \
    && wget -q "$CHROMEDRIVER_URL" -O /tmp/chromedriver.zip \
    && unzip /tmp/chromedriver.zip -d /tmp/ \
    && mv /tmp/chromedriver-linux64/chromedriver /usr/local/bin/ \
    && chmod +x /usr/local/bin/chromedriver \
    && rm -rf /tmp/chromedriver* \
    && echo "Installed ChromeDriver for Chrome $CHROME_VERSION"

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY . .

# Port
EXPOSE 8000

# Run
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
