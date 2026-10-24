"""Clips4U private, short-video YouTube and TikTok importer.

Only for public videos the operator owns or is allowed to reuse.
No cookies, accounts, playlist downloads, DRM bypass or login support.
Temporary files are deleted after each response.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field

app = FastAPI(title="Clips4U Video Import", docs_url=None, redoc_url=None)
APP_ORIGIN = os.environ.get("CLIPS4U_ALLOWED_ORIGIN", "https://8myztggm5j-eng.github.io").rstrip("/")
ACCESS_KEY = os.environ.get("CLIPS4U_IMPORT_KEY", "")
MAX_SECONDS = 600
MAX_BYTES = 90 * 1024 * 1024
TIMEOUT_SECONDS = 200
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
lock = asyncio.Lock()
logger = logging.getLogger('clips4u.import')


app.add_middleware(
    CORSMiddleware,
    allow_origins=[APP_ORIGIN],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Clips4U-Key"],
    expose_headers=["Content-Disposition"],
)

class ImportRequest(BaseModel):
    url: str = Field(min_length=12, max_length=500)
    rights_confirmed: bool = False

def video_id_from_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        raise HTTPException(400, "Invalid YouTube URL")
    if parts.scheme != "https" or parts.username or parts.password or parts.port:
        raise HTTPException(400, "Use a normal https YouTube link")
    host = (parts.hostname or "").lower()
    if host in ("youtube.com", "www.youtube.com", "m.youtube.com"):
        path = parts.path.strip("/")
        if path == "watch":
            value = parse_qs(parts.query).get("v", [""])[0]
        elif path.startswith(("shorts/", "live/")):
            value = path.split("/")[1] if len(path.split("/")) == 2 else ""
        else:
            value = ""
    elif host in ("youtu.be", "www.youtu.be"):
        value = parts.path.strip("/")
    else:
        raise HTTPException(400, "Only public YouTube video links are accepted")
    if not VIDEO_ID.fullmatch(value):
        raise HTTPException(400, "Could not find a valid 11-character YouTube video ID")
    return value

def source_from_url(url: str) -> tuple[str, str]:
    """Allow only recognized HTTPS video URLs; never accept arbitrary fetch hosts."""
    try:
        parts = urlsplit(url)
    except ValueError:
        raise HTTPException(400, "Invalid video URL")
    if parts.scheme != "https" or parts.username or parts.password or parts.port:
        raise HTTPException(400, "Use a standard HTTPS video link")
    host = (parts.hostname or "").lower()
    if host in ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"):
        video_id = video_id_from_url(url)
        return ("youtube", "https://www.youtube.com/watch?v=" + video_id)
    if host in ("www.tiktok.com", "tiktok.com", "m.tiktok.com"):
        match = re.fullmatch(r"/@([A-Za-z0-9._]+)/video/(\d+)/?", parts.path)
        if match:
            return ("tiktok", "https://www.tiktok.com/@" + match.group(1) + "/video/" + match.group(2))
        # The official TikTok Share action often provides a /t/CODE/ redirect.
        short = re.fullmatch(r"/t/([A-Za-z0-9_-]+)/?", parts.path)
        if short:
            return ("tiktok", "https://www.tiktok.com/t/" + short.group(1) + "/")
        raise HTTPException(400, "Use a public TikTok video link or official tiktok.com/t/ share link")
    if host in ("vm.tiktok.com", "vt.tiktok.com"):
        if not re.fullmatch(r"/[A-Za-z0-9]+/?", parts.path):
            raise HTTPException(400, "Invalid TikTok short link")
        return ("tiktok", "https://" + host + parts.path)
    raise HTTPException(400, "Only YouTube and TikTok video links are supported")


def checked_run(command: list[str], timeout: int, stage: str = 'download') -> subprocess.CompletedProcess:
    # Show a specific, safe error category rather than an unhelpful generic 422.
    # Never log credentials, full command lines or raw stderr to avoid leaks.
    tool = Path(command[0]).name
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        logger.warning("import failure category=timeout stage=%s tool=%s", stage, tool)
        raise HTTPException(504, "The import timed out. Try a shorter or smaller video.")

    if result.returncode == 0:
        return result

    stderr = (result.stderr or "").lower()
    if "sign in to confirm you're not a bot" in stderr or "confirm you’re not a bot" in stderr:
        category = "youtube_bot_check"
        message = "YouTube requires verification from this hosting server. This cannot be solved by the private access key. Import your own original video using Select local video instead."
    elif "sign in" in stderr or "login required" in stderr or "age-restricted" in stderr or "members-only" in stderr:
        category = "youtube_login_required"
        message = "YouTube requires a sign-in or restricts this video. Private, age-restricted and member-only videos are not supported. Use your authorized local copy."
    elif "requested format is not available" in stderr or "requested format is unavailable" in stderr:
        category = "format_unavailable"
        message = "YouTube does not offer a compatible video stream for this link. Try another public video or import your original MP4."
    elif "video unavailable" in stderr or "this video is unavailable" in stderr:
        category = "video_unavailable"
        message = "YouTube reports that this video is unavailable from the server."
    elif "http error 403" in stderr or "forbidden" in stderr:
        category = "youtube_forbidden"
        message = "YouTube rejected this server's video request (403). Use an authorized local copy instead."
    elif "http error 429" in stderr or "too many requests" in stderr:
        category = "youtube_rate_limit"
        message = "YouTube temporarily limited requests from this server. Wait before trying again."
    elif "unsupported url" in stderr:
        category = "url_unsupported"
        message = "The video link is not supported by YouTube import."
    elif "no supported javascript runtime" in stderr or "could not resolve" in stderr and "challenge" in stderr:
        category = "youtube_player_challenge"
        message = "The current server cannot process YouTube's video player information. Try an authorized local MP4 copy."
    elif "unable to extract" in stderr or "no video formats found" in stderr or "only images are available" in stderr:
        category = "youtube_stream_unavailable"
        message = "YouTube did not provide a downloadable video stream to this server. Use your original video file or try another public video."
    elif tool in ("ffmpeg", "ffprobe"):
        category = "media_conversion"
        message = "The video was retrieved, but conversion to MP4 failed. Try a shorter original MP4."
    else:
        category = "retrieval_failed"
        message = "YouTube could not provide this video to the hosting server. Cloud-hosted downloads are not always available; try an authorized local video instead."

    logger.warning("import failure category=%s stage=%s tool=%s exit=%s", category, stage, tool, result.returncode)
    raise HTTPException(422, message)

def retrieve_public_video(video_url: str, provider: str) -> bytes:
    base = ["yt-dlp", "--ignore-config", "--no-playlist", "--no-warnings",
            "--socket-timeout", "15", "--retries", "1", "--fragment-retries", "1",
            "--js-runtimes", "node"]
    with tempfile.TemporaryDirectory(prefix="clips4u_") as temp:
        root = Path(temp)
        # Inspect first so long/livestream videos are rejected before downloading.
        metadata = checked_run(base + ["--skip-download", "--dump-single-json", video_url], 70, stage="metadata")
        try:
            info = json.loads(metadata.stdout)
        except (ValueError, TypeError):
            raise HTTPException(422, "Unable to read video information")
        duration = info.get("duration")
        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
            raise HTTPException(422, "Live streams are not supported")
        if not isinstance(duration, (int, float)) or duration <= 0 or duration > MAX_SECONDS:
            raise HTTPException(422, "Import a public video of 10 minutes or less")
        # Prefer a single mobile-friendly progressive MP4 stream.
        checked_run(base + [
            "--max-filesize", "90M",
            "-f", "b[ext=mp4][height<=720]/b[height<=720]/bv[height<=720]+ba/bv+ba/best" if provider == "youtube" else "best[height<=720]/best",
            "--merge-output-format", "mp4",
            "-o", str(root / "source.%(ext)s"),
            video_url
        ], TIMEOUT_SECONDS, stage="download")
        candidates = [p for p in root.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
        if len(candidates) != 1:
            raise HTTPException(422, "Could not prepare a single video file")
        source = candidates[0]
        if source.stat().st_size > MAX_BYTES:
            raise HTTPException(413, "Video is over the 90 MB import limit")
        details = checked_run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(source)], 20, stage="probe")
        streams = json.loads(details.stdout).get("streams", [])
        video = next((x for x in streams if x.get("codec_type") == "video"), None)
        audio = next((x for x in streams if x.get("codec_type") == "audio"), None)
        if not video:
            raise HTTPException(422, "No video track was found")
        target = root / "import.mp4"
        if source.suffix.lower() == ".mp4" and video.get("codec_name") == "h264" and (not audio or audio.get("codec_name") == "aac"):
            # Avoid needlessly re-encoding already iPhone-compatible videos.
            payload = source.read_bytes()
        else:
            checked_run([
                "ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(source),
                "-map", "0:v:0", "-map", "0:a:0?",
                "-vf", "scale='min(720,iw)':-2",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
                "-movflags", "+faststart", str(target)
            ], TIMEOUT_SECONDS, stage="transcode")
            if not target.exists() or target.stat().st_size > MAX_BYTES:
                raise HTTPException(413, "Converted video exceeds the 90 MB import limit")
            payload = target.read_bytes()
        return payload

@app.get("/health")
def health():
    return {"status": "ok", "requires_access_key": True}

@app.post("/api/import")
async def import_video(data: ImportRequest, x_clips4u_key: str | None = Header(default=None)):
    if len(ACCESS_KEY) < 16:
        raise HTTPException(503, "Server setup is incomplete: import key is not configured")
    if not x_clips4u_key or not hmac.compare_digest(x_clips4u_key, ACCESS_KEY):
        raise HTTPException(401, "Incorrect Clips4U import key")
    if not data.rights_confirmed:
        raise HTTPException(400, "Confirm that you own or may reuse this video")
    provider, safe_url = source_from_url(data.url)
    if lock.locked():
        raise HTTPException(429, "An import is in progress. Please retry shortly.")
    async with lock:
        output = await run_in_threadpool(retrieve_public_video, safe_url, provider)
    return Response(
        content=output,
        media_type="video/mp4",
        headers={
            "Content-Disposition": 'attachment; filename="clips4u-' + provider + '-import.mp4"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


# Keep AI analysis independent from the original authorized YouTube import route.
from highlights import router as highlights_router
app.include_router(highlights_router)
