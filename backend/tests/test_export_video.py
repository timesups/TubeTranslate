from __future__ import annotations

import pytest

from backend.app import database
from backend.app.adapters import export_video
from backend.app.pipeline import PipelineRunner
from backend.tests.test_settings_and_api import configure_tmp_runtime


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


def test_export_basename():
    assert export_video.export_basename(task_id="tid1", title="Demo Title") == "Demo_Title__tid1"


def test_resolve_chinese_subtitle_uses_existing_file(tmp_path):
    session, final_video = _session_with_zh_subtitle(tmp_path)
    assert export_video.resolve_chinese_subtitle(final_video, session) == (
        session / "metadata" / "subtitles.zh.srt"
    )


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
