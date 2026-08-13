from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydub import AudioSegment

from ..devices import resolve_device

_MODEL = None

# Regroup Whisper word timestamps into longer utterances. Default segments are often
# 1-word crumbs on tutorial speech; word-gap rechunking is the ASR-stage fix.
RECHUNK_GAP_MS = 700
RECHUNK_TERM_GAP_MS = 280
RECHUNK_HUGE_GAP_MS = 1400
RECHUNK_MAX_DURATION_MS = 12_000
RECHUNK_MAX_WORDS = 48
RECHUNK_MIN_WORDS_FOR_GAP = 3

_TERMINAL_RE = re.compile(r"""[.!?…](?:["'`”’)\]]+)?\s*$""")


def _whisper_cache_file(whisper, name: str, download_root: str | None) -> Path | None:
    if not download_root:
        return None
    model_url = getattr(whisper, "_MODELS", {}).get(name)
    if not model_url:
        return None
    filename = Path(urlparse(model_url).path).name
    if not filename:
        return None
    return Path(download_root).expanduser() / filename


def _is_checksum_error(exc: RuntimeError) -> bool:
    return "sha256 checksum" in str(exc).lower()


def _remove_corrupt_whisper_cache(whisper, name: str, download_root: str | None) -> bool:
    cache_file = _whisper_cache_file(whisper, name, download_root)
    if not cache_file or not cache_file.exists():
        return False
    cache_file.unlink()
    return True


def _load_model():
    global _MODEL
    if _MODEL is not None:
        return _MODEL

    import whisper

    name = os.getenv("WHISPER_MODEL", "large-v3-turbo")
    whisper_device = resolve_device("whisper").selected
    download_root = os.getenv("WHISPER_DOWNLOAD_ROOT") or None
    try:
        _MODEL = whisper.load_model(name, device=whisper_device, download_root=download_root)
    except RuntimeError as exc:
        if not _is_checksum_error(exc):
            raise
        if not _remove_corrupt_whisper_cache(whisper, name, download_root):
            raise
        _MODEL = whisper.load_model(name, device=whisper_device, download_root=download_root)

    return _MODEL


def _to_ms(seconds: float) -> int:
    return int(round(float(seconds) * 1000))


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _convert_words(words: list) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for word in words or []:
        text = word.get("word", "")
        if not str(text).strip():
            continue
        converted.append(
            {
                "text": text,
                "start_time": _to_ms(word.get("start", 0.0)),
                "end_time": _to_ms(word.get("end", 0.0)),
            }
        )
    return converted


def _convert_segments(segments: list) -> list[dict[str, Any]]:
    return [
        {
            "text": seg.get("text", "").strip(),
            "start_time": _to_ms(seg.get("start", 0.0)),
            "end_time": _to_ms(seg.get("end", 0.0)),
            "words": _convert_words(seg.get("words", [])),
        }
        for seg in segments
        if str(seg.get("text", "")).strip()
    ]


def _flatten_words(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = []
    for seg in segments:
        seg_words = seg.get("words") or []
        if seg_words:
            words.extend(seg_words)
            continue
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        # Fallback when word timestamps are missing: keep the segment as one token.
        words.append(
            {
                "text": f" {text}",
                "start_time": int(seg["start_time"]),
                "end_time": int(seg["end_time"]),
            }
        )
    return words


def _join_word_texts(words: list[dict[str, Any]]) -> str:
    # openai-whisper usually prefixes English tokens with a space.
    return "".join(str(word.get("text") or "") for word in words).strip()


def _has_terminal(text: str) -> bool:
    return bool(_TERMINAL_RE.search((text or "").rstrip()))


def rechunk_words(
    words: list[dict[str, Any]],
    *,
    gap_ms: int | None = None,
    term_gap_ms: int | None = None,
    huge_gap_ms: int | None = None,
    max_duration_ms: int | None = None,
    max_words: int | None = None,
    min_words_for_gap: int | None = None,
) -> list[dict[str, Any]]:
    """Build utterances from a flat word stream using silence + punctuation cues."""
    if not words:
        return []

    gap_ms = gap_ms if gap_ms is not None else _env_int("WHISPER_RECHUNK_GAP_MS", RECHUNK_GAP_MS)
    term_gap_ms = (
        term_gap_ms
        if term_gap_ms is not None
        else _env_int("WHISPER_RECHUNK_TERM_GAP_MS", RECHUNK_TERM_GAP_MS)
    )
    huge_gap_ms = (
        huge_gap_ms
        if huge_gap_ms is not None
        else _env_int("WHISPER_RECHUNK_HUGE_GAP_MS", RECHUNK_HUGE_GAP_MS)
    )
    max_duration_ms = (
        max_duration_ms
        if max_duration_ms is not None
        else _env_int("WHISPER_RECHUNK_MAX_DURATION_MS", RECHUNK_MAX_DURATION_MS)
    )
    max_words = (
        max_words if max_words is not None else _env_int("WHISPER_RECHUNK_MAX_WORDS", RECHUNK_MAX_WORDS)
    )
    min_words_for_gap = (
        min_words_for_gap
        if min_words_for_gap is not None
        else _env_int("WHISPER_RECHUNK_MIN_WORDS_FOR_GAP", RECHUNK_MIN_WORDS_FOR_GAP)
    )

    chunks: list[list[dict[str, Any]]] = []
    current = [words[0]]
    for word in words[1:]:
        prev = current[-1]
        gap = int(word["start_time"]) - int(prev["end_time"])
        duration = int(word["end_time"]) - int(current[0]["start_time"])
        count = len(current) + 1
        prev_term = _has_terminal(str(prev.get("text") or ""))

        split = False
        if count > max_words or duration > max_duration_ms:
            split = True
        elif prev_term and gap >= term_gap_ms:
            split = True
        elif gap >= huge_gap_ms:
            split = True
        elif gap >= gap_ms and len(current) >= min_words_for_gap:
            split = True

        if split:
            chunks.append(current)
            current = [word]
        else:
            current.append(word)
    chunks.append(current)

    utterances: list[dict[str, Any]] = []
    for chunk in chunks:
        text = _join_word_texts(chunk)
        if not text:
            continue
        utterances.append(
            {
                "text": text,
                "start_time": int(chunk[0]["start_time"]),
                "end_time": int(chunk[-1]["end_time"]),
                "words": [
                    {
                        "text": str(item.get("text") or ""),
                        "start_time": int(item["start_time"]),
                        "end_time": int(item["end_time"]),
                    }
                    for item in chunk
                ],
            }
        )
    return utterances


def build_utterances_from_whisper_segments(segments: list) -> list[dict[str, Any]]:
    """Convert Whisper segments, preferring word-gap rechunking over raw crumbs."""
    converted = _convert_segments(segments)
    words = _flatten_words(converted)
    # Prefer rechunk whenever we have a real word stream (typically >> segment count).
    if len(words) >= max(8, len(converted)):
        return rechunk_words(words)
    return converted


def recognize_speech(vocals_file: Path, session: Path, language: str) -> Path:
    metadata_dir = session / "metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    output_file = metadata_dir / "asr.json"
    if output_file.exists():
        return output_file

    model = _load_model()
    result = model.transcribe(
        str(vocals_file),
        language=language,
        word_timestamps=True,
        verbose=False,
    )

    utterances = build_utterances_from_whisper_segments(result.get("segments", []))
    if not utterances:
        raise RuntimeError("Whisper did not return any segments.")

    duration_ms = len(AudioSegment.from_file(vocals_file))
    payload = {
        "audio_info": {"duration": duration_ms},
        "result": {
            "text": (result.get("text") or "").strip(),
            "utterances": utterances,
        },
    }
    output_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_file
