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

API = "https://archive.org/advancedsearch.php"
METADATA = "https://archive.org/metadata/"

DATA_FILE = Path("published.json")
DOWNLOAD_DIR = Path("downloads")

MAX_MOVIES = 3
MAX_SIZE = 49 * 1024 * 1024

# Безопасный режим включён по умолчанию. Для публикации нужны одновременно
# DRY_RUN=false и явный список проверенных вручную идентификаторов.
DRY_RUN = os.getenv("DRY_RUN", "true").strip().lower() != "false"
APPROVED_IDENTIFIERS = {
    item.strip()
    for item in os.getenv("APPROVED_IDENTIFIERS", "").split(",")
    if item.strip()
}

# Дополнительный фильтр очевидно опасных материалов по названию.
BLOCKED_TITLE_TERMS = (
    "suicide", "beheading", "gore", "snuff", "ak-47",
    "3 guys 1 hammer", "three guys one hammer",
    "самоубий", "расчлен", "казнь", "терроризм",
)

DOWNLOAD_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

ALLOWED_LICENSE_PATHS = (
    "/publicdomain/zero/",
    "/publicdomain/mark/",
    "/licenses/by/4.0/",
    "/licenses/by-sa/4.0/",
    "/licenses/by/3.0/",
    "/licenses/by-sa/3.0/",
)


def title_is_allowed(title):
    normalized = str(title or "").casefold()
    return not any(term in normalized for term in BLOCKED_TITLE_TERMS)


def is_manually_approved(identifier):
    return identifier in APPROVED_IDENTIFIERS


def load_published():
    if not DATA_FILE.exists():
        return []

    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        logging.exception("Ошибка чтения published.json")
        # Fail closed: не сбрасываем историю и не публикуем повторно.
        raise RuntimeError("Не удалось безопасно прочитать published.json")


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
    payload = response.json()
    return payload.get("response", {}).get("docs", [])


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

    parsed = urlparse(license_url.strip().lower())
    if parsed.scheme != "https" or parsed.hostname != "creativecommons.org":
        return None

    if any(parsed.path.startswith(path) for path in ALLOWED_LICENSE_PATHS):
        return license_url.strip()

    return None


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
        raise RuntimeError("Не настроены BOT_TOKEN или CHANNEL_ID")

    if not DRY_RUN and not APPROVED_IDENTIFIERS:
        raise RuntimeError(
            "Публикация заблокирована: APPROVED_IDENTIFIERS пуст. "
            "Добавьте только вручную проверенные материалы."
        )

    published = load_published()
    movies = search_movies()
    logging.info("Найдено кандидатов: %s", len(movies))
    logging.info(
        "Режим: %s; вручную одобрено идентификаторов: %s",
        "DRY RUN — публикация отключена" if DRY_RUN else "ПУБЛИКАЦИЯ",
        len(APPROVED_IDENTIFIERS)
    )

    bot = None
    count = 0

    if not DRY_RUN:
        bot = Bot(TOKEN)
        await bot.initialize()

    try:
        for movie in movies:
            if count >= MAX_MOVIES:
                break

            identifier = movie.get("identifier")
            title = movie.get("title", "")

            if not identifier or identifier in published:
                continue

            if not title_is_allowed(title):
                logging.warning("Отклонено опасное название: %s", title)
                continue

            if not is_manually_approved(identifier):
                logging.info(
                    "Пропуск: нет ручного одобрения: %s (%s)",
                    title, identifier
                )
                continue

            try:
                logging.info("Проверяем одобренный материал: %s", title)
                metadata = get_metadata(identifier)

                license_url = check_license(metadata)
                if not license_url:
                    logging.warning("Лицензия не подтверждена: %s", identifier)
                    continue

                file_info = get_video_file(metadata)
                if not file_info:
                    logging.info("Подходящий MP4 не найден: %s", identifier)
                    continue

                # В DRY_RUN не скачиваем файлы и не обращаемся к Telegram.
                if DRY_RUN:
                    logging.info(
                        "DRY RUN: кандидат прошёл метаданные и лицензию: %s",
                        identifier
                    )
                    continue

                file_path = download_movie(identifier, file_info)
                try:
                    await publish_movie(bot, movie, file_path, license_url)
                finally:
                    file_path.unlink(missing_ok=True)

                published.append(identifier)
                save_published(published)
                count += 1
                logging.info("Опубликован фильм: %s", title)

            except Exception:
                logging.exception("Ошибка обработки фильма %s", identifier)
                continue

    finally:
        if bot is not None:
            await bot.shutdown()

    logging.info("Работа завершена. Опубликовано: %s", count)


if __name__ == "__main__":
    asyncio.run(main())
