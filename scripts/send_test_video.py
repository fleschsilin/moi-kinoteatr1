import asyncio
import os
import sys
from pathlib import Path

from telegram import Bot


async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    chat_id = os.getenv("TEST_CHAT_ID")
    base_url = os.getenv("BOT_API_BASE_URL", "http://127.0.0.1:8081/bot")
    file_url = os.getenv("BOT_API_FILE_BASE_URL", "http://127.0.0.1:8081/file/bot")
    confirmed = os.getenv("CONFIRM_SEND_TEST_VIDEO", "false").lower() == "true"

    if not token or not chat_id:
        raise RuntimeError("Set BOT_TOKEN and TEST_CHAT_ID.")
    if not confirmed:
        raise RuntimeError("Set CONFIRM_SEND_TEST_VIDEO=true to authorize a real test upload.")
    if len(sys.argv) != 2:
        raise RuntimeError("Usage: python scripts/send_test_video.py /path/to/test.mp4")

    video_path = Path(sys.argv[1]).expanduser().resolve()
    if not video_path.is_file() or video_path.suffix.lower() != ".mp4":
        raise RuntimeError("The test input must be an existing MP4 file.")

    size = video_path.stat().st_size
    if size <= 0 or size > 1_900_000_000:
        raise RuntimeError(f"Test video size is outside the safe limit: {size} bytes.")

    bot = Bot(token, base_url=base_url, base_file_url=file_url)
    await bot.initialize()
    try:
        me = await bot.get_me()
        print(f"Connected to Local Bot API as @{me.username or me.id}; sending {size} bytes.")
        with video_path.open("rb") as stream:
            message = await bot.send_video(
                chat_id=chat_id,
                video=stream,
                supports_streaming=True,
                caption=f"Тест доставки Local Bot API · {video_path.name}",
                read_timeout=900,
                write_timeout=900,
                connect_timeout=30,
                pool_timeout=30,
            )
        print(f"Upload succeeded. chat_id={message.chat_id}, message_id={message.message_id}")
    finally:
        await bot.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
