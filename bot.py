
import os
import json
import asyncio
import logging
from pathlib import Path

import requests
from telegram import Bot
from telegram.constants import ParseMode

TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

API = "https://archive.org/advancedsearch.php"
METADATA = "https://archive.org/metadata/"

DATA_FILE = Path("published.json")
DOWNLOAD_DIR = Path("downloads")

DOWNLOAD_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

# Не считаем фильм разрешённым только по наличию лицензии.
# Для автоматической публикации нужны подтверждённые права
# на скачивание и повторное распространение.


def load_published():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logging.exception("Не удалось прочитать published.json")
    return []


def save_published(items):
    DATA_FILE.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def search_movies():
    params = {
        "q": 'mediatype:movies AND language:rus',
        "fl[]": ["identifier", "title", "description", "licenseurl"],
        "rows": 30,
        "page": 1,
        "output": "json"
    }

    response = requests.get(API, params=params, timeout=30)
    response.raise_for_status()

    return response.json()["response"]["docs"]


def get_video_file(identifier):
    response = requests.get(
        METADATA + identifier,
        timeout=30
    )
    response.raise_for_status()

    files = response.json().get("files", [])

    for file in files:
        name = file.get("name", "")
        if name.lower().endswith(".mp4"):
            return file

    return None


def download_movie(identifier, file_info):
    filename = Path(file_info["name"]).name
    destination = DOWNLOAD_DIR / filename

    url = (
        f"https://archive.org/download/"
        f"{identifier}/{file_info['name']}"
    )

    with requests.get(
        url,
        stream=True,
        timeout=60
    ) as response:
        response.raise_for_status()

        with open(destination, "wb") as output:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    output.write(chunk)

    return destination


async def publish_movie(bot, movie, file_path):
    with open(file_path, "rb") as video:
        await bot.send_video(
            chat_id=CHANNEL_ID,
            video=video,
            caption=f"🎬 {movie.get('title', 'Фильм')}",
            supports_streaming=True
        )


async def main():
    if not TOKEN or not CHANNEL_ID:
        raise RuntimeError("Не настроены BOT_TOKEN или CHANNEL_ID")

    bot = Bot(TOKEN)
    published = load_published()

    movies = search_movies()

    logging.info("Найдено фильмов: %s", len(movies))

    # Пока автоматическая публикация отключена:
    # без проверки прав и разрешения на распространение
    # нельзя безопасно скачивать и публиковать найденные фильмы.
    for movie in movies:
        identifier = movie.get("identifier")

        if not identifier or identifier in published:
            continue

        logging.info(
            "Кандидат: %s (%s)",
            movie.get("title"),
            identifier
        )

    logging.info("Поиск завершён. Публикация ожидает проверки прав.")


if __name__ == "__main__":
    asyncio.run(main())
