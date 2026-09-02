from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app import database
from backend.app.adapters import export_video
from backend.app.pipeline import PipelineRunner
from backend.tests.test_settings_and_api import authenticated_client, configure_tmp_runtime


def configure_db(monkeypatch, tmp_path):
    configure_tmp_runtime(monkeypatch, tmp_path)


def _session_with_zh_subtitle(tmp_path, subtitle_text: str = "你好"):
    session = tmp_path / "session"
    media = session / "media"
    metadata = session / "metadata"
    media.mkdir(parents=True)
    metadata.mkdir(parents=True)
    final_video = media / "video_final.mp4"
    final_video.write_bytes(b"mp4")
    (metadata / "subtitles.zh.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n" + subtitle_text + "\n",
        encoding="utf-8",
    )
    return session, final_video


def test_export_final_video_noop_when_output_dir_empty(tmp_path):
    source = tmp_path / "video_final.mp4"
    source.write_bytes(b"mp4")
    assert (
        export_video.export_final_video(
            source,
            task_id="tid",
            title="Demo",
            output_dir="",
        )
        is None
    )


def test_export_final_video_copies_video_without_subtitle(tmp_path):
    session, source = _session_with_zh_subtitle(tmp_path)
    dest_dir = tmp_path / "out"
    exported = export_video.export_final_video(
        source,
        task_id="tid1",
        title="Demo",
        output_dir=str(dest_dir),
        session=session,
    )
    assert exported is not None
    assert exported.video == dest_dir / "Demo__tid1.mp4"
    assert exported.subtitle is None
    assert exported.video.read_bytes() == b"mp4"
    assert not (dest_dir / "Demo__tid1.srt").exists()


def test_export_final_video_does_not_build_subtitle_for_output_dir(tmp_path):
    session = tmp_path / "session"
    media = session / "media"
    metadata = session / "metadata"
    media.mkdir(parents=True)
    metadata.mkdir(parents=True)
    source = media / "video_final.mp4"
    source.write_bytes(b"mp4")
    metadata.joinpath("timings.json").write_text(
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
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    dest_dir = tmp_path / "out"
    exported = export_video.export_final_video(
        source,
        task_id="tid1",
        title="Demo",
        output_dir=str(dest_dir),
        session=session,
    )
    assert exported is not None
    assert exported.subtitle is None
    assert not (dest_dir / "Demo__tid1.srt").exists()
    assert not (session / "metadata" / "subtitles.zh.srt").exists()


def test_export_final_video_writes_bilibili_description(tmp_path):
    session, source = _session_with_zh_subtitle(tmp_path)
    meta = {
        "title": "测试标题",
        "desc": "这是简介\n第二行",
        "tag": "配音,翻译",
        "dynamic": "动态文案",
        "tid": 201,
    }
    (session / "metadata" / "bilibili_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False),
        encoding="utf-8",
    )
    dest_dir = tmp_path / "out"
    exported = export_video.export_final_video(
        source,
        task_id="tid1",
        title="Demo",
        output_dir=str(dest_dir),
        session=session,
    )
    assert exported is not None
    assert exported.description == dest_dir / "Demo__tid1.bilibili.txt"
    text = exported.description.read_text(encoding="utf-8")
    assert "测试标题" in text
    assert "这是简介" in text
    assert "配音,翻译" in text
    saved = json.loads((dest_dir / "Demo__tid1.bilibili.json").read_text(encoding="utf-8"))
    assert saved["title"] == "测试标题"


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


def test_pipeline_exports_only_when_auto_publish_disabled(monkeypatch, tmp_path):
    configure_db(monkeypatch, tmp_path)
    task_id = database.create_task(
        "https://www.youtube.com/watch?v=exportvid001",
        bilibili_auto_publish=False,
    )
    database.update_task(task_id, title="Export Demo")
    session, final_video = _session_with_zh_subtitle(tmp_path, subtitle_text="导出字幕")
    (session / "metadata" / "bilibili_meta.json").write_text(
        json.dumps(
            {
                "title": "导出标题",
                "desc": "导出简介",
                "tag": "配音",
                "dynamic": "",
                "tid": 201,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    export_dir = tmp_path / "auto-out"
    database.save_output_settings(str(export_dir))

    def merge_video(self, task):
        self.artifacts.session = session
        self.artifacts.final_video = final_video

    def bilibili_meta(self, task):
        self.artifacts.session = session
        self.artifacts.final_video = final_video
        self.artifacts.bilibili_meta = session / "metadata" / "bilibili_meta.json"

    for name in (
        "_download",
        "_separate",
        "_asr",
        "_asr_fix",
        "_translate",
        "_split_audio",
        "_tts",
        "_merge_audio",
        "_bilibili_publish",
    ):
        monkeypatch.setattr(PipelineRunner, name, lambda self, task: None)
    monkeypatch.setattr(PipelineRunner, "_merge_video", merge_video)
    monkeypatch.setattr(PipelineRunner, "_bilibili_meta", bilibili_meta)
    monkeypatch.setattr("backend.app.pipeline.validate_runtime_device", lambda: None)
    monkeypatch.setattr("backend.app.pipeline.device_plan_summary", lambda: "cpu")

    PipelineRunner(task_id).run()

    task = database.get_task(task_id)
    assert task["status"] == "succeeded"
    exported_video = export_dir / f"Export_Demo__{task_id}.mp4"
    exported_desc = export_dir / f"Export_Demo__{task_id}.bilibili.txt"
    assert exported_video.exists()
    assert not (export_dir / f"Export_Demo__{task_id}.srt").exists()
    assert exported_desc.exists()
    assert "导出简介" in exported_desc.read_text(encoding="utf-8")


def test_pipeline_skips_export_when_auto_publish_enabled(monkeypatch, tmp_path):
    configure_db(monkeypatch, tmp_path)
    task_id = database.create_task(
        "https://www.youtube.com/watch?v=exportvid002",
        bilibili_auto_publish=True,
    )
    database.update_task(task_id, title="No Export")
    session, final_video = _session_with_zh_subtitle(tmp_path)
    export_dir = tmp_path / "auto-out"
    database.save_output_settings(str(export_dir))

    def merge_video(self, task):
        self.artifacts.session = session
        self.artifacts.final_video = final_video

    def bilibili_meta(self, task):
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
        "_bilibili_publish",
    ):
        monkeypatch.setattr(PipelineRunner, name, lambda self, task: None)
    monkeypatch.setattr(PipelineRunner, "_merge_video", merge_video)
    monkeypatch.setattr(PipelineRunner, "_bilibili_meta", bilibili_meta)
    monkeypatch.setattr("backend.app.pipeline.validate_runtime_device", lambda: None)
    monkeypatch.setattr("backend.app.pipeline.device_plan_summary", lambda: "cpu")

    PipelineRunner(task_id).run()
    assert database.get_task(task_id)["status"] == "succeeded"
    assert list(export_dir.glob("*")) == []


def test_resolve_output_dir_rejects_null_bytes():
    with pytest.raises(ValueError, match="null bytes"):
        export_video.resolve_output_dir("D:/bad\x00path")


def test_normalize_bilibili_auto_publish():
    assert database.normalize_bilibili_auto_publish(None) is True
    assert database.normalize_bilibili_auto_publish(True) is True
    assert database.normalize_bilibili_auto_publish("false") is False
    assert database.normalize_bilibili_auto_publish(0) is False
    with pytest.raises(ValueError):
        database.normalize_bilibili_auto_publish("maybe")


def test_resolve_bilibili_generate_meta_forced_when_publishing():
    assert database.resolve_bilibili_generate_meta(False, bilibili_auto_publish=True) is True
    assert database.resolve_bilibili_generate_meta(False, bilibili_auto_publish=False) is False
    assert database.resolve_bilibili_generate_meta(True, bilibili_auto_publish=False) is True
    assert database.resolve_bilibili_generate_meta(None, bilibili_auto_publish=False) is True


def test_create_task_persists_generate_meta(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.sqlite")
    database.init_db()
    task_id = database.create_task(
        "https://www.youtube.com/watch?v=genmetatest",
        bilibili_auto_publish=False,
        bilibili_generate_meta=False,
    )
    task = database.get_task(task_id)
    assert task["bilibili_auto_publish"] is False
    assert task["bilibili_generate_meta"] is False

    forced = database.create_task(
        "https://www.youtube.com/watch?v=genmetaforce",
        bilibili_auto_publish=True,
        bilibili_generate_meta=False,
    )
    assert database.get_task(forced)["bilibili_generate_meta"] is True


def test_pipeline_skips_bilibili_meta_when_disabled(monkeypatch, tmp_path):
    configure_db(monkeypatch, tmp_path)
    task_id = database.create_task(
        "https://www.youtube.com/watch?v=skipmetavid1",
        bilibili_auto_publish=False,
        bilibili_generate_meta=False,
    )
    database.update_task(task_id, title="Skip Meta")
    session, final_video = _session_with_zh_subtitle(tmp_path)
    export_dir = tmp_path / "skip-meta-out"
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
        "_bilibili_publish",
    ):
        monkeypatch.setattr(PipelineRunner, name, lambda self, task: None)
    monkeypatch.setattr(PipelineRunner, "_merge_video", merge_video)
    monkeypatch.setattr("backend.app.pipeline.validate_runtime_device", lambda: None)
    monkeypatch.setattr("backend.app.pipeline.device_plan_summary", lambda: "cpu")

    called = {"generate": False}

    async def boom(*_args, **_kwargs):
        called["generate"] = True
        raise AssertionError("generate_bilibili_meta should not run")

    monkeypatch.setattr(
        "backend.app.bilibili.deepseek_meta.generate_bilibili_meta",
        boom,
    )

    PipelineRunner(task_id).run()
    task = database.get_task(task_id)
    assert task["status"] == "succeeded"
    assert called["generate"] is False
    assert not (session / "metadata" / "bilibili_meta.json").exists()
    assert (export_dir / f"Skip_Meta__{task_id}.mp4").exists()
    assert not (export_dir / f"Skip_Meta__{task_id}.bilibili.txt").exists()
