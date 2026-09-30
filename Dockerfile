FROM python:3.12-slim

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Install system dependencies and the Stable Chrome available at build time.
# hadolint ignore=DL3008  # Chrome's rolling apt repository and Debian helper packages do not retain immutable versions.
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg \
    ca-certificates \
    unzip \
    fonts-ipafont-gothic \
    && wget -q -O - https://dl.google.com/linux/linux_signing_key.pub \
       | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] \
       https://dl.google.com/linux/chrome/deb/ stable main" \
       > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && chrome_version="$(google-chrome --product-version)" \
    && [[ "$chrome_version" =~ ^[0-9]+([.][0-9]+){3}$ ]] \
    && chrome_build="${chrome_version%.*}" \
    && chromedriver_version="$(wget -q -O - "https://googlechromelabs.github.io/chrome-for-testing/LATEST_RELEASE_${chrome_build}")" \
    && [[ "$chromedriver_version" =~ ^[0-9]+([.][0-9]+){3}$ ]] \
    && chromedriver_build="${chromedriver_version%.*}" \
    && test "$chrome_build" = "$chromedriver_build" \
    && chromedriver_url="https://storage.googleapis.com/chrome-for-testing-public/${chromedriver_version}/linux64/chromedriver-linux64.zip" \
    && wget -q -O /tmp/chromedriver.zip "$chromedriver_url" \
    && chromedriver_archive_sha256="$(sha256sum /tmp/chromedriver.zip | awk '{print $1}')" \
    && [[ "$chromedriver_archive_sha256" =~ ^[0-9a-f]{64}$ ]] \
    && unzip -q /tmp/chromedriver.zip -d /tmp/chromedriver \
    && install -m 0755 /tmp/chromedriver/chromedriver-linux64/chromedriver /usr/local/bin/chromedriver \
    && actual_driver_version="$(/usr/local/bin/chromedriver --version | awk '{print $2}')" \
    && [[ "$actual_driver_version" =~ ^[0-9]+([.][0-9]+){3}$ ]] \
    && test "$actual_driver_version" = "$chromedriver_version" \
    && chromedriver_binary_sha256="$(sha256sum /usr/local/bin/chromedriver | awk '{print $1}')" \
    && [[ "$chromedriver_binary_sha256" =~ ^[0-9a-f]{64}$ ]] \
    && install -d -m 0755 /usr/local/share/hems-api \
    && printf '%s\n' \
       "chrome_version=$chrome_version" \
       "chrome_build=$chrome_build" \
       "chromedriver_version=$chromedriver_version" \
       "chromedriver_url=$chromedriver_url" \
       "chromedriver_archive_sha256=$chromedriver_archive_sha256" \
       "chromedriver_binary_sha256=$chromedriver_binary_sha256" \
       > /usr/local/share/hems-api/chrome-build-info \
    && rm -rf /tmp/chromedriver /tmp/chromedriver.zip /var/lib/apt/lists/*

WORKDIR /app

# Python の stdout を行バッファに (デフォルトはブロックバッファで docker logs に出ない)
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY hems_control.py .
COPY hems_runtime.py .
COPY hems_snapshot.py .
COPY hems_api.py .

# Chrome writes its temporary profile under /tmp and crashpad/config under HOME.
RUN groupadd --gid 10001 hems \
    && useradd --uid 10001 --gid hems --create-home --shell /usr/sbin/nologin hems \
    && install -d -o hems -g hems -m 0700 /home/hems/.config /home/hems/.cache
ENV HOME=/home/hems \
    XDG_CONFIG_HOME=/home/hems/.config \
    XDG_CACHE_HOME=/home/hems/.cache
USER 10001:10001

EXPOSE 5000

ENTRYPOINT ["python3", "hems_api.py"]
