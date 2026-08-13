from __future__ import annotations

import asyncio

from backend.app.bilibili import deepseek_meta, staging
from backend.tests.test_settings_and_api import authenticated_client, configure_tmp_runtime


def test_bilibili_partitions_and_settings(monkeypatch, tmp_path):
    configure_tmp_runtime(monkeypatch, tmp_path)
    data_dir = tmp_path / "bilibili"
    data_dir.mkdir()
    monkeypatch.setattr(deepseek_meta, "DATA_DIR", data_dir)
    monkeypatch.setattr(deepseek_meta, "SETTINGS_PATH", data_dir / "settings.json")

    client = authenticated_client()

    partitions = client.get("/api/bilibili/partitions")
    assert partitions.status_code == 200
    assert any(item["tid"] == 201 for item in partitions.json())

    saved = client.post(
        "/api/bilibili/settings",
        json={
            "default_tid": 201,
            "default_tag": "配音",
            "default_copyright": 1,
            "video_dir": str(tmp_path / "staging"),
        },
    )
    assert saved.status_code == 200
    body = saved.json()
    assert "deepseek_api_key" not in body
    assert "deepseek_model" not in body
    assert body["default_copyright"] == 1
    assert body["video_dir"] == str(tmp_path / "staging")

    ready = client.get("/api/bilibili/ready")
    assert ready.status_code == 200
    assert ready.json()["items"] == []


def test_prepare_task_staging_prefers_original_cover(monkeypatch, tmp_path):
    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    monkeypatch.setattr(staging, "staging_dir", lambda: staging_root)
    monkeypatch.setattr(
        staging,
        "resolve_chinese_subtitle",
        lambda final_video, session=None: None,
    )

    session = tmp_path / "session"
    (session / "media").mkdir(parents=True)
    original_cover = session / "media" / "cover_source.jpg"
    original_cover.write_bytes(b"original-cover")

    video = tmp_path / "final.mp4"
    video.write_bytes(b"fake-mp4")

    def fail_extract(*args, **kwargs):
        raise AssertionError("should use original cover, not ffmpeg frame")

    monkeypatch.setattr(staging, "_extract_cover", fail_extract)

    package = staging.prepare_task_staging(
        task_id="task-1",
        title="Demo Title",
        final_video=video,
        session=session,
    )

    assert package.cover is not None
    assert package.cover.read_bytes() == b"original-cover"


def test_prepare_task_staging_copies_media(monkeypatch, tmp_path):
    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    monkeypatch.setattr(staging, "staging_dir", lambda: staging_root)
    monkeypatch.setattr(
        staging,
        "_extract_cover",
        lambda video, cover: cover.write_bytes(b"jpg"),
    )
    monkeypatch.setattr(
        staging,
        "resolve_chinese_subtitle",
        lambda final_video, session=None: None,
    )

    video = tmp_path / "final.mp4"
    video.write_bytes(b"fake-mp4")

    package = staging.prepare_task_staging(
        task_id="task-1",
        title="Demo Title",
        final_video=video,
    )

    assert package.video.exists()
    assert package.cover is not None and package.cover.exists()
    assert package.video.parent == staging_root


def test_generate_meta_uses_openai_settings(monkeypatch, tmp_path):
    configure_tmp_runtime(monkeypatch, tmp_path)
    from backend.app import database

    database.save_openai_settings("https://example.com/v1", "sk-shared", "gpt-test")

    def fake_generate_sync(*, filename: str, subtitle_text: str):
        assert "demo" in filename
        assert "hello" in subtitle_text
        return {
            "title": "测试标题",
            "desc": "测试简介" * 20,
            "tag": ["配音", "翻译"],
            "tag_str": "配音,翻译",
            "dynamic": "动态",
            "raw": {},
        }

    monkeypatch.setattr(deepseek_meta, "_generate_sync", fake_generate_sync)

    result = asyncio.run(
        deepseek_meta.generate_bilibili_meta(
            filename="demo.mp4",
            subtitle_text="hello world",
        )
    )
    assert result["title"] == "测试标题"


def test_append_original_video_link_for_youtube_only():
    desc = "这是 AI 生成的简介。"
    yt = "https://www.youtube.com/watch?v=abcdefghijk"
    out = deepseek_meta.append_original_video_link(desc, yt)
    assert out.startswith(desc)
    assert out.endswith(f"原视频：{yt}")

    # Non-YouTube / empty: unchanged
    assert deepseek_meta.append_original_video_link(desc, None) == desc
    assert (
        deepseek_meta.append_original_video_link(
            desc, "https://www.bilibili.com/video/BV1xx411c7mD"
        )
        == desc
    )

    # Idempotent
    assert deepseek_meta.append_original_video_link(out, yt) == out


def test_generate_bilibili_meta_appends_youtube_link(monkeypatch, tmp_path):
    configure_tmp_runtime(monkeypatch, tmp_path)

    def fake_generate_sync(*, filename: str, subtitle_text: str):
        return {
            "title": "标题",
            "desc": "简介正文",
            "tag": ["配音"],
            "tag_str": "配音",
            "dynamic": "",
            "raw": {},
        }

    monkeypatch.setattr(deepseek_meta, "_generate_sync", fake_generate_sync)
    result = asyncio.run(
        deepseek_meta.generate_bilibili_meta(
            filename="demo.mp4",
            subtitle_text="hello",
            source_url="https://youtu.be/abcdefghijk",
        )
    )
    assert "简介正文" in result["desc"]
    assert "原视频：https://youtu.be/abcdefghijk" in result["desc"]


def test_extract_json_handles_fences_and_raw_newlines():
    raw = (
        "```json\n"
        "{\n"
        '  "title": "修边循环",\n'
        '  "desc": "第一行\n'
        '第二行",\n'
        '  "tag": ["Blender", "教程"],\n'
        '  "dynamic": "分享教程"\n'
        "}\n"
        "```"
    )
    data = deepseek_meta._extract_json(raw)
    assert data["title"] == "修边循环"
    assert "第一行" in data["desc"]
    assert data["tag"] == ["Blender", "教程"]


def test_extract_json_handles_trailing_comma_and_prefix():
    raw = '好的，这是结果：{"title":"A","desc":"B","tag":["t"],"dynamic":"d",}'
    data = deepseek_meta._extract_json(raw)
    assert data["title"] == "A"


def test_fallback_meta_from_subtitle():
    result = deepseek_meta._fallback_meta(
        filename="Fix_edge_loops_in_Blender.mp4",
        subtitle_text="Select the edge loop.\nThen dissolve it.",
    )
    assert "Fix_edge_loops_in_Blender" in result["title"]
    assert "Select the edge loop" in result["desc"]
    assert result["tag_str"]
