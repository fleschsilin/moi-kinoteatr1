#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -m)" != "aarch64" && "$(uname -m)" != "x86_64" ]]; then
  echo "Unsupported architecture: $(uname -m)" >&2
  exit 1
fi

sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  git gperf cmake g++ build-essential zlib1g-dev libssl-dev

if [[ ! -d /opt/telegram-bot-api/.git ]]; then
  sudo git clone --recursive https://github.com/tdlib/telegram-bot-api.git /opt/telegram-bot-api
else
  sudo git -C /opt/telegram-bot-api submodule update --init --recursive
  sudo git -C /opt/telegram-bot-api pull --ff-only
fi

sudo cmake -S /opt/telegram-bot-api -B /opt/telegram-bot-api/build \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local
sudo cmake --build /opt/telegram-bot-api/build --target install --parallel 2

sudo useradd --system --home-dir /var/lib/telegram-bot-api \
  --create-home --shell /usr/sbin/nologin telegram-bot-api 2>/dev/null || true
sudo install -d -o telegram-bot-api -g telegram-bot-api -m 0750 \
  /var/lib/telegram-bot-api /var/lib/telegram-bot-api/tmp
sudo install -d -m 0750 /etc/movie-bot

echo "Telegram Local Bot API binary installed."
echo "Next: create /etc/movie-bot/local-api.env and install the systemd unit."
