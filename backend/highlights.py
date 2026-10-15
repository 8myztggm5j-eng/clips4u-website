"""AI-assisted speech highlight suggestions for authorized local or imported videos.
The model only transcribes speech. Highlight scores use an explainable heuristic;
they are not predictions of reach or evidence of copyright permission.
"""
from __future__ import annotations

import hmac
import json
import sys
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from threading import Timer
from pathlib import Path
from bisect import bisect_left
from secrets import token_urlsafe

from fastapi import APIRouter, BackgroundTasks, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse

router = APIRouter()
logger = logging.getLogger("clips4u.highlights")
MAX_BYTES = 90 * 1024 * 1024
MAX_SECONDS = 600
JOBS: dict[str, dict] = {}
ACTIVE_JOB: str | None = None
EXPIRE_SECONDS = 3600
OUTPUT_SIZES = {"540p": (540, 960), "1080p": (1080, 1920), "4k": (2160, 3840)}
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
            if job.get("output_dir"):
                shutil.rmtree(job["output_dir"], ignore_errors=True)
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


def rank_moments(words: list[dict], duration: float, clip_length: int, clip_count: int) -> list[dict]:
    """Rank fixed-duration speech windows, avoiding repeated or near-identical clips."""
    if duration < clip_length:
        raise ValueError(f"Your source is shorter than {clip_length} seconds. Choose a shorter clip length.")
    if not words:
        return []
    starts = [float(w["start"]) for w in words]
    candidates = []
    seen_starts = set()
    for first in range(0, len(words), 4):
        # Keep requested output lengths exact, even when a sentence ends early.
        start = max(0.0, min(duration - clip_length, starts[first] - 0.5))
        start = round(start, 1)
        if start in seen_starts:
            continue
        seen_starts.add(start)
        end = round(start + clip_length, 1)
        first_index = bisect_left(starts, start)
        last_index = bisect_left(starts, end)
        selection = words[first_index:last_index]
        if not selection:
            continue
        script = " ".join(w["text"].strip() for w in selection).strip()
        lower = script.lower()
        count = len(selection)
        hooks = sum(1 for phrase in HOOKS if re.search(r"\b" + re.escape(phrase) + r"\b", lower))
        impact = sum(1 for phrase in IMPACT if re.search(r"\b" + re.escape(phrase) + r"\b", lower))
        density = count / max(clip_length, 1)
        beginning = selection[0]["text"].strip()
        ending = selection[-1]["text"].strip()
        opening = " ".join(w["text"].strip() for w in selection[:15]).lower()
        hook_at_start = sum(1 for phrase in HOOKS if phrase in opening)
        score = 32 + min(15, count / 5.0) + min(14, hooks * 3.5)
        score += min(12, impact * 3.5) + min(8, script.count("?") * 3 + script.count("!") * 2)
        score += min(8, density * 2.0) + min(9, hook_at_start * 4)
        score += 4 if ending.endswith((".", "!", "?")) else 0
        if beginning and beginning[0].isupper():
            score += 2
        if count < max(12, clip_length // 2):
            score -= 12
        score = max(1, min(97, round(score)))
        reasons = []
        if hook_at_start:
            reasons.append("strong opening hook")
        if hooks:
            reasons.append("attention-grabbing language")
        if impact:
            reasons.append("expressive wording")
        if "?" in script:
            reasons.append("curiosity or question")
        if ending.endswith((".", "!", "?")):
            reasons.append("natural sentence ending")
        if not reasons:
            reasons.append("continuous spoken content")
        candidates.append({
            "start": start, "end": end, "score": score,
            "reason": ", ".join(reasons[:2]).capitalize(),
            "transcript": script[:460],
        })
    candidates.sort(key=lambda x: (-x["score"], x["start"]))
    selected = []

    # First pass: no overlap. This produces truly different clips on long videos.
    for item in candidates:
        if all(max(0, min(item["end"], prior["end"]) -
                   max(item["start"], prior["start"])) <= 1.5 for prior in selected):
            selected.append(item)
            if len(selected) == clip_count:
                break
    # Second pass: allow some overlap if the source lacks enough distinct segments.
    if len(selected) < clip_count:
        for item in candidates:
            if item in selected:
                continue
            if any(abs(item["start"] - prior["start"]) <
                   max(7, clip_length * 0.42) for prior in selected):
                continue
            selected.append(item)
            if len(selected) == clip_count:
                break
    selected.sort(key=lambda x: -x["score"])
    for rank, item in enumerate(selected, 1):
        item["rank"] = rank
    return selected


def subtitle_time(seconds: float) -> str:
    millis = max(0, int(round(seconds * 1000)))
    hours, millis = divmod(millis, 3600000)
    minutes, millis = divmod(millis, 60000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write_caption_file(words: list[dict], start: float, end: float, target: Path) -> bool:
    """Make relative-time SRT subtitles in 2-5 word blocks for readable mobile captions."""
    timed = [w for w in words if float(w["end"]) > start and float(w["start"]) < end]
    lines = []
    group = []
    for word in timed:
        token = str(word["text"]).strip().replace("-->", "").replace("<", "").replace(">", "")
        if not token:
            continue
        group.append((max(0.0, float(word["start"]) - start),
                      min(end - start, float(word["end"]) - start), token))
        span = group[-1][1] - group[0][0]
        if len(group) >= 5 or span >= 2.5 or (len(group) >= 2 and token.endswith((".", "!", "?"))):
            lines.append(group)
            group = []
    if group:
        lines.append(group)
    if not lines:
        return False
    chunks = []
    for idx, group in enumerate(lines, 1):
        start_at = group[0][0]
        end_at = max(start_at + 0.2, group[-1][1])
        phrase = " ".join(item[2] for item in group)
        chunks.append(f"{idx}\n{subtitle_time(start_at)} --> {subtitle_time(end_at)}\n{phrase}\n")
    target.write_text("\n".join(chunks), encoding="utf-8")
    return True


def render_vertical_mp4(source_path: str, target: Path, start: float, end: float,
                        subtitle_path: Path | None, output_quality: str = "540p",
                        video_layout: str = "portrait_blur") -> None:
    """Compose portrait or wide foreground with synchronized blurred backdrop.

    portrait_blur creates a sharp 9:16 center-cropped foreground with a narrow
    blurred border. blur preserves the full wide image; crop fills the screen.
    """
    seconds = round(end - start, 2)
    if not 0 < seconds <= 61:
        raise ValueError("Invalid highlight timestamps")
    if output_quality not in OUTPUT_SIZES:
        raise ValueError("Unsupported output quality")
    width, height = OUTPUT_SIZES[output_quality]
    if video_layout not in ("portrait_blur", "blur", "crop"):
        raise ValueError("Unsupported video layout")
    is_4k = output_quality == "4k"
    style = None
    if subtitle_path:
        factor = width / 540
        style = (
            f"FontName=DejaVu Sans,FontSize={round(19 * factor)},"
            "PrimaryColour=&H00FFFFFF,OutlineColour=&H00101010,"
            f"BorderStyle=1,Outline={round(3 * factor)},Shadow={round(factor)},"
            f"Alignment=2,MarginV={round(130 * factor)}"
        )
    subtitle_filter = (
        f"subtitles=filename='{subtitle_path}':force_style='{style}'"
        if subtitle_path else None
    )

    if video_layout in ("portrait_blur", "blur"):
        # Both foreground and blurred background are copies of the same frame.
        # Blurring at a small size keeps 4K memory demands lower.
        foreground_width = int(width * 0.90) // 2 * 2
        foreground_height = int(height * 0.90) // 2 * 2
        if video_layout == "portrait_blur":
            # Truly portrait front: wide footage loses the sides by center crop.
            foreground_filter = (
                f"scale={foreground_width}:{foreground_height}:"
                "force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={foreground_width}:{foreground_height},setsar=1"
            )
        else:
            foreground_filter = (
                f"scale={foreground_width}:{foreground_height}:"
                "force_original_aspect_ratio=decrease:"
                "force_divisible_by=2:flags=lanczos,setsar=1"
            )
        graph = (
            "[0:v]fps=24,split=2[bgsrc][fgsrc];"
            "[bgsrc]scale=270:480:force_original_aspect_ratio=increase,"
            f"crop=270:480,boxblur=18:2,scale={width}:{height}:flags=bicubic,"
            "setsar=1[bg];"
            f"[fgsrc]{foreground_filter}[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2:format=auto,format=yuv420p"
        )
        if subtitle_filter:
            graph += f",{subtitle_filter}"
        graph += "[vout]"
        filter_args = ["-filter_complex", graph, "-map", "[vout]"]
    else:
        filters = [
            f"scale={width}:{height}:flags=lanczos:force_original_aspect_ratio=increase",
            f"crop={width}:{height}", "setsar=1", "fps=24"
        ]
        if is_4k:
            filters.append("unsharp=5:5:0.35:5:5:0.0")
        if subtitle_filter:
            filters.append(subtitle_filter)
        filter_args = ["-vf", ",".join(filters), "-map", "0:v:0"]

    try:
        cmd = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-filter_threads", "1", "-filter_complex_threads", "1",
            "-y", "-ss", str(start), "-i", source_path, "-t", str(seconds)
        ]
        cmd.extend(filter_args)
        cmd.extend([
            "-map", "0:a:0?", "-c:v", "libx264", "-preset", "ultrafast",
            "-tune", "zerolatency", "-crf", "31" if is_4k else "28",
            "-threads", "1" if is_4k else "2", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(target)
        ])
        result = subprocess.run(cmd, capture_output=True,
                                timeout=480 if is_4k else 240)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Rendering took too long on this server. Try 540p or shorter clips.") from exc
    if result.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        logger.warning("clip rendering failed stage=ffmpeg exit=%s captions=%s quality=%s layout=%s",
                       result.returncode, bool(subtitle_path), output_quality, video_layout)
        raise ValueError("Video rendering failed. Try 540p, fewer clips, or turn captions off.")
    if target.stat().st_size > MAX_BYTES:
        raise ValueError("The rendered clip exceeded 90 MB. Choose a lower quality or shorter length.")


def analyze(job_id: str, folder: str, source_path: str,
            clip_count: int, clip_length: int, captions: bool,
            output_quality: str = "540p", video_layout: str = "portrait_blur") -> None:
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
        job.update(stage="Transcribing speech in a separate low-memory process", updated=time.time())
        # The worker exits immediately after transcription. Only then does FFmpeg
        # render the video, so their memory demands never overlap on Render Free.
        transcript_path = Path(folder) / "transcript.json"
        logger.info("highlights stage=transcription_started job=%s", job_id[:8])
        try:
            speech = subprocess.run([
                sys.executable, "/app/transcribe_worker.py",
                str(audio_path), str(transcript_path)
            ], capture_output=True, timeout=480)
        except subprocess.TimeoutExpired as exc:
            logger.warning("highlights stage=transcription_timeout job=%s", job_id[:8])
            raise ValueError("Speech transcription took too long. Try a shorter source video.") from exc
        if speech.returncode or not transcript_path.exists():
            logger.warning("highlights stage=transcription_failed exit=%s job=%s",
                           speech.returncode, job_id[:8])
            raise ValueError("Speech transcription could not finish on this server. Try a shorter source or retry later.")
        transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
        words = transcript.get("words", [])
        language = transcript.get("language", "unknown")
        transcript_path.unlink(missing_ok=True)
        job.update(stage="Ranking speech highlights", updated=time.time())
        logger.info("highlights stage=transcription_complete job=%s count=%s", job_id[:8], len(words))
        if len(words) < 6:
            raise ValueError("Not enough recognizable speech to find meaningful highlights.")
        highlights = rank_moments(words, duration, clip_length, clip_count)
        if not highlights:
            raise ValueError("No suitable spoken highlights were found at that length. Try shorter clips or a speech-heavy video.")
        # Render the selected moments as real, separate MP4 files. No user
        # interaction or on-device FFmpeg is required to produce the three clips.
        clips = []
        for i, highlight in enumerate(highlights, start=1):
            job.update(stage=f"Rendering {output_quality.upper()} video {i} of {len(highlights)}", updated=time.time())
            logger.info("highlights stage=render_started job=%s clip=%s quality=%s layout=%s",
                        job_id[:8], i, output_quality, video_layout)
            output_path = Path(folder) / f"clip_{i}.mp4"
            subtitle_path = None
            if captions:
                candidate = Path(folder) / f"captions_{i}.srt"
                if write_caption_file(words, highlight["start"], highlight["end"], candidate):
                    subtitle_path = candidate
                else:
                    raise ValueError("No recognizable words to caption. Try again with captions turned off.")
            render_vertical_mp4(source_path, output_path, highlight["start"], highlight["end"],
                                subtitle_path, output_quality, video_layout)
            if subtitle_path:
                subtitle_path.unlink(missing_ok=True)
            logger.info("highlights stage=render_complete job=%s clip=%s", job_id[:8], i)
            clips.append({"rank": i, "filename": f"clips4u-highlight-{i}.mp4",
                          "bytes": output_path.stat().st_size, "captions": captions,
                          "width": OUTPUT_SIZES[output_quality][0],
                          "height": OUTPUT_SIZES[output_quality][1],
                          "video_layout": video_layout})
        # Retain only the short finished clips temporarily; discard source/audio
        # before notifying the iPhone that all exports are available.
        Path(source_path).unlink(missing_ok=True)
        audio_path.unlink(missing_ok=True)
        job["output_dir"] = folder
        logger.info("highlights stage=complete job=%s clips=%s", job_id[:8], len(clips))
        job.update(status="complete", stage=f"{len(clips)} MP4 clips ready", result={
            "highlights": highlights,
            "clips": clips,
            "duration": round(duration, 1),
            "requested_count": clip_count,
            "clip_length_seconds": clip_length,
            "captions": captions,
            "output_quality": output_quality,
            "video_layout": video_layout,
            "width": OUTPUT_SIZES[output_quality][0],
            "height": OUTPUT_SIZES[output_quality][1],
            "language": language,
            "method": "faster-whisper transcription + speech-content scoring",
            "note": "Portrait blur shows a sharp 9:16 center-cropped foreground over matching blurred footage. Wide source videos lose their outer sides. 4K is an upscale, not AI detail restoration."
        }, updated=time.time())
        # Clear temporary exported files even if no more API requests arrive.
        expiration = Timer(EXPIRE_SECONDS + 60, cleanup_jobs)
        expiration.daemon = True
        expiration.start()
    except ValueError as exc:
        job.update(status="error", stage="Unable to analyze", error=str(exc), updated=time.time())
    except (subprocess.TimeoutExpired, Exception) as exc:
        logger.warning("highlight processing failed category=%s", type(exc).__name__)
        job.update(status="error", stage="Unable to analyze",
                   error="Speech analysis couldn't finish on this server. Try a shorter video or retry later.",
                   updated=time.time())
    finally:
        if job.get("status") != "complete":
            shutil.rmtree(folder, ignore_errors=True)
        ACTIVE_JOB = None


@router.post("/api/highlights", status_code=202)
async def start_highlights(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    rights_confirmed: str = Form("false"),
    clip_count: int = Form(3),
    clip_length: int = Form(30),
    captions: bool = Form(True),
    output_quality: str = Form("540p"),
    video_layout: str = Form("portrait_blur"),
    x_clips4u_key: str | None = Header(default=None),
):
    global ACTIVE_JOB
    require_key(x_clips4u_key)
    if rights_confirmed.lower() != "true":
        raise HTTPException(400, "Confirm you own or are allowed to analyze this video")
    if not 1 <= clip_count <= 5:
        raise HTTPException(400, "Choose between 1 and 5 clips")
    if clip_length not in (15, 20, 30, 45, 60):
        raise HTTPException(400, "Choose a clip length of 15, 20, 30, 45 or 60 seconds")
    if output_quality not in OUTPUT_SIZES:
        raise HTTPException(400, "Choose 540p, 1080p or 4k output quality")
    if video_layout not in ("portrait_blur", "blur", "crop"):
        raise HTTPException(400, "Choose portrait blur, full-width blur, or center crop")
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
    background_tasks.add_task(analyze, job_id, folder, str(dest),
                              clip_count, clip_length, captions, output_quality,
                              video_layout)
    return {"job_id": job_id, "status": "queued"}


@router.get("/api/highlights/{job_id}")
def get_highlights(job_id: str, x_clips4u_key: str | None = Header(default=None)):
    require_key(x_clips4u_key)
    cleanup_jobs()
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "Analysis job has expired or the server restarted. Start a new analysis.")
    # Internal disk paths are never part of the API response.
    return {key: value for key, value in job.items() if key not in ("output_dir",)}


@router.get("/api/highlights/{job_id}/clip/{rank}")
def download_highlight(job_id: str, rank: int, x_clips4u_key: str | None = Header(default=None)):
    require_key(x_clips4u_key)
    cleanup_jobs()
    job = JOBS.get(job_id)
    if not job or job.get("status") != "complete":
        raise HTTPException(404, "Video clips aren't ready or have expired.")
    clips = job.get("result", {}).get("clips", [])
    if rank not in [clip["rank"] for clip in clips]:
        raise HTTPException(404, "That clip is not available.")
    folder = job.get("output_dir")
    if not folder:
        raise HTTPException(404, "Video clips have expired.")
    file_path = Path(folder) / f"clip_{rank}.mp4"
    if not file_path.is_file():
        raise HTTPException(404, "Temporary video file has expired.")
    return FileResponse(
        str(file_path), media_type="video/mp4",
        filename=f"clips4u-highlight-{rank}.mp4", content_disposition_type="attachment",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
    )
