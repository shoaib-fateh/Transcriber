#!/usr/bin/env python3
"""Minimal offline SRT transcription worker using the local Faster-Whisper Small model."""
from __future__ import annotations

import re
import sys
import textwrap
import wave
from pathlib import Path

MODEL_DIR = Path(r"D:\DAVYAN\models\faster-whisper-small")
LINE_WIDTH = 42
SENTENCE_FALLBACK_SECONDS = 9.5
SENTENCE_FALLBACK_CHARS = 140


def srt_time(seconds: float) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1_000)
    return f"{hours:02}:{minutes:02}:{secs:02},{milliseconds:03}"


def get_duration(audio_path: Path) -> float:
    try:
        with wave.open(str(audio_path), "rb") as wav_file:
            return wav_file.getnframes() / max(1, wav_file.getframerate())
    except (wave.Error, OSError):
        return 0.0


def joined_text(words: list[dict]) -> str:
    return "".join(word["text"] for word in words).strip()


def wrap_caption(text: str) -> str:
    lines = textwrap.wrap(
        text,
        width=LINE_WIDTH,
        break_long_words=False,
        break_on_hyphens=False,
        replace_whitespace=True,
        drop_whitespace=True,
    )
    return "\n".join(lines) if lines else text


def sentence_finished(text: str) -> bool:
    return bool(re.search(r"[.!?…][\"'’”)}\]]*$", text.strip()))


def make_entries(words: list[dict], duration: float, mode: str) -> list[tuple[float, float, str]]:
    """Group word-level timings into short captions or complete-sentence cues."""
    entries: list[tuple[float, float, str]] = []

    def add_entry(group: list[dict]) -> None:
        if not group:
            return
        text = joined_text(group)
        start = max(0.0, group[0]["start"])
        end = min(duration or group[-1]["end"], max(group[-1]["end"], start + 0.08))
        if text and end > start:
            entries.append((start, end, wrap_caption(text)))

    if mode in {"2", "3", "4"}:
        # First form natural phrases, respecting punctuation and pauses. Then
        # split each phrase into the requested maximum, balancing a final
        # single word across the previous caption whenever possible.
        phrases: list[list[dict]] = []
        current: list[dict] = []
        for word in words:
            if current:
                gap = word["start"] - current[-1]["end"]
                if gap >= 0.8 and len(current) >= 2 or gap >= 1.35:
                    phrases.append(current)
                    current = []
            current.append(word)
            if sentence_finished(joined_text(current)):
                phrases.append(current)
                current = []
        if current:
            phrases.append(current)

        max_words = int(mode)
        for phrase in phrases:
            offset = 0
            while offset < len(phrase):
                remaining = len(phrase) - offset
                take = max_words
                if remaining > max_words and remaining - max_words == 1:
                    take = max_words - 1
                group = phrase[offset:offset + take]
                add_entry(group)
                offset += take
    else:
        current: list[dict] = []
        for word in words:
            if current:
                gap = word["start"] - current[-1]["end"]
                current_text = joined_text(current)
                elapsed = word["end"] - current[0]["start"]
                too_long = elapsed >= SENTENCE_FALLBACK_SECONDS and len(current_text) >= SENTENCE_FALLBACK_CHARS
                has_clause_break = bool(re.search(r"[,;:][\"'’”)}\]]*$", current_text.strip()))
                if gap >= 1.5 or (too_long and has_clause_break):
                    add_entry(current)
                    current = []
            current.append(word)
            if sentence_finished(joined_text(current)):
                add_entry(current)
                current = []
        if current:
            add_entry(current)

    # Clip any timing overlap from imperfect word alignment.
    fixed: list[tuple[float, float, str]] = []
    for index, (start, end, text) in enumerate(entries):
        if index + 1 < len(entries):
            next_start = entries[index + 1][0]
            if end > next_start > start:
                end = next_start
        if end > start:
            fixed.append((start, end, text))
    return fixed


def write_srt(entries: list[tuple[float, float, str]], path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="\n") as output:
        for index, (start, end, text) in enumerate(entries, start=1):
            output.write(f"{index}\n{srt_time(start)} --> {srt_time(end)}\n{text}\n\n")


def emit_progress(percent: int, label: str) -> None:
    print(f"PROGRESS|{max(0, min(99, percent))}|{label}", flush=True)


def main() -> int:
    lines = [sys.stdin.readline().strip() for _ in range(2)]
    audio_path = Path(lines[0].strip().strip('"')) if lines[0] else None
    mode = lines[1].strip().lower() if lines[1] else "3"
    if mode not in {"2", "3", "4", "sentence"}:
        mode = "3"

    if audio_path is None or not audio_path.is_file():
        print("ERROR: The selected audio file could not be found.", flush=True)
        return 1

    if not MODEL_DIR.is_dir():
        print(f"ERROR: Model folder not found: {MODEL_DIR}", flush=True)
        return 1
    required = ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt")
    missing = [name for name in required if not (MODEL_DIR / name).is_file()]
    if missing:
        print("ERROR: Model folder is missing: " + ", ".join(missing), flush=True)
        return 1

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print(
            "ERROR: faster-whisper is not installed in this Python environment. "
            "Install it with: python -m pip install faster-whisper",
            flush=True,
        )
        return 1

    duration = get_duration(audio_path)
    if duration > 0:
        print(f"Audio duration: {duration:.2f} seconds", flush=True)
    print("Loading faster-whisper-small in CPU INT8 mode…", flush=True)
    emit_progress(12, "Loading model")
    try:
        model = WhisperModel(
            str(MODEL_DIR),
            device="cpu",
            compute_type="int8",
            cpu_threads=4,
            num_workers=1,
        )
    except Exception as exc:
        print(f"ERROR: Could not load the local Faster-Whisper model: {type(exc).__name__}: {exc}", flush=True)
        return 1

    print("Model loaded. Transcribing with word timestamps…", flush=True)
    emit_progress(32, "Transcribing audio")
    try:
        segments, info = model.transcribe(
            str(audio_path),
            language=None,
            beam_size=3,
            word_timestamps=True,
            vad_filter=False,
            condition_on_previous_text=True,
            temperature=0.0,
        )
        print(
            f"Detected language: {info.language} ({info.language_probability:.0%})",
            flush=True,
        )
        words: list[dict] = []
        for segment in segments:
            if segment.words:
                for word in segment.words:
                    if word.start is None or word.end is None or not word.word.strip():
                        continue
                    start = max(0.0, float(word.start))
                    end = max(start, float(word.end))
                    if end > start:
                        words.append({"start": start, "end": end, "text": word.word})
            if duration > 0:
                percent = 32 + int(58 * min(1.0, float(segment.end) / duration))
                emit_progress(percent, "Transcribing audio")
        if not words:
            print("ERROR: No word timestamps were returned for this audio.", flush=True)
            return 1
        media_duration = duration or max(word["end"] for word in words)
        print("Grouping captions…", flush=True)
        emit_progress(93, "Formatting captions")
        entries = make_entries(words, media_duration, mode)
        output_path = audio_path.with_suffix(".srt")
        write_srt(entries, output_path)
        print(f"Created {len(entries)} timed subtitle cues", flush=True)
        print(f"SRT saved to: {output_path}", flush=True)
        emit_progress(99, "Finishing SRT")
        return 0
    except Exception as exc:
        print(f"ERROR while transcribing: {type(exc).__name__}: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
