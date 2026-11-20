import os
import json
import asyncio
import logging
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
from telegram import Bot

TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

METADATA = "https://archive.org/metadata/"
DATA_FILE = Path("published.json")
LIBRARY_FILE = Path("library.json")
DOWNLOAD_DIR = Path("downloads")

MAX_MOVIES = 3
LOCAL_BOT_API = os.getenv("TELEGRAM_LOCAL_API", "false").strip().lower() == "true"
DEFAULT_MAX_SIZE = 1_900_000_000 if LOCAL_BOT_API else 49 * 1024 * 1024
MAX_SIZE = int(os.getenv("MAX_UPLOAD_BYTES", str(DEFAULT_MAX_SIZE)))
BOT_API_BASE_URL = os.getenv("BOT_API_BASE_URL", "https://api.telegram.org/bot")
BOT_API_FILE_BASE_URL = os.getenv("BOT_API_FILE_BASE_URL", "https://api.telegram.org/file/bot")
DRY_RUN = os.getenv("DRY_RUN", "true").strip().lower() != "false"
TARGET_TERRITORY = "KZ"

BLOCKED_TITLE_TERMS = (
    "suicide", "beheading", "gore", "snuff", "ak-47",
    "3 guys 1 hammer", "three guys one hammer",
    "самоубий", "расчлен", "казнь", "терроризм",
)

ALLOWED_LICENSE_PATHS = (
    "/publicdomain/zero/",
    "/licenses/by/4.0/",
    "/licenses/by-sa/4.0/",
    "/licenses/by/3.0/",
    "/licenses/by-sa/3.0/",
)

DOWNLOAD_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)


def title_is_allowed(title):
    normalized = str(title or "").casefold()
    return not any(term in normalized for term in BLOCKED_TITLE_TERMS)


def check_license(metadata):
    info = metadata.get("metadata", {})
    license_url = info.get("licenseurl", "")

    if not isinstance(license_url, str):
        return None

    parsed = urlparse(license_url.strip().lower())
    if parsed.scheme != "https" or parsed.hostname != "creativecommons.org":
        return None

    if any(parsed.path.startswith(path) for path in ALLOWED_LICENSE_PATHS):
        return license_url.strip()

    return None


def validate_library_entry(entry):
    required = (
        "identifier", "title", "year", "director", "language",
        "synopsis", "genre", "license_url", "rights_basis",
        "rights_evidence_url", "rights_status", "rights_territories",
        "content_review", "reviewed_by", "reviewed_at", "approved",
    )
    if not isinstance(entry, dict):
        raise ValueError("Запись фильма должна быть объектом")

    missing = [key for key in required if key not in entry or entry[key] in (None, "", [])]
    if missing:
        raise ValueError("В записи фильма отсутствуют поля: " + ", ".join(missing))

    if not isinstance(entry["year"], int) or not 1888 <= entry["year"] <= 2100:
        raise ValueError("Некорректный год фильма: " + str(entry.get("identifier")))

    if entry["approved"] is not True:
        raise ValueError("В library.json допускаются только approved=true")

    if entry["rights_status"] != "verified":
        raise ValueError("Права не подтверждены: " + str(entry["identifier"]))

    if entry["content_review"] != "passed":
        raise ValueError("Проверка содержания не пройдена: " + str(entry["identifier"]))

    if TARGET_TERRITORY not in entry["rights_territories"]:
        raise ValueError("Нет подтверждения прав для территории KZ: " + str(entry["identifier"]))

    if not title_is_allowed(entry["title"]):
        raise ValueError("Название попало в блок-лист: " + str(entry["identifier"]))

    evidence = urlparse(str(entry["rights_evidence_url"]))
    if evidence.scheme != "https" or not evidence.netloc:
        raise ValueError("Некорректная ссылка на доказательство прав")

    license = urlparse(str(entry["license_url"]).strip().lower())
    if license.scheme != "https" or license.hostname != "creativecommons.org":
        raise ValueError("Разрешены только проверенные лицензии Creative Commons")

    if not any(license.path.startswith(path) for path in ALLOWED_LICENSE_PATHS):
        raise ValueError("Лицензия не входит в список разрешённых")

    return entry


def load_library():
    if not LIBRARY_FILE.exists():
        raise RuntimeError("Нет library.json: публикация остановлена")

    try:
        data = json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError("Не удалось прочитать library.json") from exc

    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise RuntimeError("Неверная версия схемы library.json")

    films = data.get("films")
    if not isinstance(films, list):
        raise RuntimeError("Поле films должно быть списком")

    seen = set()
    for entry in films:
        validate_library_entry(entry)
        identifier = entry["identifier"]
        if identifier in seen:
            raise RuntimeError("Дублирующийся identifier в library.json: " + identifier)
        seen.add(identifier)

    return films


def load_published():
    if not DATA_FILE.exists():
        return []

    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
            raise ValueError("published.json должен содержать список строк")
        return data
    except (json.JSONDecodeError, OSError, ValueError):
        logging.exception("Ошибка чтения published.json")
        raise RuntimeError("Не удалось безопасно прочитать published.json")


def save_published(items):
    DATA_FILE.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def get_metadata(identifier):
    response = requests.get(
        METADATA + quote(identifier, safe=""),
        timeout=40
    )
    response.raise_for_status()
    return response.json()


def get_video_file(metadata):
    files = metadata.get("files", [])
    candidates = []

    for file_info in files:
        if not isinstance(file_info, dict):
            continue

        name = file_info.get("name", "")
        if not isinstance(name, str) or not name.lower().endswith(".mp4"):
            continue

        try:
            size = int(file_info.get("size", 0))
        except (ValueError, TypeError):
            continue

        if 0 < size <= MAX_SIZE:
            candidates.append(file_info)

    if not candidates:
        return None

    return max(candidates, key=lambda item: int(item.get("size", 0)))


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

    with requests.get(url, stream=True, timeout=(30, 120)) as response:
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


async def publish_movie(bot, movie, file_path):
    identifier = movie["identifier"]
    title = movie["title"]
    caption = (
        f"🎬 {title} ({movie['year']})\n"
        f"Режиссёр: {movie['director']}\n"
        f"Жанр: {movie['genre']}\n\n"
        f"{movie['synopsis']}\n\n"
        f"Источник: https://archive.org/details/{identifier}\n"
        f"Лицензия: {movie['license_url']}"
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
        raise RuntimeError("Не настроены BOT_TOKEN или CHANNEL_ID")

    library = load_library()
    if not DRY_RUN and not library:
        raise RuntimeError("Публикация заблокирована: проверенная библиотека пуста")

    published = load_published()
    logging.info("Проверенных фильмов в библиотеке: %s", len(library))
    logging.info(
        "Режим: %s",
        "DRY RUN — публикация отключена" if DRY_RUN else "ПУБЛИКАЦИЯ"
    )

    bot = None
    count = 0
    if not DRY_RUN:
        bot = Bot(TOKEN, base_url=BOT_API_BASE_URL, base_file_url=BOT_API_FILE_BASE_URL)
        await bot.initialize()

    try:
        for movie in library:
            if count >= MAX_MOVIES:
                break

            identifier = movie["identifier"]
            if identifier in published:
                logging.info("Уже опубликован: %s", identifier)
                continue

            try:
                metadata = get_metadata(identifier)
                actual_license = check_license(metadata)
                if not actual_license or actual_license.rstrip("/") != movie["license_url"].rstrip("/"):
                    logging.warning("Лицензия источника не совпала с каталогом: %s", identifier)
                    continue

                file_info = get_video_file(metadata)
                if not file_info:
                    logging.warning("Нет подходящего MP4: %s", identifier)
                    continue

                if DRY_RUN:
                    logging.info(
                        "DRY RUN: проверен фильм из каталога: %s (%s)",
                        movie["title"], identifier
                    )
                    continue

                file_path = download_movie(identifier, file_info)
                try:
                    await publish_movie(bot, movie, file_path)
                finally:
                    file_path.unlink(missing_ok=True)

                published.append(identifier)
                save_published(published)
                count += 1
                logging.info("Опубликован фильм: %s", movie["title"])

            except Exception:
                logging.exception("Ошибка обработки фильма %s", identifier)
                continue

    finally:
        if bot is not None:
            await bot.shutdown()

    logging.info("Работа завершена. Опубликовано: %s", count)


if __name__ == "__main__":
    asyncio.run(main())
