from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import soundfile as sf

from backend.app.adapters import audio


def test_slot_window_never_crosses_next_cue():
    segment = {"start_time": 1000, "end_time": 1800}
    nxt = {"start_time": 2000, "end_time": 3000}
    start, end = audio._slot_window(segment, nxt, last_end_ms=0)
    assert start == 1000
    assert end == 2000 - audio.GAP_GUARD_MS


def test_slot_window_respects_prior_audio_end():
    segment = {"start_time": 1000, "end_time": 1800}
    start, end = audio._slot_window(segment, None, last_end_ms=1500)
    assert start == 1500
    assert end == 1800


def test_fit_ratio_speeds_up_overflow():
    # 2s TTS into 1s slot → ratio ~0.5
    ratio = audio._fit_ratio(current_sec=2.0, available_sec=1.0, base=1.0)
    assert 0.24 <= ratio <= 0.51


def test_merge_tts_audio_hard_truncates_to_next_cue(tmp_path, monkeypatch):
    session = tmp_path / "session"
    tts_dir = session / "segments" / "tts"
    meta = session / "metadata"
    tts_dir.mkdir(parents=True)
    meta.mkdir(parents=True)

    translation = [
        {"start_time": 0, "end_time": 500, "src": "Also.", "dst": "另外"},
        {"start_time": 800, "end_time": 1500, "src": "Ok.", "dst": "好"},
    ]
    translation_file = meta / "translation.zh.json"
    translation_file.write_text(
        json.dumps({"translation": translation}, ensure_ascii=False),
        encoding="utf-8",
    )

    sr = 16000
    # First clip is intentionally much longer than its slot to next cue.
    long = np.zeros(int(3.0 * sr), dtype=np.float32)
    short = np.zeros(int(0.3 * sr), dtype=np.float32)
    sf.write(str(tts_dir / "0001.wav"), long, sr)
    sf.write(str(tts_dir / "0002.wav"), short, sr)

    monkeypatch.setattr(audio, "_stretch_segment", lambda path, ratio, target_sec, cache_dir: (
        np.zeros(int(target_sec * sr), dtype=np.float32),
        sr,
    ))
    monkeypatch.setattr(audio, "_base_speed_factor", lambda *_: 1.0)

    dubbing, timings = audio.merge_tts_audio(translation_file, tts_dir, session)
    assert dubbing.exists()
    data = json.loads(timings.read_text(encoding="utf-8"))["translation"]

    # First cue must end before the second cue starts (with guard).
    assert data[0]["actual_end_time"] <= data[1]["actual_start_time"]
    assert data[1]["actual_start_time"] >= 800 - 1
    # Timeline must not cascade past the original second cue window by minutes.
    assert data[1]["actual_end_time"] < 2500
