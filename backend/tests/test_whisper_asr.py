from __future__ import annotations

import sys
from types import SimpleNamespace

from backend.app.adapters import whisper_asr


def test_rechunk_words_merges_across_tiny_whisper_segments():
    words = [
        {"text": " In", "start_time": 0, "end_time": 140},
        {"text": " this", "start_time": 140, "end_time": 300},
        {"text": " tutorial,", "start_time": 300, "end_time": 680},
        {"text": " we", "start_time": 900, "end_time": 960},
        {"text": " are", "start_time": 960, "end_time": 1060},
        {"text": " going", "start_time": 1060, "end_time": 1300},
        {"text": " to", "start_time": 1300, "end_time": 1520},
        {"text": " model", "start_time": 1520, "end_time": 1880},
        {"text": " this", "start_time": 1880, "end_time": 2160},
        {"text": " robot", "start_time": 2160, "end_time": 2560},
        {"text": " in", "start_time": 2560, "end_time": 2980},
        {"text": " Blender.", "start_time": 2980, "end_time": 3460},
        {"text": " Let's", "start_time": 3980, "end_time": 4580},
        {"text": " dive", "start_time": 4580, "end_time": 4800},
        {"text": " in.", "start_time": 4800, "end_time": 5080},
    ]
    utts = whisper_asr.rechunk_words(words)
    assert [u["text"] for u in utts] == [
        "In this tutorial, we are going to model this robot in Blender.",
        "Let's dive in.",
    ]


def test_rechunk_words_splits_on_large_silence():
    words = [
        {"text": " Create", "start_time": 0, "end_time": 400},
        {"text": " a", "start_time": 400, "end_time": 600},
        {"text": " sphere.", "start_time": 600, "end_time": 1000},
        {"text": " Then", "start_time": 3000, "end_time": 3300},
        {"text": " rotate", "start_time": 3300, "end_time": 3700},
        {"text": " it.", "start_time": 3700, "end_time": 4000},
    ]
    utts = whisper_asr.rechunk_words(words)
    assert len(utts) == 2
    assert utts[0]["text"] == "Create a sphere."
    assert utts[1]["text"] == "Then rotate it."


def test_build_utterances_prefers_word_rechunk_over_raw_segments():
    segments = [
        {"text": " And.", "start": 1.0, "end": 1.2, "words": [{"word": " And.", "start": 1.0, "end": 1.2}]},
        {"text": " then", "start": 1.25, "end": 1.4, "words": [{"word": " then", "start": 1.25, "end": 1.4}]},
        {
            "text": " rotate this.",
            "start": 1.45,
            "end": 2.2,
            "words": [
                {"word": " rotate", "start": 1.45, "end": 1.8},
                {"word": " this.", "start": 1.8, "end": 2.2},
            ],
        },
        {
            "text": " Next topic.",
            "start": 4.0,
            "end": 4.8,
            "words": [
                {"word": " Next", "start": 4.0, "end": 4.3},
                {"word": " topic.", "start": 4.3, "end": 4.8},
            ],
        },
    ]
    # Pad with enough dummy segments/words so rechunk path activates.
    for index in range(10):
        t0 = 10.0 + index * 0.2
        segments.append(
            {
                "text": f" word{index}",
                "start": t0,
                "end": t0 + 0.15,
                "words": [{"word": f" word{index}", "start": t0, "end": t0 + 0.15}],
            }
        )
    utts = whisper_asr.build_utterances_from_whisper_segments(segments)
    assert utts[0]["text"].startswith("And. then rotate this.")
    assert any(u["text"] == "Next topic." for u in utts)


def test_load_model_removes_corrupt_cache_and_retries(monkeypatch, tmp_path):
    calls = {"count": 0}
    model = object()
    cache_file = tmp_path / "tiny.pt"
    cache_file.write_bytes(b"bad")

    def load_model(name, device, download_root=None):
        calls["count"] += 1
        assert name == "tiny"
        assert device == "cpu"
        assert download_root == str(tmp_path)
        if calls["count"] == 1:
            raise RuntimeError("SHA256 checksum does not match")
        return model

    fake_whisper = SimpleNamespace(_MODELS={"tiny": "https://example.com/tiny.pt"}, load_model=load_model)
    monkeypatch.setitem(sys.modules, "whisper", fake_whisper)
    monkeypatch.setenv("WHISPER_MODEL", "tiny")
    monkeypatch.setenv("WHISPER_DOWNLOAD_ROOT", str(tmp_path))
    monkeypatch.setattr(whisper_asr, "_MODEL", None)
    monkeypatch.setattr(whisper_asr, "resolve_device", lambda component: SimpleNamespace(selected="cpu"))

    assert whisper_asr._load_model() is model
    assert calls["count"] == 2
    assert not cache_file.exists()
