from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app import database
from backend.app.adapters import export_video
from backend.app.pipeline import PipelineRunner
from backend.tests.test_settings_and_api import authenticated_client, configure_tmp_runtime


def configure_db(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.sqlite")
    database.init_db()


def _session_with_zh_subtitle(tmp_path: Path, *, subtitle_text: str = "你好世界") -> tuple[Path, Path]:
    session = tmp_path / "session"
    media = session / "media"
    metadata = session / "metadata"
    media.mkdir(parents=True)
    metadata.mkdir(parents=True)
    final_video = media / "video_final.mp4"
    final_video.write_bytes(b"mp4-bytes")
    (metadata / "subtitles.zh.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n"
        f"{subtitle_text}\n",
        encoding="utf-8",
    )
    return session, final_video


def test_export_final_video_noop_when_output_dir_empty(tmp_path):
    source = tmp_path / "video_final.mp4"
    source.write_bytes(b"mp4")
    assert (
        export_video.export_final_video(
            source,
            task_id="task-1",
            title="Demo",
            output_dir="",
        )
        is None
    )


def test_export_final_video_copies_video_and_matching_chinese_srt(tmp_path):
    session, source = _session_with_zh_subtitle(tmp_path)
    dest_dir = tmp_path / "exports"
    exported = export_video.export_final_video(
        source,
        task_id="abcd1234",
        title="Hello / World?",
        output_dir=str(dest_dir),
        session=session,
    )
    assert exported is not None
    assert exported.video == dest_dir / "Hello_World__abcd1234.mp4"
    assert exported.subtitle == dest_dir / "Hello_World__abcd1234.srt"
    assert exported.video.read_bytes() == b"mp4-bytes"
    assert "你好世界" in exported.subtitle.read_text(encoding="utf-8")


def test_export_final_video_builds_chinese_srt_from_timings_when_missing(tmp_path):
    session = tmp_path / "session"
    media = session / "media"
    metadata = session / "metadata"
    media.mkdir(parents=True)
    metadata.mkdir(parents=True)
    final_video = media / "video_final.mp4"
    final_video.write_bytes(b"mp4")
    (metadata / "timings.json").write_text(
        json.dumps(
            {
                "translation": [
                    {
                        "src": "Hello",
                        "dst": "你好",
                        "src_lang": "en",
                        "dst_lang": "zh",
                        "start_time": 0,
                        "end_time": 1000,
                        "actual_start_time": 0,
                        "actual_end_time": 1000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    dest_dir = tmp_path / "exports"
    exported = export_video.export_final_video(
        final_video,
        task_id="tid1",
        title="Demo",
        output_dir=str(dest_dir),
        session=session,
    )
    assert exported is not None
    assert exported.subtitle == dest_dir / "Demo__tid1.srt"
    assert "你好" in exported.subtitle.read_text(encoding="utf-8")
    assert (session / "metadata" / "subtitles.zh.srt").exists()


def test_output_settings_api_roundtrip(monkeypatch, tmp_path):
    configure_tmp_runtime(monkeypatch, tmp_path)
    client = authenticated_client()

    empty = client.get("/api/settings/output")
    assert empty.status_code == 200
    assert empty.json()["output_dir"] == ""

    saved = client.post("/api/settings/output", json={"output_dir": str(tmp_path / "out")})
    assert saved.status_code == 200
    assert Path(saved.json()["output_dir"]) == (tmp_path / "out").expanduser()
    assert database.get_output_settings()["output_dir"] == saved.json()["output_dir"]


def test_pipeline_exports_video_and_subtitle_on_success(monkeypatch, tmp_path):
    configure_db(monkeypatch, tmp_path)
    task_id = database.create_task("https://www.youtube.com/watch?v=exportvid001")
    database.update_task(task_id, title="Export Demo")
    session, final_video = _session_with_zh_subtitle(tmp_path, subtitle_text="导出字幕")
    export_dir = tmp_path / "auto-out"
    database.save_output_settings(str(export_dir))

    def merge_video(self, task):
        self.artifacts.session = session
        self.artifacts.final_video = final_video

    for name in (
        "_download",
        "_separate",
        "_asr",
        "_asr_fix",
        "_translate",
        "_split_audio",
        "_tts",
        "_merge_audio",
    ):
        monkeypatch.setattr(PipelineRunner, name, lambda self, task: None)
    monkeypatch.setattr(PipelineRunner, "_merge_video", merge_video)
    monkeypatch.setattr("backend.app.pipeline.validate_runtime_device", lambda: None)
    monkeypatch.setattr("backend.app.pipeline.device_plan_summary", lambda: "cpu")

    PipelineRunner(task_id).run()

    task = database.get_task(task_id)
    assert task["status"] == "succeeded"
    exported_video = export_dir / f"Export_Demo__{task_id}.mp4"
    exported_srt = export_dir / f"Export_Demo__{task_id}.srt"
    assert exported_video.exists()
    assert exported_srt.exists()
    assert "导出字幕" in exported_srt.read_text(encoding="utf-8")


def test_resolve_output_dir_rejects_null_bytes():
    with pytest.raises(ValueError, match="null bytes"):
        export_video.resolve_output_dir("D:/bad\x00path")
