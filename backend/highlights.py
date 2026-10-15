"""AI-assisted speech highlight suggestions for authorized local or imported videos.
The model only transcribes speech. Highlight scores use an explainable heuristic;
they are not predictions of reach or evidence of copyright permission.
"""
from __future__ import annotations

import hmac
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from secrets import token_urlsafe

from fastapi import APIRouter, BackgroundTasks, File, Form, Header, HTTPException, UploadFile

router = APIRouter()
logger = logging.getLogger("clips4u.highlights")
MAX_BYTES = 90 * 1024 * 1024
MAX_SECONDS = 600
JOBS: dict[str, dict] = {}
ACTIVE_JOB: str | None = None
EXPIRE_SECONDS = 3600
HOOKS = ("wait", "watch", "listen", "look", "imagine", "what if", "why", "how",
         "the reason", "the truth", "here's", "you won't", "did you know", "actually",
         "this is", "never", "first time", "the moment", "secret", "turns out")
IMPACT = ("incredible", "unbelievable", "amazing", "surprising", "crazy",
          "shocking", "worst", "best", "biggest", "danger", "important",
          "love", "hate", "scared", "excited", "finally", "suddenly", "wow", "laugh")


def require_key(key: str | None) -> None:
    expected = os.environ.get("CLIPS4U_IMPORT_KEY", "")
    if len(expected) < 16:
        raise HTTPException(503, "AI highlight service needs the existing Render access key configuration")
    if not key or not hmac.compare_digest(expected, key):
        raise HTTPException(401, "Incorrect Clips4U import key")


def cleanup_jobs() -> None:
    now = time.time()
    for ident, job in list(JOBS.items()):
        if job.get("status") in ("complete", "error") and now - job.get("updated", now) > EXPIRE_SECONDS:
            del JOBS[ident]


def probe_seconds(video_path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
        capture_output=True, text=True, timeout=25,
    )
    if result.returncode:
        raise ValueError("Not a readable video")
    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise ValueError("Unknown video duration") from exc
    if not 1 <= duration <= MAX_SECONDS:
        raise ValueError("Choose a video between 1 second and 10 minutes")
    return duration


def rank_moments(words: list[dict], duration: float) -> list[dict]:
    """Build speech-aligned 15-60 second ranges, rank, then avoid overlaps."""
    if not words:
        return []
    candidates = []
    for first in range(0, len(words), 7):
        start = max(0.0, float(words[first]["start"]) - 0.6)
        for desired in (20, 30, 45, 60):
            end_limit = min(duration, start + desired)
            indices = [i for i in range(first, len(words))
                       if words[i]["end"] <= end_limit]
            if not indices:
                continue
            last = indices[-1]
            # Prefer ending after a sentence, when close enough to the target.
            for pos in range(last, max(first - 1, last - 14), -1):
                if words[pos]["text"].rstrip().endswith((".", "!", "?")) and words[pos]["end"] - start >= 15:
                    last = pos
                    break
            end = min(duration, float(words[last]["end"]) + 0.5)
            length = end - start
            if length < 15 or length > 61:
                continue
            snippet_words = [w["text"].strip() for w in words[first:last + 1]]
            script = " ".join(snippet_words)
            lower = script.lower()
            count = len(snippet_words)
            hooks = sum(1 for phrase in HOOKS if re.search(r"\b" + re.escape(phrase) + r"\b", lower))
            impact = sum(1 for phrase in IMPACT if re.search(r"\b" + re.escape(phrase) + r"\b", lower))
            density = count / max(length, 1)
            beginning = words[first]["text"].strip()
            ending = words[last]["text"].strip()
            score = 35 + min(15, count / 5.0) + min(17, hooks * 4.3)
            score += min(13, impact * 4.0) + min(8, script.count("?") * 3 + script.count("!") * 2)
            score += min(7, density * 2.0) + (5 if ending.endswith((".", "!", "?")) else 0)
            if beginning and beginning[0].isupper():
                score += 2
            if count < 25:
                score -= 10
            score = max(1, min(97, round(score)))
            reason_parts = []
            if hooks:
                reason_parts.append("attention-grabbing language")
            if impact:
                reason_parts.append("expressive wording")
            if "?" in script:
                reason_parts.append("question or curiosity hook")
            if ending.endswith((".", "!", "?")):
                reason_parts.append("natural sentence ending")
            if not reason_parts:
                reason_parts.append("continuous spoken content")
            candidates.append({
                "start": round(start, 1), "end": round(end, 1),
                "score": score,
                "reason": ", ".join(reason_parts[:2]).capitalize(),
                "transcript": script[:460],
            })
    candidates.sort(key=lambda x: (-x["score"], x["start"]))
    selected = []
    for item in candidates:
        if any(max(0, min(item["end"], chosen["end"]) - max(item["start"], chosen["start"])) > 2 for chosen in selected):
            continue
        selected.append(item)
        if len(selected) == 3:
            break
    selected.sort(key=lambda x: -x["score"])
    for i, item in enumerate(selected):
        item["rank"] = i + 1
    return selected


def analyze(job_id: str, folder: str, source_path: str) -> None:
    global ACTIVE_JOB
    job = JOBS[job_id]
    try:
        job.update(status="running", stage="Checking video", updated=time.time())
        duration = probe_seconds(Path(source_path))
        audio_path = Path(folder) / "speech.wav"
        job.update(stage="Extracting speech audio", updated=time.time())
        result = subprocess.run([
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", source_path, "-map", "0:a:0", "-vn",
            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(audio_path)
        ], capture_output=True, timeout=100)
        if result.returncode or not audio_path.is_file():
            raise ValueError("Could not read an audio track. This feature needs speech in the video.")
        job.update(stage="Transcribing with faster-whisper (first run may load the model)", updated=time.time())
        from faster_whisper import WhisperModel
        model = WhisperModel("tiny", device="cpu", compute_type="int8", cpu_threads=2,
                             num_workers=1, download_root="/tmp/clips4u-whisper-model")
        segments, info = model.transcribe(str(audio_path), beam_size=1, word_timestamps=True,
                                          vad_filter=True, condition_on_previous_text=False)
        words = []
        for segment in segments:
            if segment.words:
                for word in segment.words:
                    if word.word.strip() and word.end is not None and word.start is not None:
                        words.append({"start": float(word.start), "end": float(word.end), "text": word.word})
            elif segment.text.strip():
                words.append({"start": float(segment.start), "end": float(segment.end), "text": segment.text.strip()})
        job.update(stage="Ranking speech highlights", updated=time.time())
        if len(words) < 6:
            raise ValueError("Not enough recognizable speech to find meaningful highlights.")
        highlights = rank_moments(words, duration)
        if not highlights:
            raise ValueError("No complete 15–60 second speech highlights found. Try a longer speaking video.")
        job.update(status="complete", stage="Ready", result={
            "highlights": highlights,
            "duration": round(duration, 1),
            "language": info.language,
            "method": "faster-whisper transcription + speech-content scoring",
            "note": "Suggestions use speech, not visual excitement or a guarantee of engagement."
        }, updated=time.time())
    except ValueError as exc:
        job.update(status="error", stage="Unable to analyze", error=str(exc), updated=time.time())
    except (subprocess.TimeoutExpired, Exception) as exc:
        logger.warning("highlight processing failed category=%s", type(exc).__name__)
        job.update(status="error", stage="Unable to analyze",
                   error="Speech analysis couldn't finish on this server. Try a shorter video or retry later.",
                   updated=time.time())
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        ACTIVE_JOB = None


@router.post("/api/highlights", status_code=202)
async def start_highlights(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    rights_confirmed: str = Form("false"),
    x_clips4u_key: str | None = Header(default=None),
):
    global ACTIVE_JOB
    require_key(x_clips4u_key)
    if rights_confirmed.lower() != "true":
        raise HTTPException(400, "Confirm you own or are allowed to analyze this video")
    cleanup_jobs()
    if ACTIVE_JOB is not None:
        raise HTTPException(429, "Another highlight analysis is underway. Try again shortly.")
    if not file.filename or Path(file.filename).suffix.lower() not in (".mp4", ".mov", ".m4v", ".webm", ".mkv"):
        raise HTTPException(400, "Choose an MP4, MOV, M4V, WEBM or MKV video file")
    folder = tempfile.mkdtemp(prefix="clips4u_analysis_")
    dest = Path(folder) / ("source" + Path(file.filename).suffix.lower())
    # Stream to disk instead of retaining a 90 MB upload in memory.
    try:
        total = 0
        with open(dest, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_BYTES:
                    raise HTTPException(413, "Video is over the 90 MB analysis limit")
                out.write(chunk)
        if total == 0:
            raise HTTPException(400, "Video is empty")
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    finally:
        await file.close()
    job_id = token_urlsafe(20)
    JOBS[job_id] = {"status": "queued", "stage": "Waiting to analyze", "updated": time.time()}
    ACTIVE_JOB = job_id
    background_tasks.add_task(analyze, job_id, folder, str(dest))
    return {"job_id": job_id, "status": "queued"}


@router.get("/api/highlights/{job_id}")
def get_highlights(job_id: str, x_clips4u_key: str | None = Header(default=None)):
    require_key(x_clips4u_key)
    cleanup_jobs()
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "Analysis job has expired or the server restarted. Start a new analysis.")
    return job
