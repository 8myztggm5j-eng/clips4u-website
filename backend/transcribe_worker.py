"""Isolated transcription worker: process exits before video rendering begins.

This prevents CTranslate2/FFmpeg memory from accumulating inside the
long-running FastAPI worker on a small Render instance.
"""
from __future__ import annotations

import gc
import json
import os
import sys
from pathlib import Path


def transcribe(audio: Path, output: Path) -> None:
    from faster_whisper import WhisperModel
    model = WhisperModel(
        "/opt/clips4u-models/tiny", device="cpu",
        compute_type="int8", cpu_threads=1, num_workers=1
    )
    segments, info = model.transcribe(
        str(audio), beam_size=1, best_of=1,
        word_timestamps=True, vad_filter=False,
        condition_on_previous_text=False
    )
    words = []
    for segment in segments:
        if segment.words:
            for word in segment.words:
                if word.word.strip() and word.end is not None and word.start is not None:
                    words.append({
                        "start": round(float(word.start), 3),
                        "end": round(float(word.end), 3),
                        "text": word.word[:100]
                    })
        elif segment.text.strip():
            words.append({
                "start": round(float(segment.start), 3),
                "end": round(float(segment.end), 3),
                "text": segment.text.strip()[:300]
            })
    language = info.language
    del model
    gc.collect()
    result = {"words": words, "language": language}
    # Write atomically so a killed process never leaves a valid-looking partial JSON.
    temp = output.with_suffix(".tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, output)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(2)
    transcribe(Path(sys.argv[1]), Path(sys.argv[2]))
