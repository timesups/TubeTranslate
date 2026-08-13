from __future__ import annotations

from pathlib import Path

from backend.app import config


def test_ffmpeg_bin_directories_from_exe_env(monkeypatch, tmp_path):
    bin_dir = tmp_path / "ffmpeg" / "bin"
    bin_dir.mkdir(parents=True)
    exe = bin_dir / "ffmpeg.exe"
    exe.write_bytes(b"x")
    (bin_dir / "avutil-60.dll").write_bytes(b"dll")
    (bin_dir / "avcodec-62.dll").write_bytes(b"dll")

    monkeypatch.setenv("FFMPEG_PATH", str(exe))
    monkeypatch.delenv("FFPROBE_PATH", raising=False)
    monkeypatch.setenv("PATH", "")

    dirs = config._ffmpeg_bin_directories()
    assert bin_dir in dirs


def test_ensure_ffmpeg_dll_search_path_registers_windows_dir(monkeypatch, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ffmpeg.exe").write_bytes(b"x")
    (bin_dir / "avutil-60.dll").write_bytes(b"dll")
    (bin_dir / "avcodec-62.dll").write_bytes(b"dll")

    monkeypatch.setenv("FFMPEG_PATH", str(bin_dir / "ffmpeg.exe"))
    monkeypatch.delenv("FFPROBE_PATH", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    added: list[str] = []

    def fake_add(path: str):
        added.append(path)

    monkeypatch.setattr(config.os, "add_dll_directory", fake_add, raising=False)
    monkeypatch.setattr(config.os, "name", "nt")

    registered = config.ensure_ffmpeg_dll_search_path()
    assert str(bin_dir) in registered
    assert str(bin_dir) in added
    assert str(bin_dir) in config.os.environ["PATH"].split(config.os.pathsep)
