FROM python:3.11-slim

# Install Java JRE (for JADX) and Git
RUN apt-get update && apt-get install -y \
    default-jre \
    wget \
    unzip \
    git \
    && rm -rf /var/lib/apt/lists/*

# Download and set up JADX
RUN wget https://github.com/skylot/jadx/releases/download/v1.5.0/jadx-1.5.0.zip -O /tmp/jadx.zip \
    && unzip /tmp/jadx.zip -d /opt/jadx \
    && ln -s /opt/jadx/bin/jadx /usr/local/bin/jadx \
    && rm /tmp/jadx.zip

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
