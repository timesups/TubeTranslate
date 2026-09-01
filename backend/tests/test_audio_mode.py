from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.app import database
from backend.app.adapters import ffmpeg
from backend.app.pipeline import PipelineRunner


def _complete_ffmpeg_run(
    cmd: list[str],
    *,
    ffprobe_stdout: str = "1920,1080\n",
    encoders_stdout: str = "",
    encode_returncode: int = 0,
    encode_stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    binary = Path(cmd[0]).name
    if binary.startswith("ffprobe") or cmd[0] == "ffprobe":
        return subprocess.CompletedProcess(cmd, 0, stdout=ffprobe_stdout, stderr="")
    if "-encoders" in cmd:
        return subprocess.CompletedProcess(cmd, 0, stdout=encoders_stdout, stderr="")
    output = Path(cmd[-1])
    if encode_returncode == 0 and output.suffix.lower() == ".mp4":
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"mp4")
    return subprocess.CompletedProcess(
        cmd,
        encode_returncode,
        stdout="",
        stderr=encode_stderr,
    )


@pytest.fixture(autouse=True)
def isolated_merge_video_encoders(monkeypatch):
    if hasattr(ffmpeg._list_ffmpeg_video_encoders, "cache_clear"):
        ffmpeg._list_ffmpeg_video_encoders.cache_clear()
    monkeypatch.setattr(ffmpeg, "_list_ffmpeg_video_encoders", lambda: frozenset())
    yield


def configure_db(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.sqlite")
    database.init_db()


def test_normalize_audio_mode_defaults_and_rejects_invalid():
    assert database.normalize_audio_mode(None) == "replace"
    assert database.normalize_audio_mode("replace") == "replace"
    with pytest.raises(ValueError, match="audio_mode must be one of"):
        database.normalize_audio_mode("mute")


def test_create_task_persists_audio_mode(monkeypatch, tmp_path):
    configure_db(monkeypatch, tmp_path)
    task_id = database.create_task(
        "https://www.youtube.com/watch?v=audiomodetst",
        audio_mode="replace",
    )
    task = database.get_task(task_id)
    assert task["audio_mode"] == "replace"


def test_separate_replace_mode_extracts_source_audio(monkeypatch, tmp_path):
    configure_db(monkeypatch, tmp_path)
    session = tmp_path / "session"
    (session / "media").mkdir(parents=True)
    video = session / "media" / "video_source.mp4"
    video.write_bytes(b"mp4")
    extracted = session / "media" / "audio_vocals.wav"

    task_id = database.create_task(
        "https://www.youtube.com/watch?v=replaceaudio",
        audio_mode="replace",
    )
    runner = PipelineRunner(task_id)
    runner.artifacts.session = session
    runner.artifacts.video_file = video

    def fake_extract(video_file, session_path):
        assert video_file == video
        assert session_path == session
        extracted.write_bytes(b"wav")
        return extracted

    monkeypatch.setattr("backend.app.adapters.ffmpeg.extract_source_audio", fake_extract)
    runner._separate(database.get_task(task_id))

    assert runner.artifacts.vocals_file == extracted
    assert runner.artifacts.bgm_file is None


def test_merge_video_replace_audio_skips_bgm_mix(monkeypatch, tmp_path):
    session = tmp_path / "session"
    metadata_dir = session / "metadata"
    metadata_dir.mkdir(parents=True)
    timings = metadata_dir / "timings.json"
    timings.write_text(
        '{"translation":[{"start_time":0,"end_time":1000,"actual_start_time":0,"actual_end_time":1000,"zh":"你好"}]}',
        encoding="utf-8",
    )
    commands: list[list[str]] = []

    def fake_run(cmd, capture_output=False, text=False, check=False, **kwargs):
        commands.append(cmd)
        return _complete_ffmpeg_run(cmd, ffprobe_stdout="1280,720\n")

    monkeypatch.setattr(ffmpeg.subprocess, "run", fake_run)
    monkeypatch.setattr(ffmpeg, "get_video_orientation", lambda _: "landscape")

    ffmpeg.merge_video(
        tmp_path / "video.mp4",
        tmp_path / "dubbing.wav",
        None,
        timings,
        session,
        replace_audio=True,
    )

    mix_command = commands[0]
    assert "amix" not in " ".join(mix_command)
    assert mix_command.count("-i") == 1
