#!/usr/bin/env python3
"""Prepare reviewed open-movie candidates; never publishes to Telegram."""
import argparse
import hashlib
import json
import logging
import shutil
import subprocess
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
QUEUE = ROOT / "film_research_queue.json"
WORK = ROOT / "movie_work"
OUTPUT = ROOT / "prepared_movies"
MANIFEST = ROOT / "prepared_manifest.json"
TARGET_BYTES = 48_000_000
AUDIO_KBPS = 96
MAX_SOURCE_BYTES = 3_000_000_000
VIDEO_EXTENSIONS = {".mov", ".mp4", ".mkv", ".webm", ".m4v"}
ALLOWED_SOURCE_HOSTS = {"download.blender.org"}
MAX_ZIP_ENTRIES = 500
MAX_UNPACKED_BYTES = 5_000_000_000


def require_tool(name):
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"Required executable not found: {name}")
    return path


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_url(url):
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_SOURCE_HOSTS
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise ValueError("Source URL must use HTTPS on an approved Blender download host")
    return url


def validate_redirect_chain(response):
    for hop in [*response.history, response]:
        parsed = urlparse(hop.url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_SOURCE_HOSTS:
            raise ValueError("Download redirected outside the approved Blender host")


def download(url, destination):
    safe_url(url)
    destination.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with requests.get(url, stream=True, timeout=(30, 180), allow_redirects=True) as response:
        response.raise_for_status()
        validate_redirect_chain(response)
        declared_size = response.headers.get("Content-Length")
        if declared_size and int(declared_size) > MAX_SOURCE_BYTES:
            raise ValueError("Declared source size exceeds 3 GB safety limit")
        with destination.open("wb") as output:
            for chunk in response.iter_content(1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_SOURCE_BYTES:
                    raise ValueError("Source download exceeded 3 GB safety limit")
                output.write(chunk)
    if total == 0:
        raise ValueError("Downloaded source is empty")
    return total


def safe_extract_zip(archive, destination):
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive) as zf:
        members = zf.infolist()
        if len(members) > MAX_ZIP_ENTRIES:
            raise ValueError("ZIP contains too many entries")
        unpacked = sum(member.file_size for member in members)
        if unpacked > MAX_UNPACKED_BYTES:
            raise ValueError("ZIP expanded size exceeds 5 GB safety limit")
        for member in members:
            if member.is_dir():
                continue
            target = (destination / member.filename).resolve()
            if target != root and root not in target.parents:
                raise ValueError("Unsafe path in source ZIP archive")
            mode = (member.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise ValueError("Symlink found in source ZIP archive")
        zf.extractall(destination)


def probe(path):
    result = subprocess.run(
        [require_tool("ffprobe"), "-v", "error", "-show_format",
         "-show_streams", "-of", "json", str(path)],
        check=True, capture_output=True, text=True
    )
    data = json.loads(result.stdout)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        raise ValueError("Source has no video stream")
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    if duration <= 0:
        raise ValueError("Could not determine source duration")
    return {
        "duration_seconds": duration,
        "width": int(video.get("width", 0)),
        "height": int(video.get("height", 0)),
        "video_codec": video.get("codec_name"),
        "audio_streams": sum(1 for s in data.get("streams", []) if s.get("codec_type") == "audio"),
        "subtitle_streams": sum(1 for s in data.get("streams", []) if s.get("codec_type") == "subtitle"),
    }


def video_bitrate_kbps(duration_seconds, target_bytes=TARGET_BYTES):
    # Reserve space for audio, MP4 muxing and metadata.
    total_kbps = target_bytes * 8 / duration_seconds / 1000
    bitrate = int(total_kbps - AUDIO_KBPS - 40)
    if bitrate < 160:
        raise ValueError("Runtime is too long for safe Telegram size at minimum video bitrate")
    return bitrate


def transcode(source, destination, source_duration):
    ffmpeg = require_tool("ffmpeg")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(".working.mp4")
    base_rate = video_bitrate_kbps(source_duration)

    for attempt in range(4):
        rate = max(160, int(base_rate * (0.90 ** attempt)))
        passlog = destination.parent / (destination.stem + "_ffmpeg2pass")
        common = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", "scale='min(854,iw)':-2",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-b:v", f"{rate}k", "-maxrate", f"{rate}k", "-bufsize", f"{rate * 2}k",
            "-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k", "-ac", "2",
            "-map_metadata", "0", "-movflags", "+faststart",
        ]
        try:
            subprocess.run(
                common + ["-pass", "1", "-passlogfile", str(passlog),
                          "-an", "-f", "mp4", "/dev/null"],
                check=True
            )
            subprocess.run(
                common + ["-pass", "2", "-passlogfile", str(passlog), str(temp)],
                check=True
            )
            size = temp.stat().st_size
            if size <= TARGET_BYTES:
                final_probe = probe(temp)
                if abs(final_probe["duration_seconds"] - source_duration) > max(2.0, source_duration * 0.005):
                    raise ValueError("Transcode duration differs from source; credits may have been cut")
                temp.replace(destination)
                return {
                    "output_bytes": size,
                    "output_sha256": sha256_file(destination),
                    "video_bitrate_kbps": rate,
                    "probe": final_probe,
                }
        finally:
            for suffix in (".log", ".log.mbtree"):
                Path(str(passlog) + suffix).unlink(missing_ok=True)
    temp.unlink(missing_ok=True)
    raise ValueError("Could not encode below 48,000,000 bytes without violating minimum bitrate")


def process_candidate(candidate):
    cid = candidate["candidate_id"]
    work_dir = WORK / cid
    out_dir = OUTPUT / cid
    work_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    source_url = safe_url(candidate["source_download_url"])
    source_name = Path(urlparse(source_url).path).name or "source.bin"
    source_path = work_dir / source_name
    source_bytes = download(source_url, source_path)

    media_path = source_path
    if source_path.suffix.lower() == ".zip":
        extracted = work_dir / "extracted"
        safe_extract_zip(source_path, extracted)
        videos = [p for p in extracted.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS]
        if not videos:
            raise ValueError("ZIP archive contains no supported video file")
        media_path = max(videos, key=lambda p: p.stat().st_size)

    source_info = probe(media_path)
    output_path = out_dir / f"{cid}.mp4"
    encode_info = transcode(media_path, output_path, source_info["duration_seconds"])

    subtitles = []
    subtitle_url = candidate.get("russian_subtitles_url")
    if subtitle_url:
        subtitle_path = out_dir / f"{cid}.ru.srt"
        subtitle_bytes = download(subtitle_url, subtitle_path)
        text = subtitle_path.read_text(encoding="utf-8-sig", errors="replace")
        import re
        cue_pattern = re.compile(
            r"^(\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2},\d{3})$",
            re.MULTILINE,
        )
        cues = cue_pattern.findall(text)
        if not cues or not any(line.strip().isdigit() for line in text.splitlines()):
            raise ValueError("Downloaded subtitle file does not look like valid SRT")
        def to_ms(stamp):
            hours, minutes, rest = stamp.split(":")
            seconds, millis = rest.split(",")
            return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(millis)
        previous_start = -1
        for start, end in cues:
            if to_ms(end) <= to_ms(start) or to_ms(start) < previous_start:
                raise ValueError("Subtitle cue timestamps are invalid or out of order")
            previous_start = to_ms(start)
        last_end = to_ms(cues[-1][1])
        if last_end > source_info["duration_seconds"] * 1000 + 5000:
            raise ValueError("Subtitle file extends beyond the source film duration")
        subtitles.append({
            "language": "ru",
            "path": str(subtitle_path.relative_to(ROOT)),
            "bytes": subtitle_bytes,
            "sha256": sha256_file(subtitle_path),
            "status": "downloaded_format_checked_not_human_reviewed",
        })

    return {
        "candidate_id": cid,
        "title": candidate["title"],
        "year": candidate["year"],
        "source_url": source_url,
        "source_bytes": source_bytes,
        "source_sha256": sha256_file(source_path),
        "source_media_file": str(media_path.relative_to(ROOT)),
        "source_media_sha256": sha256_file(media_path),
        "source_media_probe": source_info,
        "prepared_video": str(output_path.relative_to(ROOT)),
        **encode_info,
        "subtitles": subtitles,
        "credits_preservation": "full source duration retained; human visual end-credit verification still required",
        "output_container": "mp4",
        "output_video_codec": encode_info["probe"]["video_codec"],
        "output_audio_streams": encode_info["probe"]["audio_streams"],
        "output_subtitle_streams": encode_info["probe"]["subtitle_streams"],
        "telegram_size_check": "passed" if encode_info["output_bytes"] <= TARGET_BYTES else "failed",
        "content_review": "pending_human_full_film_review",
        "rights_review": "official_license_page_recorded; territory/legal review still required",
        "publication_approved": False,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="Prepare all queue candidates")
    parser.add_argument("--candidate", help="Prepare one candidate_id")
    args = parser.parse_args()
    if not args.all and not args.candidate:
        parser.error("Use --all or --candidate")

    require_tool("ffmpeg")
    require_tool("ffprobe")
    queue = json.loads(QUEUE.read_text(encoding="utf-8"))
    candidates = queue.get("candidates", [])
    if args.candidate:
        candidates = [c for c in candidates if c.get("candidate_id") == args.candidate]
        if not candidates:
            raise SystemExit("Candidate not found")

    results = []
    for candidate in candidates:
        logging.info("Preparing %s", candidate["title"])
        try:
            results.append({"status": "prepared_not_approved", **process_candidate(candidate)})
        except Exception as exc:
            logging.exception("Preparation failed for %s", candidate["candidate_id"])
            results.append({
                "candidate_id": candidate["candidate_id"],
                "title": candidate["title"],
                "status": "failed",
                "error": str(exc),
                "publication_approved": False,
            })

    MANIFEST.write_text(json.dumps({
        "schema_version": 1,
        "target_max_bytes": TARGET_BYTES,
        "note": "Preparation output is not approval. Every movie still requires full human content review and legal/territory rights review.",
        "results": results,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    failures = [r for r in results if r["status"] != "prepared_not_approved"]
    logging.info("Prepared: %d; failed: %d; approved for publication: 0", len(results) - len(failures), len(failures))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
