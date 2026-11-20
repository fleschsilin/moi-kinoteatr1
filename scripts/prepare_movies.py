#!/usr/bin/env python3
"""Prepare reviewed open-movie candidates; never publishes to Telegram."""
import argparse
import hashlib
import json
import logging
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import requests

ROOT = Path(__file__).resolve().parents[1]
QUEUE = ROOT / "film_research_queue.json"
WORK = ROOT / "movie_work"
OUTPUT = ROOT / "prepared_movies"
MANIFEST = ROOT / "prepared_manifest.json"
VIDEO_CRF = 18
VIDEO_PRESET = "slow"
AUDIO_KBPS = 192
MAX_SOURCE_BYTES = 3_000_000_000
MAX_ZIP_ENTRIES = 500
MAX_UNPACKED_BYTES = 5_000_000_000
# Telegram's self-hosted Bot API documents a 2,000 MB upload ceiling.
# Keep a margin; this is informational, not an artificial encode-size target.
LOCAL_BOT_API_SAFE_BYTES = 1_900_000_000
VIDEO_EXTENSIONS = {".mov", ".mp4", ".mkv", ".webm", ".m4v"}
MIRROR_HOSTS = (
    "mirror.freedif.org",
    "mirrors.iu13.net",
    "mirror.nju.edu.cn",
    "mirror.dotsrc.org",
)
ALLOWED_SOURCE_HOSTS = set(MIRROR_HOSTS)


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
    if (parsed.scheme != "https" or parsed.hostname not in ALLOWED_SOURCE_HOSTS
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError("Source URL must use HTTPS on an approved Blender mirror")
    return url


def validate_redirect_chain(response):
    for hop in [*response.history, response]:
        parsed = urlparse(hop.url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_SOURCE_HOSTS:
            raise ValueError("Download redirected outside the approved Blender mirror hosts")


def download(url, destination):
    parsed = urlparse(safe_url(url))
    destination.parent.mkdir(parents=True, exist_ok=True)
    errors = []
    for host in MIRROR_HOSTS:
        mirror_url = urlunparse(parsed._replace(netloc=host))
        total = 0
        try:
            with requests.get(mirror_url, stream=True, timeout=(30, 180),
                              allow_redirects=True,
                              headers={"User-Agent": "Mozilla/5.0 (compatible; MoiKinoteatrMediaPrep/2.0)"}) as response:
                response.raise_for_status()
                validate_redirect_chain(response)
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > MAX_SOURCE_BYTES:
                    raise ValueError("Declared source size exceeds 3 GB safety limit")
                with destination.open("wb") as output:
                    for chunk in response.iter_content(1024 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_SOURCE_BYTES:
                            raise ValueError("Source download exceeded 3 GB safety limit")
                        output.write(chunk)
            if total:
                return total
            raise ValueError("Downloaded source is empty")
        except Exception as exc:
            destination.unlink(missing_ok=True)
            errors.append(f"{host}: {exc}")
            logging.warning("Mirror %s failed; trying next approved mirror", host)
    raise RuntimeError("All approved Blender mirrors failed: " + " | ".join(errors))


def safe_extract_zip(archive, destination):
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive) as zf:
        members = zf.infolist()
        if len(members) > MAX_ZIP_ENTRIES:
            raise ValueError("ZIP contains too many entries")
        if sum(m.file_size for m in members) > MAX_UNPACKED_BYTES:
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
         "-show_streams", "-show_chapters", "-of", "json", str(path)],
        check=True, capture_output=True, text=True)
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise ValueError("Source has no video stream")
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    if duration <= 0:
        raise ValueError("Could not determine source duration")
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    subtitles = [s for s in streams if s.get("codec_type") == "subtitle"]
    return {
        "duration_seconds": duration,
        "width": int(video.get("width", 0)),
        "height": int(video.get("height", 0)),
        "frame_rate": video.get("avg_frame_rate") or video.get("r_frame_rate"),
        "video_codec": video.get("codec_name"),
        "pixel_format": video.get("pix_fmt"),
        "audio_streams": len(audio),
        "audio_codecs": [s.get("codec_name") for s in audio],
        "audio_channels": [s.get("channels") for s in audio],
        "subtitle_streams": len(subtitles),
        "subtitle_codecs": [s.get("codec_name") for s in subtitles],
        "chapter_count": len(data.get("chapters", [])),
    }


def transcode(source, destination, source_info, external_subtitle=None):
    """Quality-based encode. Never upscales or shrinks to a fixed byte budget."""
    ffmpeg = require_tool("ffmpeg")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.stem + ".working.mp4")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source)]
    if external_subtitle:
        cmd += ["-i", str(external_subtitle)]
    cmd += ["-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0", "-map_chapters", "0",
            "-c:v", "libx264", "-preset", VIDEO_PRESET, "-crf", str(VIDEO_CRF),
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k",
            "-movflags", "+faststart"]
    if external_subtitle:
        cmd += ["-map", "1:0", "-c:s", "mov_text", "-metadata:s:s:0", "language=rus"]
    else:
        # Retain any source subtitle tracks supported by MP4's timed-text format.
        cmd += ["-map", "0:s?", "-c:s", "mov_text"]
    cmd += [str(temp)]
    try:
        subprocess.run(cmd, check=True)
        final_probe = probe(temp)
        duration = source_info["duration_seconds"]
        if abs(final_probe["duration_seconds"] - duration) > max(2.0, duration * 0.005):
            raise ValueError("Transcode duration differs from source; possible missing ending/credits")
        temp.replace(destination)
    finally:
        temp.unlink(missing_ok=True)
    size = destination.stat().st_size
    return {
        "output_bytes": size,
        "output_sha256": sha256_file(destination),
        "encoding": {"video": "H.264", "crf": VIDEO_CRF, "preset": VIDEO_PRESET,
                     "audio": "AAC", "audio_bitrate_kbps": AUDIO_KBPS,
                     "resolution_policy": "preserve source; never upscale"},
        "probe": final_probe,
        "local_bot_api_size_check": "within_1900MB_safety_margin" if size <= LOCAL_BOT_API_SAFE_BYTES else "exceeds_local_bot_api_safe_margin",
    }


def validate_srt(path, duration):
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    pattern = re.compile(
        r"^(\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2},\d{3})$",
        re.MULTILINE)
    cues = pattern.findall(text)
    if not cues or not any(line.strip().isdigit() for line in text.splitlines()):
        raise ValueError("Downloaded subtitle file does not look like valid SRT")
    def to_ms(stamp):
        h, m, rest = stamp.split(":")
        s, ms = rest.split(",")
        return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms)
    previous = -1
    for start, end in cues:
        if to_ms(end) <= to_ms(start) or to_ms(start) < previous:
            raise ValueError("Subtitle timestamps are invalid or out of order")
        previous = to_ms(start)
    if to_ms(cues[-1][1]) > duration * 1000 + 5000:
        raise ValueError("Subtitle file extends beyond source film duration")
    return len(cues)


def process_candidate(candidate):
    cid = candidate["candidate_id"]
    work_dir, out_dir = WORK / cid, OUTPUT / cid
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
    # Keep byte-identical original master and downloaded package alongside the encode.
    master_dir = out_dir / "original"
    master_dir.mkdir(parents=True, exist_ok=True)
    master_package = master_dir / source_path.name
    shutil.copy2(source_path, master_package)
    master_media = master_dir / media_path.name
    if media_path != source_path:
        shutil.copy2(media_path, master_media)
    else:
        master_media = master_package

    subtitle_path = None
    subtitle_record = []
    subtitle_url = candidate.get("russian_subtitles_url")
    if subtitle_url:
        subtitle_path = out_dir / f"{cid}.ru.srt"
        subtitle_bytes = download(subtitle_url, subtitle_path)
        cue_count = validate_srt(subtitle_path, source_info["duration_seconds"])
        subtitle_record.append({
            "language": "ru", "path": str(subtitle_path.relative_to(ROOT)),
            "bytes": subtitle_bytes, "sha256": sha256_file(subtitle_path),
            "cue_count": cue_count, "status": "format_checked_not_human_reviewed",
        })

    output_path = out_dir / f"{cid}.mp4"
    encode_info = transcode(media_path, output_path, source_info, subtitle_path)
    return {
        "candidate_id": cid, "title": candidate["title"], "year": candidate["year"],
        "source_url": source_url, "download_package_bytes": source_bytes,
        "download_package_sha256": sha256_file(source_path),
        "preserved_original_package": str(master_package.relative_to(ROOT)),
        "preserved_original_media": str(master_media.relative_to(ROOT)),
        "original_media_bytes": media_path.stat().st_size,
        "original_media_sha256": sha256_file(media_path),
        "source_media_probe": source_info,
        "prepared_video": str(output_path.relative_to(ROOT)),
        **encode_info,
        "subtitles": subtitle_record,
        "credits_preservation": "duration_checked_against_source; visual end-credit review still required",
        "content_review": "pending_human_full_film_review",
        "rights_review": "official_license_page_recorded; territory/legal review still required",
        "publication_approved": False,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--candidate")
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
            results.append({"candidate_id": candidate["candidate_id"], "title": candidate["title"],
                            "status": "failed", "error": str(exc), "publication_approved": False})
    MANIFEST.write_text(json.dumps({
        "schema_version": 2,
        "encoding_policy": "quality-based H.264 CRF 18; no 48 MB cap; original masters preserved",
        "telegram_delivery": {
            "standard_cloud_bot_api_max_upload_bytes": 50_000_000,
            "self_hosted_local_bot_api_max_upload_bytes": 2_000_000_000,
            "prepared_file_safe_margin_bytes": LOCAL_BOT_API_SAFE_BYTES,
            "note": "Local Bot API infrastructure is not deployed by this preparation workflow. Files over the safe margin require a separate delivery/storage decision."
        },
        "note": "Preparation is not approval. Full human content review and legal/territory rights review remain mandatory. No Telegram publication is performed.",
        "results": results,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    failures = [r for r in results if r["status"] != "prepared_not_approved"]
    logging.info("Prepared: %d; failed: %d; approved for publication: 0",
                 len(results) - len(failures), len(failures))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
