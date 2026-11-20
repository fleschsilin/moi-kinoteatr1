# Local Telegram Bot API deployment

This deployment is intended for an Ubuntu VM (preferably OCI Ampere A1, Always Free eligible).
The official Local Bot API server supports uploads up to 2000 MB in `--local` mode.
The HTTP API is bound to loopback only; do not expose port 8081 to the internet.

## 1. Create the Oracle VM

In OCI Console, create an Ubuntu VM in the tenancy home region and select an Always Free eligible
`VM.Standard.A1.Flex` shape. A single VM can use up to 2 OCPUs and 12 GB RAM within the
Always Free allowance. Assign a public IPv4 address for SSH access, but do not open TCP 8081
in the VCN security list or network security group. Keep SSH restricted to your own IP.

Oracle may report `out of host capacity`; if so, try another availability domain or retry later.
Always Free compute must be provisioned in the tenancy home region.

## 2. Install Local Bot API

Copy this repository to the VM, then run:

```bash
chmod +x deploy/install_local_bot_api.sh
./deploy/install_local_bot_api.sh
```

The script installs build dependencies and compiles the official TDLib server from source.
The build can take time on a small VM.

Create the private credentials file:

```bash
sudo install -m 600 /dev/null /etc/movie-bot/local-api.env
sudo nano /etc/movie-bot/local-api.env
```

Set `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from the Telegram API development page
(my.telegram.org). Do not put these values in GitHub or commit them to this repository.

Install and start the Local Bot API service:

```bash
sudo install -m 644 deploy/telegram-bot-api.service /etc/systemd/system/telegram-bot-api.service
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-bot-api
sudo systemctl status telegram-bot-api --no-pager
```

The service listens only on 127.0.0.1:8081. Verify locally with a Bot API `getMe` request
after the bot is moved to the local server.

## 3. Prepare the bot

Create a dedicated system user and a virtual environment, then install this repository's
Python dependencies. Configure `/etc/movie-bot/bot.env` from
`deploy/movie-bot.env.example` with the bot token and channel ID. Keep the file mode 600.
Initially keep `DRY_RUN=true`; do not enable channel publication during infrastructure testing.

The bot uses:
- `BOT_API_BASE_URL=http://127.0.0.1:8081/bot`
- `BOT_API_FILE_BASE_URL=http://127.0.0.1:8081/file/bot`
- `TELEGRAM_LOCAL_API=true`
- `MAX_UPLOAD_BYTES=1900000000`

The application retains the 49 MiB default when Local Bot API mode is off.

## 4. Move the bot safely

Telegram requires calling `logOut` against the cloud Bot API before switching the bot to a
local Bot API server. This can interrupt the existing bot and prevents logging back into the
cloud API for 10 minutes. Do not call `logOut` until the VM, local API service, private
credentials, and bot configuration are all ready and a maintenance window is agreed.

Do not run the same bot token on the cloud API and local API simultaneously. Follow the
official TDLib migration instructions.

## 5. Test one real video privately

Place a legally cleared MP4 on the VM. Set `TEST_CHAT_ID` to a private test chat where the bot
is allowed to send messages, and set `CONFIRM_SEND_TEST_VIDEO=true` only for the test.

```bash
CONFIRM_SEND_TEST_VIDEO=true \
python scripts/send_test_video.py /path/to/test-video.mp4
```

The script refuses to send unless explicitly confirmed and enforces a 1.9 GB safety cap.
Verify that Telegram receives the video and can play it. This test does not publish to the
public channel.

## Safety gates

- No real credentials belong in Git.
- Keep port 8081 private and bound to loopback.
- Do not merge this branch or switch the production bot until the deployment is reviewed.
- Do not enable channel publication as part of this infrastructure test.
- Upload only media with documented distribution rights.
