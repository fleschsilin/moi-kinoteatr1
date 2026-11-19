import os
import json
import asyncio
import logging
from pathlib import Path
from urllib.parse import quote

import requests
from telegram import Bot

TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

API = "https://archive.org/advancedsearch.php"
METADATA = "https://archive.org/metadata/"

DATA_FILE = Path("published.json")
DOWNLOAD_DIR = Path("downloads")

MAX_MOVIES = 3
MAX_SIZE = 49 * 1024 * 1024

DOWNLOAD_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

# Разрешаем только известные лицензии на распространение.
ALLOWED_LICENSES = (
    "creativecommons.org/publicdomain/zero/",
    "creativecommons.org/publicdomain/mark/",
    "creativecommons.org/licenses/by/4.0/",
    "creativecommons.org/licenses/by-sa/4.0/",
    "creativecommons.org/licenses/by/3.0/",
    "creativecommons.org/licenses/by-sa/3.0/",
)


def load_published():
    if not DATA_FILE.exists():
        return []

    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logging.exception("Ошибка чтения published.json")
        return []


def save_published(items):
    DATA_FILE.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def search_movies():
    params = [
        ("q", "mediatype:movies AND language:rus"),
        ("fl[]", "identifier"),
        ("fl[]", "title"),
        ("rows", "100"),
        ("page", "1"),
        ("output", "json"),
        ("sort[]", "downloads desc"),
    ]

    response = requests.get(API, params=params, timeout=40)
    response.raise_for_status()

    return response.json()["response"]["docs"]


def get_metadata(identifier):
    response = requests.get(
        METADATA + quote(identifier, safe=""),
        timeout=40
    )
    response.raise_for_status()
    return response.json()


def check_license(metadata):
    info = metadata.get("metadata", {})
    license_url = info.get("licenseurl", "")

    if not isinstance(license_url, str):
        return None

    license_url = license_url.lower().strip()

    if any(item in license_url for item in ALLOWED_LICENSES):
        return license_url

    return None


def get_video_file(metadata):
    files = metadata.get("files", [])

    candidates = []

    for file in files:
        name = file.get("name", "")

        if not name.lower().endswith(".mp4"):
            continue

        try:
            size = int(file.get("size", 0))
        except (ValueError, TypeError):
            continue

        if 0 < size <= MAX_SIZE:
            candidates.append(file)

    if not candidates:
        return None

    return max(
        candidates,
        key=lambda item: int(item.get("size", 0))
    )


def download_movie(identifier, file_info):
    name = file_info["name"]

    url = (
        "https://archive.org/download/"
        + quote(identifier, safe="")
        + "/"
        + quote(name, safe="/")
    )

    filename = Path(name).name
    destination = DOWNLOAD_DIR / filename

    downloaded = 0

    with requests.get(
        url,
        stream=True,
        timeout=(30, 120)
    ) as response:

        response.raise_for_status()

        with open(destination, "wb") as output:
            for chunk in response.iter_content(1024 * 1024):
                if not chunk:
                    continue

                downloaded += len(chunk)

                if downloaded > MAX_SIZE:
                    raise ValueError("Файл превышает лимит Telegram")

                output.write(chunk)

    return destination


async def publish_movie(bot, movie, file_path, license_url):
    identifier = movie["identifier"]
    title = movie.get("title", "Фильм")

    caption = (
        f"🎬 {title}\n\n"
        f"Источник: https://archive.org/details/{identifier}\n"
        f"Лицензия: {license_url}"
    )

    with open(file_path, "rb") as video:
        await bot.send_video(
            chat_id=CHANNEL_ID,
            video=video,
            caption=caption,
            supports_streaming=True,
            read_timeout=180,
            write_timeout=180,
            connect_timeout=30
        )


async def main():
    if not TOKEN or not CHANNEL_ID:
        raise RuntimeError(
            "Не настроены BOT_TOKEN или CHANNEL_ID"
        )

    published = load_published()
    movies = search_movies()

    logging.info("Найдено кандидатов: %s", len(movies))

    bot = Bot(TOKEN)
    await bot.initialize()

    count = 0

    try:
        for movie in movies:

            if count >= MAX_MOVIES:
                break

            identifier = movie.get("identifier")

            if not identifier or identifier in published:
                continue

            try:
                logging.info("Проверяем: %s", movie.get("title"))

                metadata = get_metadata(identifier)

                license_url = check_license(metadata)

                if not license_url:
                    logging.info("Лицензия не подтверждена: %s", identifier)
                    continue

                file_info = get_video_file(metadata)

                if not file_info:
                    logging.info("Подходящий MP4 не найден: %s", identifier)
                    continue

                file_path = download_movie(identifier, file_info)

                try:
                    await publish_movie(
                        bot,
                        movie,
                        file_path,
                        license_url
                    )
                finally:
                    file_path.unlink(missing_ok=True)

                published.append(identifier)
                save_published(published)

                count += 1

                logging.info(
                    "Опубликован фильм: %s",
                    movie.get("title")
                )

            except Exception:
                logging.exception(
                    "Ошибка обработки фильма %s",
                    identifier
                )
                continue

    finally:
        await bot.shutdown()

    logging.info("Работа завершена. Опубликовано: %s", count)


if __name__ == "__main__":
    asyncio.run(main())
