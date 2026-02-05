FROM python:3.11-slim

# Chrome for Selenium
RUN apt-get update && apt-get install -y \
    wget \
    gnupg2 \
    unzip \
    curl \
    && wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add - \
    && echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# ChromeDriver
RUN CHROME_VERSION=$(google-chrome --version | grep -oP '\d+\.\d+\.\d+') \
    && wget -q "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VERSION}.0/linux64/chromedriver-linux64.zip" -O /tmp/chromedriver.zip \
    || wget -q "https://chromedriver.storage.googleapis.com/$(curl -s https://chromedriver.storage.googleapis.com/LATEST_RELEASE)/chromedriver_linux64.zip" -O /tmp/chromedriver.zip \
    && unzip /tmp/chromedriver.zip -d /tmp/ \
    && mv /tmp/chromedriver*/chromedriver /usr/local/bin/ 2>/dev/null || mv /tmp/chromedriver /usr/local/bin/ \
    && chmod +x /usr/local/bin/chromedriver \
    && rm -rf /tmp/chromedriver*

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
