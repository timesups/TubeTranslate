from __future__ import annotations

import json
import subprocess
from pathlib import Path

from backend.app.adapters import local_video
from backend.app.sources import detect_source


def _fake_probe_payload(*, video="h264", audio="aac", duration="10.0") -> str:
    streams = [{"codec_type": "video", "codec_name": video}]
    if audio is not None:
        streams.append({"codec_type": "audio", "codec_name": audio})
    return json.dumps({"streams": streams, "format": {"duration": duration}})


def test_import_local_video_stream_copies_compatible_h264_aac(monkeypatch, tmp_path):
    task_id = "local-task"
    upload_dir = local_video.uploaded_video_dir(tmp_path, task_id)
    upload_dir.mkdir(parents=True)
    source_file = upload_dir / "demo.mov"
    source_file.write_bytes(b"video")
    commands: list[list[str]] = []

    def fake_run(cmd, check=False, **kwargs):
        commands.append(list(cmd))
        binary = str(cmd[0]).lower()
        if "ffprobe" in binary:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_fake_probe_payload(duration="12.0"), stderr=""
            )
        output = Path(cmd[-1])
        output.write_bytes(b"mp4")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setenv("FFMPEG_PATH", "/opt/bin/ffmpeg")
    monkeypatch.setenv("FFPROBE_PATH", "/opt/bin/ffprobe")
    monkeypatch.setattr(local_video.subprocess, "run", fake_run)
    monkeypatch.setattr(local_video, "_is_usable_output", lambda path, expected: path.exists())

    session, info = local_video.import_local_video(
        f"local://upload/{task_id}?direction=zh-en&filename=demo.mov",
        tmp_path,
        detect_source("local://upload/local-task?direction=zh-en"),
    )

    assert session == tmp_path / "local" / f"demo__{task_id}"
    assert info["title"] == "demo"
    assert info["target_language"] == "en"
    ffmpeg_cmds = [cmd for cmd in commands if "ffmpeg" in str(cmd[0]).lower()]
    assert ffmpeg_cmds
    assert ffmpeg_cmds[0][0] == "/opt/bin/ffmpeg"
    assert "-c" in ffmpeg_cmds[0]
    assert "copy" in ffmpeg_cmds[0]
    assert ffmpeg_cmds[0][-1] == str(session / "media" / "video_source.mp4")
    metadata = json.loads((session / "metadata" / "local_info.json").read_text(encoding="utf-8"))
    assert metadata["original_path"] == str(source_file)


def test_import_local_video_reencodes_when_codec_requires_it(monkeypatch, tmp_path):
    task_id = "reencode-task"
    upload_dir = local_video.uploaded_video_dir(tmp_path, task_id)
    upload_dir.mkdir(parents=True)
    (upload_dir / "demo.mov").write_bytes(b"video")
    commands: list[list[str]] = []

    def fake_run(cmd, check=False, **kwargs):
        commands.append(list(cmd))
        binary = str(cmd[0]).lower()
        if "ffprobe" in binary:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=_fake_probe_payload(video="prores", audio="pcm_s16le", duration="8.0"),
                stderr="",
            )
        Path(cmd[-1]).write_bytes(b"mp4")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setenv("FFMPEG_PATH", "/opt/bin/ffmpeg")
    monkeypatch.setenv("FFPROBE_PATH", "/opt/bin/ffprobe")
    monkeypatch.setattr(local_video.subprocess, "run", fake_run)
    monkeypatch.setattr(local_video, "_is_usable_output", lambda path, expected: path.exists())

    local_video.import_local_video(
        f"local://upload/{task_id}?direction=en-zh&filename=demo.mov",
        tmp_path,
        detect_source(f"local://upload/{task_id}?direction=en-zh"),
    )

    ffmpeg_cmds = [cmd for cmd in commands if "ffmpeg" in str(cmd[0]).lower()]
    assert ffmpeg_cmds
    assert "libx264" in ffmpeg_cmds[0]
    assert "aac" in ffmpeg_cmds[0]


def test_import_local_video_rejects_truncated_cached_output(monkeypatch, tmp_path):
    task_id = "truncated-task"
    upload_dir = local_video.uploaded_video_dir(tmp_path, task_id)
    upload_dir.mkdir(parents=True)
    (upload_dir / "demo.mov").write_bytes(b"source")

    session = tmp_path / "local" / f"demo__{task_id}"
    media = session / "media"
    media.mkdir(parents=True)
    cached = media / "video_source.mp4"
    cached.write_bytes(b"partial")

    probe_calls = {"n": 0}

    def fake_run(cmd, check=False, **kwargs):
        binary = str(cmd[0]).lower()
        if "ffprobe" in binary:
            probe_calls["n"] += 1
            target = str(cmd[-1])
            if target.endswith("demo.mov"):
                payload = _fake_probe_payload(duration="66.0")
            else:
                payload = _fake_probe_payload(duration="28.0")
            return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr="")
        Path(cmd[-1]).write_bytes(b"complete-mp4")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setenv("FFMPEG_PATH", "/opt/bin/ffmpeg")
    monkeypatch.setenv("FFPROBE_PATH", "/opt/bin/ffprobe")
    monkeypatch.setattr(local_video.subprocess, "run", fake_run)

    # After ffmpeg rewrite, treat new file as usable.
    original_usable = local_video._is_usable_output

    def usable(path: Path, expected):
        if path.exists() and path.read_bytes() == b"complete-mp4":
            return True
        return original_usable(path, expected)

    monkeypatch.setattr(local_video, "_is_usable_output", usable)

    local_video.import_local_video(
        f"local://upload/{task_id}?direction=en-zh&filename=demo.mov",
        tmp_path,
        detect_source(f"local://upload/{task_id}?direction=en-zh"),
    )

    assert cached.read_bytes() == b"complete-mp4"
    assert probe_calls["n"] >= 1


def test_import_local_video_keeps_legacy_root_upload_compatibility(monkeypatch, tmp_path):
    task_id = "legacy-task"
    upload_dir = local_video.upload_dir(tmp_path, task_id)
    upload_dir.mkdir(parents=True)
    source_file = upload_dir / "legacy.mp4"
    source_file.write_bytes(b"video")

    def fake_run(cmd, check=False, **kwargs):
        binary = str(cmd[0]).lower()
        if "ffprobe" in binary:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_fake_probe_payload(duration="5.0"), stderr=""
            )
        Path(cmd[-1]).write_bytes(b"mp4")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(local_video.subprocess, "run", fake_run)
    monkeypatch.setattr(local_video, "_is_usable_output", lambda path, expected: path.exists())

    session, info = local_video.import_local_video(
        f"local://upload/{task_id}?direction=en-zh&filename=legacy.mp4",
        tmp_path,
        detect_source("local://upload/legacy-task?direction=en-zh"),
    )

    assert session == tmp_path / "local" / f"legacy__{task_id}"
    assert info["original_path"] == str(source_file)


def test_can_stream_copy_requires_compatible_codecs():
    assert local_video._can_stream_copy({"video_codec": "h264", "audio_codec": "aac"})
    assert local_video._can_stream_copy({"video_codec": "h264", "audio_codec": None})
    assert not local_video._can_stream_copy({"video_codec": "prores", "audio_codec": "aac"})
    assert not local_video._can_stream_copy({"video_codec": "h264", "audio_codec": "pcm_s16le"})
