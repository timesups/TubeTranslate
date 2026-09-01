from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app import package_db, package_tasks
from backend.app.adapters import local_video
from backend.tests.test_settings_and_api import authenticated_client, configure_tmp_runtime


def test_validate_source_dir_requires_existing_directory(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(ValueError, match="does not exist"):
        package_tasks.validate_source_dir(str(missing))


def test_scan_source_dir_finds_videos(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"mp4")
    (tmp_path / "b.txt").write_bytes(b"txt")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "c.mkv").write_bytes(b"mkv")

    items = package_tasks.scan_source_dir(tmp_path, recursive=False, output_suffix="_译制")
    assert len(items) == 1
    assert items[0]["title"] == "a"
    assert str(items[0]["export_path"]).endswith("a_译制.mp4")


def test_export_package_item_writes_next_to_source(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"src")
    final_video = tmp_path / "session" / "media" / "video_final.mp4"
    final_video.parent.mkdir(parents=True)
    final_video.write_bytes(b"final")
    metadata = tmp_path / "session" / "metadata"
    metadata.mkdir(parents=True)
    (metadata / "subtitles.zh.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n你好\n", encoding="utf-8")

    exported_video, exported_subtitle = package_tasks.export_package_item(
        final_video=final_video,
        source_path=source,
        output_suffix="_译制",
        export_subtitle=True,
        session=tmp_path / "session",
    )

    assert exported_video == tmp_path / "clip_译制.mp4"
    assert exported_video.read_bytes() == b"final"
    assert exported_subtitle == tmp_path / "clip_译制.srt"
    assert exported_subtitle is not None
    assert "你好" in exported_subtitle.read_text(encoding="utf-8")


def test_create_and_get_task_package(monkeypatch, tmp_path):
    configure_tmp_runtime(monkeypatch, tmp_path)
    source_dir = tmp_path / "videos"
    source_dir.mkdir()
    (source_dir / "one.mp4").write_bytes(b"x")
    (source_dir / "two.mp4").write_bytes(b"y")

    package_id = package_db.create_package(
        name="batch",
        source_root=str(source_dir),
        output_suffix="_译制",
        direction="en-zh",
        execution_mode="auto",
        audio_mode="replace",
        tts_provider="azure",
        export_subtitle=True,
        continue_on_error=True,
        skip_if_export_exists=False,
        items=[
            {"source_path": str(source_dir / "one.mp4"), "relative_path": "one.mp4", "title": "one"},
            {"source_path": str(source_dir / "two.mp4"), "relative_path": "two.mp4", "title": "two"},
        ],
    )
    package = package_db.get_package(package_id)
    assert package is not None
    assert package["name"] == "batch"
    assert len(package["items"]) == 2
    assert package["items"][0]["stages"][0]["name"] == "download"


def test_scan_task_package_api(monkeypatch, tmp_path):
    configure_tmp_runtime(monkeypatch, tmp_path)
    source_dir = tmp_path / "scan"
    source_dir.mkdir()
    (source_dir / "clip.mp4").write_bytes(b"x")
    client = authenticated_client()

    response = client.post(
        "/api/task-packages/scan",
        json={"source_dir": str(source_dir), "output_suffix": "_译制"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["files"][0]["title"] == "clip"


def test_create_task_package_api_enqueues(monkeypatch, tmp_path):
    configure_tmp_runtime(monkeypatch, tmp_path)
    source_dir = tmp_path / "batch"
    source_dir.mkdir()
    (source_dir / "clip.mp4").write_bytes(b"x")
    enqueued: list[str] = []
    monkeypatch.setattr("backend.app.main.worker.enqueue_package", lambda package_id: enqueued.append(package_id))
    client = authenticated_client()

    response = client.post(
        "/api/task-packages",
        json={
            "source_dir": str(source_dir),
            "name": "My Batch",
            "direction": "en-zh",
            "output_suffix": "_译制",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "My Batch"
    assert len(body["items"]) == 1
    assert enqueued == [body["id"]]


def test_import_path_video_creates_session(monkeypatch, tmp_path):
    from backend.app.sources import detect_source

    source_file = tmp_path / "clip.mp4"
    source_file.write_bytes(b"video")
    workfolder = tmp_path / "workfolder"
    package_id = "pkg-1"
    item_id = "item-1"
    url = f"local://file/{item_id}?direction=en-zh&filename=clip.mp4"
    source = detect_source(url)

    def fake_probe(path: Path):
        return {"video_codec": "h264", "audio_codec": "aac", "duration": 1.0}

    def fake_transcode(source: Path, video_file: Path, *, stream_copy: bool):
        video_file.parent.mkdir(parents=True, exist_ok=True)
        video_file.write_bytes(b"mp4")

    monkeypatch.setattr(local_video, "_probe_media", fake_probe)
    monkeypatch.setattr(local_video, "_transcode_to_mp4", fake_transcode)
    monkeypatch.setattr(
        "backend.app.bilibili.staging.extract_cover_to_session",
        lambda video_file, session: None,
    )

    session, info = local_video.import_path_video(
        source_file,
        workfolder,
        package_id,
        item_id,
        source,
        title="clip",
    )

    assert session.exists()
    assert (session / "media" / "video_source.mp4").exists()
    local_info = json.loads((session / "metadata" / "local_info.json").read_text(encoding="utf-8"))
    assert local_info["original_path"] == str(source_file.resolve())
    assert info["title"] == "clip"
