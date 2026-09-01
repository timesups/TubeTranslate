from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from backend.app.adapters import ffmpeg


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


def test_video_orientation_uses_height_greater_than_width(monkeypatch):
    def fake_run(cmd, capture_output=False, text=False, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="720,1280\n", stderr="")

    monkeypatch.setattr(ffmpeg.subprocess, "run", fake_run)

    assert ffmpeg.get_video_orientation(Path("video.mp4")) == "portrait"


def test_video_orientation_defaults_to_landscape_when_probe_fails(monkeypatch):
    def fake_run(cmd, capture_output=False, text=False, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="ffprobe failed")

    monkeypatch.setattr(ffmpeg.subprocess, "run", fake_run)

    assert ffmpeg.get_video_orientation(Path("video.mp4")) == "landscape"


def test_subtitle_styles_match_backend_orientation_rules():
    portrait = ffmpeg.subtitle_style_for_orientation("portrait", "Noto Sans CJK SC", "zh")
    landscape = ffmpeg.subtitle_style_for_orientation("landscape", "Noto Sans CJK SC", "zh")

    assert "FontSize=7" in portrait
    assert "MarginV=100" in portrait
    assert "FontSize=14" in landscape
    assert "MarginV=28" in landscape
    assert "PrimaryColour=&H0000FFFF" in landscape
    assert "Outline=1" in landscape


def test_subtitle_styles_use_smaller_size_for_english():
    portrait_en = ffmpeg.subtitle_style_for_orientation("portrait", "Arial", "en")
    landscape_en = ffmpeg.subtitle_style_for_orientation("landscape", "Arial", "en")

    assert "FontSize=6" in portrait_en
    assert "FontSize=11" in landscape_en


def test_subtitle_filter_picks_chinese_font_for_zh_srt(monkeypatch, tmp_path):
    monkeypatch.setattr(ffmpeg, "get_video_orientation", lambda _: "landscape")
    sub_zh = tmp_path / "subtitles.zh.srt"
    sub_zh.write_text("", encoding="utf-8")
    assert "FontName=Noto Sans CJK SC" in ffmpeg.subtitle_filter(tmp_path / "v.mp4", sub_zh, tmp_path)
    sub_en = tmp_path / "subtitles.en.srt"
    sub_en.write_text("", encoding="utf-8")
    assert "FontName=Arial" in ffmpeg.subtitle_filter(tmp_path / "v.mp4", sub_en, tmp_path)


def test_merge_video_skips_subtitles_for_portrait(monkeypatch, tmp_path):
    session = tmp_path / "session"
    metadata_dir = session / "metadata"
    metadata_dir.mkdir(parents=True)
    timings = metadata_dir / "timings.json"
    timings.write_text(
        json.dumps(
            {
                "translation": [
                    {
                        "start_time": 0,
                        "end_time": 1200,
                        "actual_start_time": 0,
                        "actual_end_time": 1200,
                        "src": "Hello there",
                        "dst": "你好",
                        "src_lang": "en",
                        "dst_lang": "zh",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    commands: list[list[str]] = []

    def fake_run(cmd, capture_output=False, text=False, check=False, **kwargs):
        commands.append(cmd)
        return _complete_ffmpeg_run(cmd, ffprobe_stdout="720,1280\n")

    monkeypatch.setattr(ffmpeg.subprocess, "run", fake_run)

    final_video = ffmpeg.merge_video(
        tmp_path / "video.mp4",
        tmp_path / "dubbing.wav",
        tmp_path / "bgm.wav",
        timings,
        session,
    )

    assert final_video == session / "media" / "video_final.mp4"
    final_command = commands[-1]
    assert "-vf" not in final_command
    assert "subtitles=" not in " ".join(final_command)
    assert "-c:v" in final_command
    assert "copy" in final_command
    assert not (session / "metadata" / "subtitles.en.srt").exists()


def test_merge_video_burns_landscape_english_subtitles(monkeypatch, tmp_path):
    session = tmp_path / "session"
    metadata_dir = session / "metadata"
    metadata_dir.mkdir(parents=True)
    timings = metadata_dir / "timings.json"
    timings.write_text(
        json.dumps(
            {
                "translation": [
                    {
                        "start_time": 0,
                        "end_time": 1200,
                        "actual_start_time": 0,
                        "actual_end_time": 1200,
                        "src": "Hello there",
                        "dst": "你好",
                        "src_lang": "en",
                        "dst_lang": "zh",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    commands: list[list[str]] = []
    cwd_values: list[Path | None] = []

    def fake_run(cmd, capture_output=False, text=False, check=False, **kwargs):
        commands.append(cmd)
        cwd_values.append(kwargs.get("cwd"))
        return _complete_ffmpeg_run(cmd)

    monkeypatch.setattr(ffmpeg.subprocess, "run", fake_run)

    final_video = ffmpeg.merge_video(
        tmp_path / "video.mp4",
        tmp_path / "dubbing.wav",
        tmp_path / "bgm.wav",
        timings,
        session,
    )

    assert final_video == session / "media" / "video_final.mp4"
    final_command = commands[-1]
    filter_arg = final_command[final_command.index("-vf") + 1]
    assert filter_arg.startswith("subtitles=filename='metadata/subtitles.en.srt'")
    assert "FontSize=11" in filter_arg
    assert "MarginV=28" in filter_arg
    assert "PrimaryColour=&H0000FFFF" in filter_arg
    assert "Outline=1" in filter_arg
    assert cwd_values[-1] == session.resolve()
    burned = (session / "metadata" / "subtitles.en.srt").read_text(encoding="utf-8")
    assert "Hello there" in burned
    assert "你好" not in burned


def test_merge_video_encoder_chain_auto_portrait_prefers_copy():
    chain = ffmpeg.merge_video_encoder_chain(burn_subtitles=False, preferred="auto")
    assert chain[0] == "copy"


def test_merge_video_encoder_chain_auto_landscape_skips_copy():
    chain = ffmpeg.merge_video_encoder_chain(burn_subtitles=True, preferred="auto")
    assert "copy" not in chain
    assert chain[-1] == "x264"


def test_merge_video_encoder_chain_explicit_copy_with_subtitles_falls_back(monkeypatch):
    monkeypatch.setattr(ffmpeg, "_list_ffmpeg_video_encoders", lambda: frozenset({"h264_nvenc"}))
    chain = ffmpeg.merge_video_encoder_chain(burn_subtitles=True, preferred="copy")
    assert chain[0] == "nvenc"
    assert "copy" not in chain


def test_merge_video_uses_nvenc_when_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("MERGE_VIDEO_ENCODER", "nvenc")
    session = tmp_path / "session"
    metadata_dir = session / "metadata"
    metadata_dir.mkdir(parents=True)
    timings = metadata_dir / "timings.json"
    timings.write_text(
        json.dumps(
            {
                "translation": [
                    {
                        "start_time": 0,
                        "end_time": 1200,
                        "actual_start_time": 0,
                        "actual_end_time": 1200,
                        "src": "Hello there",
                        "dst": "你好",
                        "src_lang": "en",
                        "dst_lang": "zh",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    commands: list[list[str]] = []

    def fake_run(cmd, capture_output=False, text=False, check=False, **kwargs):
        commands.append(cmd)
        return _complete_ffmpeg_run(cmd)

    monkeypatch.setattr(ffmpeg.subprocess, "run", fake_run)

    ffmpeg.merge_video(
        tmp_path / "video.mp4",
        tmp_path / "dubbing.wav",
        tmp_path / "bgm.wav",
        timings,
        session,
    )

    final_command = commands[-1]
    assert "h264_nvenc" in final_command
    assert "-cq" in final_command


def test_merge_video_falls_back_from_nvenc_to_x264(monkeypatch, tmp_path):
    monkeypatch.setenv("MERGE_VIDEO_ENCODER", "nvenc")
    session = tmp_path / "session"
    metadata_dir = session / "metadata"
    metadata_dir.mkdir(parents=True)
    timings = metadata_dir / "timings.json"
    timings.write_text(
        json.dumps(
            {
                "translation": [
                    {
                        "start_time": 0,
                        "end_time": 1200,
                        "actual_start_time": 0,
                        "actual_end_time": 1200,
                        "src": "Hello",
                        "dst": "你好",
                        "src_lang": "en",
                        "dst_lang": "zh",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    commands: list[list[str]] = []
    encode_attempts = {"count": 0}

    def fake_run(cmd, capture_output=False, text=False, check=False, **kwargs):
        commands.append(cmd)
        if Path(cmd[0]).name.startswith("ffprobe") or cmd[0] == "ffprobe":
            return _complete_ffmpeg_run(cmd, ffprobe_stdout="720,1280\n")
        if "h264_nvenc" in cmd:
            encode_attempts["count"] += 1
            return _complete_ffmpeg_run(
                cmd,
                ffprobe_stdout="720,1280\n",
                encode_returncode=1,
                encode_stderr="nvenc failed",
            )
        return _complete_ffmpeg_run(cmd, ffprobe_stdout="720,1280\n")

    monkeypatch.setattr(ffmpeg.subprocess, "run", fake_run)
    monkeypatch.setattr(ffmpeg, "_list_ffmpeg_video_encoders", lambda: frozenset({"h264_nvenc"}))

    final_video = ffmpeg.merge_video(
        tmp_path / "video.mp4",
        tmp_path / "dubbing.wav",
        None,
        timings,
        session,
        replace_audio=True,
    )

    assert final_video.exists()
    assert encode_attempts["count"] == 1
    assert any("libx264" in cmd for cmd in commands)


def test_merge_video_uses_absolute_media_paths_when_cwd_is_session(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    session = Path("workfolder") / "uploader" / "title__videoid"
    metadata_dir = session / "metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    timings = metadata_dir / "timings.json"
    timings.write_text(
        json.dumps(
            {
                "translation": [
                    {
                        "start_time": 0,
                        "end_time": 1200,
                        "actual_start_time": 0,
                        "actual_end_time": 1200,
                        "src": "Hello",
                        "dst": "你好",
                        "src_lang": "en",
                        "dst_lang": "zh",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    commands: list[list[str]] = []
    cwd_values: list[Path | None] = []

    def fake_run(cmd, capture_output=False, text=False, check=False, **kwargs):
        commands.append(cmd)
        cwd_values.append(kwargs.get("cwd"))
        if cmd[0] == "ffprobe":
            return _complete_ffmpeg_run(cmd)
        return _complete_ffmpeg_run(cmd)

    monkeypatch.setattr(ffmpeg.subprocess, "run", fake_run)

    ffmpeg.merge_video(
        session / "media" / "video_source.mp4",
        session / "tmp" / "audio_dubbing.wav",
        session / "media" / "audio_bgm.wav",
        timings,
        session,
    )

    mix_command = next(cmd for cmd in commands if "-filter_complex" in cmd or (len(cmd) > 3 and cmd[-1].endswith("audio_mixed.m4a")))
    final_command = commands[-1]
    assert Path(mix_command[mix_command.index("-i") + 1]).is_absolute()
    if "-filter_complex" in mix_command:
        assert Path(mix_command[mix_command.index("-i", mix_command.index("-i") + 1) + 1]).is_absolute()
    assert Path(mix_command[-1]).is_absolute()
    assert Path(final_command[final_command.index("-i") + 1]).is_absolute()
    assert Path(final_command[final_command.index("-i", final_command.index("-i") + 1) + 1]).is_absolute()
    assert Path(final_command[-1]).is_absolute()
    assert cwd_values[-1] == session.resolve()


def test_split_subtitle_text_breaks_on_punctuation_and_keeps_protected():
    out = ffmpeg.split_subtitle_text("我们今天讨论一下宇宙的边界，那是一个神秘话题；不过别担心，我会详细解释。")
    assert len(out) >= 3
    assert all(len(s) >= 2 for s in out)
    protected = ffmpeg.split_subtitle_text("他说《三体，黑暗森林》是经典，必读。")
    assert any("《三体，黑暗森林》" in s for s in protected)


def test_write_srt_splits_long_sentence_into_multiple_entries(tmp_path):
    session = tmp_path / "session"
    metadata_dir = session / "metadata"
    metadata_dir.mkdir(parents=True)
    timings = metadata_dir / "timings.json"
    timings.write_text(
        json.dumps(
            {
                "translation": [
                    {
                        "start_time": 0,
                        "end_time": 6000,
                        "actual_start_time": 0,
                        "actual_end_time": 6000,
                        "src": "Today we discuss the edge of the universe, a mysterious topic; but do not worry, I will explain in detail",
                        "dst": "我们今天讨论宇宙的边界，那是一个神秘话题；不过别担心，我会详细解释",
                        "src_lang": "en",
                        "dst_lang": "zh",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    srt = ffmpeg.write_srt(timings, session)
    content = srt.read_text(encoding="utf-8")
    assert srt.name == "subtitles.en.srt"
    assert "宇宙" not in content
    blocks = [b for b in content.strip().split("\n\n") if b.strip()]
    assert len(blocks) >= 3
    assert all("-->" in b for b in blocks)


def test_write_srt_burns_english_only(tmp_path):
    session = tmp_path / "session"
    metadata_dir = session / "metadata"
    metadata_dir.mkdir(parents=True)
    timings = metadata_dir / "timings.json"
    timings.write_text(
        json.dumps(
            {
                "translation": [
                    {
                        "start_time": 0,
                        "end_time": 2000,
                        "actual_start_time": 0,
                        "actual_end_time": 2000,
                        "src": "Hello world",
                        "dst": "你好世界",
                        "src_lang": "en",
                        "dst_lang": "zh",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    srt = ffmpeg.write_srt(timings, session)
    content = srt.read_text(encoding="utf-8")
    assert "Hello world" in content
    assert "你好世界" not in content
    assert srt.name == "subtitles.en.srt"


def test_probe_video_size_uses_configured_ffprobe(monkeypatch):
    commands: list[list[str]] = []

    def fake_run(cmd, capture_output=False, text=False, **kwargs):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="1920,1080\n", stderr="")

    monkeypatch.setenv("FFPROBE_PATH", "/opt/bin/ffprobe")
    monkeypatch.setattr(ffmpeg.subprocess, "run", fake_run)

    assert ffmpeg.probe_video_size(Path("video.mp4")) == (1920, 1080)
    assert commands[0][0] == "/opt/bin/ffprobe"
