from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.adapters import demucs as demucs_adapter


def test_separate_audio_reports_missing_demucs_submodule(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(demucs_adapter, "REPO_ROOT", tmp_path)

    with pytest.raises(RuntimeError, match="missing or incomplete"):
        demucs_adapter.separate_audio(tmp_path / "video.mp4", tmp_path / "session")


def test_separate_audio_reports_incomplete_demucs_submodule(tmp_path, monkeypatch) -> None:
    (tmp_path / "submodule" / "demucs").mkdir(parents=True)
    monkeypatch.setattr(demucs_adapter, "REPO_ROOT", tmp_path)

    with pytest.raises(RuntimeError, match="Download ZIP"):
        demucs_adapter.separate_audio(tmp_path / "video.mp4", tmp_path / "session")


def test_demucs_device_uses_central_resolver(monkeypatch):
    monkeypatch.setattr(demucs_adapter, "resolve_device", lambda component: SimpleNamespace(selected="mps"))
    assert demucs_adapter._device() == "mps"


def test_demucs_progress_uses_model_shift_and_segment_offset():
    progress = demucs_adapter._demucs_progress(
        {
            "models": 2,
            "model_idx_in_bag": 1,
            "shift_idx": 1,
            "segment_offset": 50,
            "audio_length": 100,
        },
        shifts=3,
    )

    assert progress == 75
