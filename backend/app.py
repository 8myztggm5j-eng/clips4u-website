"""Clips4U private, short-video YouTube importer.

Only for public videos the operator owns or is allowed to reuse.
No cookies, accounts, playlist downloads, DRM bypass or login support.
Temporary files are deleted after each response.
"""
from __future__ import annotations

import asyncio
import hmac
import json
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

def checked_run(command: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "YouTube retrieval took too long. Try a shorter video.")
    if result.returncode != 0:
        # Do not expose internal paths, keys, cookies, or command output to the visitor.
        raise HTTPException(422, "YouTube could not provide this video. It may be restricted, blocked from cloud hosting, or unavailable.")
    return result

def retrieve_public_video(video_id: str) -> bytes:
    video_url = "https://www.youtube.com/watch?v=" + video_id
    base = ["yt-dlp", "--ignore-config", "--no-playlist", "--no-warnings",
            "--socket-timeout", "15", "--retries", "1", "--fragment-retries", "1",
            "--js-runtimes", "node"]
    with tempfile.TemporaryDirectory(prefix="clips4u_") as temp:
        root = Path(temp)
        # Inspect first so long/livestream videos are rejected before downloading.
        metadata = checked_run(base + ["--skip-download", "--dump-single-json", video_url], 70)
        try:
            info = json.loads(metadata.stdout)
        except (ValueError, TypeError):
            raise HTTPException(422, "Unable to read YouTube video information")
        duration = info.get("duration")
        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
            raise HTTPException(422, "Live streams are not supported")
        if not isinstance(duration, (int, float)) or duration <= 0 or duration > MAX_SECONDS:
            raise HTTPException(422, "Import a public video of 10 minutes or less")
        # Prefer a single mobile-friendly progressive MP4 stream.
        checked_run(base + [
            "--max-filesize", "90M",
            "-f", "b[ext=mp4][height<=720]/best[ext=mp4][height<=720]/b[height<=720]/best[height<=720]",
            "-o", str(root / "source.%(ext)s"),
            video_url
        ], TIMEOUT_SECONDS)
        candidates = [p for p in root.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
        if len(candidates) != 1:
            raise HTTPException(422, "Could not prepare a single video file")
        source = candidates[0]
        if source.stat().st_size > MAX_BYTES:
            raise HTTPException(413, "Video is over the 90 MB import limit")
        details = checked_run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(source)], 20)
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
            ], TIMEOUT_SECONDS)
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
    vid = video_id_from_url(data.url)
    if lock.locked():
        raise HTTPException(429, "An import is in progress. Please retry shortly.")
    async with lock:
        output = await run_in_threadpool(retrieve_public_video, vid)
    return Response(
        content=output,
        media_type="video/mp4",
        headers={
            "Content-Disposition": 'attachment; filename="clips4u-youtube-' + vid + '.mp4"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
