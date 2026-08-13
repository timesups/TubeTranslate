from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import soundfile as sf
from pydub import AudioSegment

# ratio < 1 shortens (speeds up). audiostretchy default safe floor is ~0.5.
BASE_FACTOR_MIN = 0.5
BASE_FACTOR_MAX = 1.2
BASE_FACTOR_SAFETY = 0.99
LOCAL_FACTOR_MIN = 0.5
LOCAL_FACTOR_MAX = 1.1
HARD_FIT_MIN_RATIO = 0.5
SPEED_NOOP_EPSILON = 1e-2
GAP_GUARD_MS = 20.0
MIN_SLOT_SEC = 0.04

log = logging.getLogger(__name__)


def split_audio_by_translation(vocals_file: Path, translation_file: Path, session: Path) -> Path:
    output_dir = session / "segments" / "vocals"
    output_dir.mkdir(parents=True, exist_ok=True)
    data = json.loads(translation_file.read_text(encoding="utf-8"))
    audio = AudioSegment.from_file(vocals_file)

    for index, item in enumerate(data["translation"], start=1):
        output_file = output_dir / f"{index:04d}.wav"
        if output_file.exists():
            continue
        start = max(0, int(item["start_time"]) - 80)
        end = min(len(audio), int(item["end_time"]) + 160)
        audio[start:end].export(output_file, format="wav")

    return output_dir


def _audio_duration(file: Path) -> tuple[float, int]:
    import librosa

    y, sr = librosa.load(str(file), sr=None)
    return len(y) / sr, sr


def _base_speed_factor(translation: list[dict], tts_files: list[Path]) -> float:
    cur_total = 0.0
    des_total = 0.0
    for segment, tts_file in zip(translation, tts_files):
        dur, _ = _audio_duration(tts_file)
        cur_total += dur
        des_total += max(0.0, (segment["end_time"] - segment["start_time"]) / 1000.0)
    if cur_total <= 0:
        return 1.0
    factor = des_total / cur_total * BASE_FACTOR_SAFETY
    return max(min(factor, BASE_FACTOR_MAX), BASE_FACTOR_MIN)


def _stretch_segment(audio_file: Path, ratio: float, target_sec: float, cache_dir: Path) -> tuple[np.ndarray, int]:
    import librosa

    if abs(ratio - 1.0) < SPEED_NOOP_EPSILON:
        y, sr = librosa.load(str(audio_file), sr=None)
        return y, sr
    from audiostretchy.stretch import stretch_audio

    out_path = cache_dir / audio_file.name
    # double_range allows down to ~0.25 when needed for pathological TTS overflow.
    stretch_audio(
        str(audio_file),
        str(out_path),
        ratio=ratio,
        double_range=ratio < 0.5 or ratio > 2.0,
    )
    y, sr = librosa.load(str(out_path), sr=None)
    return y[: int(target_sec * sr)], sr


def _local_factor(current_sec: float, base: float, desired_sec: float) -> float:
    first = current_sec * base
    if first <= 1e-3:
        return 1.0
    return max(min(desired_sec / first, LOCAL_FACTOR_MAX), LOCAL_FACTOR_MIN)


def _silence(seconds: float, sample_rate: int) -> np.ndarray:
    return np.zeros(int(max(0.0, seconds) * sample_rate), dtype=np.float32)


def _slot_window(
    segment: dict,
    next_segment: dict | None,
    last_end_ms: float,
) -> tuple[float, float]:
    """Return (real_start_ms, hard_end_ms) that must not spill into the next cue."""
    real_start_ms = max(float(segment["start_time"]), last_end_ms)
    if next_segment is not None:
        hard_end_ms = float(next_segment["start_time"]) - GAP_GUARD_MS
    else:
        hard_end_ms = float(segment["end_time"])
    hard_end_ms = max(real_start_ms, hard_end_ms)
    return real_start_ms, hard_end_ms


def _fit_ratio(current_sec: float, available_sec: float, base: float) -> float:
    """Stretch ratio to fit available_sec. Values < 1 speed up; floor ~0.25 with double_range."""
    if current_sec <= 1e-6:
        return 1.0
    desired_sec = max(MIN_SLOT_SEC, available_sec)
    soft = base * _local_factor(current_sec, base, desired_sec)
    if current_sec * soft <= available_sec + 1e-3:
        return soft
    return max(0.25, available_sec / current_sec)


def merge_tts_audio(translation_file: Path, tts_dir: Path, session: Path) -> tuple[Path, Path]:
    dubbing_file = session / "tmp" / "audio_dubbing.wav"
    timings_file = session / "metadata" / "timings.json"
    cache_dir = session / "segments" / "stretched"
    dubbing_file.parent.mkdir(parents=True, exist_ok=True)
    timings_file.parent.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    if dubbing_file.exists() and timings_file.exists():
        return dubbing_file, timings_file

    data = json.loads(translation_file.read_text(encoding="utf-8"))
    translation = data["translation"]
    tts_files = [tts_dir / f"{i:04d}.wav" for i in range(1, len(translation) + 1)]
    for path in tts_files:
        if not path.exists():
            raise FileNotFoundError(f"Missing TTS segment: {path}")

    _, sample_rate = _audio_duration(tts_files[0])
    base = _base_speed_factor(translation, tts_files)

    final_audio = np.zeros(0, dtype=np.float32)
    last_end_ms = 0.0
    truncated = 0
    for index, (segment, tts_file) in enumerate(zip(translation, tts_files)):
        last_end_ms = final_audio.shape[0] / sample_rate * 1000.0
        next_segment = translation[index + 1] if index + 1 < len(translation) else None
        real_start_ms, hard_end_ms = _slot_window(segment, next_segment, last_end_ms)
        if real_start_ms > last_end_ms:
            final_audio = np.concatenate(
                [final_audio, _silence((real_start_ms - last_end_ms) / 1000.0, sample_rate)]
            )

        available_sec = max(0.0, (hard_end_ms - real_start_ms) / 1000.0)
        current_sec, _ = _audio_duration(tts_file)
        if available_sec < MIN_SLOT_SEC:
            # Previous cue ate the gap; drop this clip rather than cascading further.
            y = np.zeros(0, dtype=np.float32)
            log.warning(
                "TTS segment %s skipped: available slot %.0fms",
                tts_file.name,
                available_sec * 1000.0,
            )
        else:
            ratio = _fit_ratio(current_sec, available_sec, base)
            target_sec = min(current_sec * ratio, available_sec)
            y, _ = _stretch_segment(tts_file, ratio, target_sec, cache_dir)
            max_samples = int(available_sec * sample_rate)
            if len(y) > max_samples:
                y = y[:max_samples]
                truncated += 1

        adjusted_sec = len(y) / sample_rate if sample_rate else 0.0
        real_end_ms = real_start_ms + adjusted_sec * 1000.0
        if len(y):
            final_audio = np.concatenate([final_audio, y])
        segment["actual_start_time"] = int(real_start_ms)
        segment["actual_end_time"] = int(real_end_ms)

    if truncated:
        log.info("Hard-truncated %d TTS segments to prevent timeline drift", truncated)

    sf.write(str(dubbing_file), final_audio, sample_rate)
    timings_file.write_text(
        json.dumps({"translation": translation}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return dubbing_file, timings_file
