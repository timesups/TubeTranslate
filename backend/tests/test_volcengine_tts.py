from __future__ import annotations

import base64
import io
import pytest
from pydub import AudioSegment

from backend.app import database
from backend.app.adapters import volcengine_tts
from backend.app.pipeline import PipelineRunner
from backend.tests.test_settings_and_api import authenticated_client, configure_tmp_runtime


def configure_db(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.sqlite")
    database.init_db()


def test_normalize_tts_provider_defaults_and_rejects_invalid():
    assert database.normalize_tts_provider(None) == "voxcpm"
    assert database.normalize_tts_provider("volcengine") == "volcengine"
    assert database.normalize_tts_provider("azure") == "azure"
    with pytest.raises(ValueError, match="tts_provider must be one of"):
        database.normalize_tts_provider("aws")


def test_create_task_persists_tts_provider(monkeypatch, tmp_path):
    configure_db(monkeypatch, tmp_path)
    task_id = database.create_task(
        "https://www.youtube.com/watch?v=ttprovidertst",
        tts_provider="volcengine",
    )
    task = database.get_task(task_id)
    assert task["tts_provider"] == "volcengine"


def test_resolve_resource_id_for_cloned_speaker():
    assert (
        volcengine_tts._resolve_resource_id("seed-tts-2.0", "S_demo_speaker")
        == "seed-icl-2.0"
    )
    assert (
        volcengine_tts._resolve_resource_id("seed-icl-1.0", "S_demo_speaker")
        == "seed-icl-1.0"
    )
    assert (
        volcengine_tts._resolve_resource_id("seed-tts-2.0", "zh_female_demo")
        == "seed-tts-2.0"
    )


def test_parse_stream_audio_concatenates_chunks_until_end():
    chunk_a = base64.b64encode(b"AAA").decode()
    chunk_b = base64.b64encode(b"BBB").decode()
    payload = "\n".join(
        [
            '{"code":0,"data":"%s"}' % chunk_a,
            '{"code":0,"data":"%s"}' % chunk_b,
            '{"code":20000000,"message":"OK"}',
            '{"code":0,"data":"%s"}' % chunk_a,
        ]
    )
    assert volcengine_tts._parse_stream_audio(payload) == b"AAABBB"


def test_parse_stream_audio_raises_on_error_code():
    with pytest.raises(RuntimeError, match="Volcengine TTS failed \\(40000000\\)"):
        volcengine_tts._parse_stream_audio(
            '{"code":40000000,"message":"invalid speaker"}'
        )


def test_volcengine_settings_mask_and_preserve_secrets(monkeypatch, tmp_path):
    configure_tmp_runtime(monkeypatch, tmp_path)
    database.save_volcengine_tts_settings(
        app_id="app-id",
        access_key="access-secret",
        api_key="api-secret",
        resource_id="seed-tts-2.0",
        speaker="zh_female_demo",
        endpoint="https://openspeech.bytedance.com/api/v3/tts/unidirectional",
        sample_rate="24000",
        speech_rate="0",
        uid="tester",
    )
    client = authenticated_client()

    response = client.get("/api/settings/volcengine-tts")
    assert response.status_code == 200
    body = response.json()
    assert body["access_key"] == "********"
    assert body["api_key"] == "********"
    assert body["has_access_key"] is True
    assert body["has_api_key"] is True
    assert "access-secret" not in str(body)
    assert "api-secret" not in str(body)

    response = client.post(
        "/api/settings/volcengine-tts",
        json={
            "app_id": "app-id-2",
            "access_key": "********",
            "clear_access_key": False,
            "api_key": "********",
            "clear_api_key": False,
            "resource_id": "seed-tts-2.0",
            "speaker": "zh_female_next",
            "endpoint": "",
            "sample_rate": "16000",
            "speech_rate": "10",
            "concurrency": "8",
            "uid": "tester-2",
        },
    )
    assert response.status_code == 200
    settings = database.get_volcengine_tts_settings()
    assert settings["app_id"] == "app-id-2"
    assert settings["access_key"] == "access-secret"
    assert settings["api_key"] == "api-secret"
    assert settings["speaker"] == "zh_female_next"
    assert settings["sample_rate"] == "16000"
    assert settings["speech_rate"] == "10"
    assert settings["concurrency"] == "8"


def test_volcengine_tts_concurrency_rejects_out_of_range(monkeypatch, tmp_path):
    configure_tmp_runtime(monkeypatch, tmp_path)
    client = authenticated_client()
    response = client.post(
        "/api/settings/volcengine-tts",
        json={
            "app_id": "",
            "access_key": "",
            "api_key": "",
            "resource_id": "seed-tts-2.0",
            "speaker": "zh_female_demo",
            "concurrency": "0",
        },
    )
    assert response.status_code == 422


def test_concurrency_from_settings():
    assert volcengine_tts._concurrency_from({"concurrency": "8"}) == 8
    assert volcengine_tts._concurrency_from({"concurrency": "0"}) == volcengine_tts.DEFAULT_CONCURRENCY
    assert volcengine_tts._concurrency_from({}) == volcengine_tts.DEFAULT_CONCURRENCY


def test_generate_tts_respects_concurrency(monkeypatch, tmp_path):
    configure_db(monkeypatch, tmp_path)
    session = tmp_path / "session"
    metadata = session / "metadata"
    metadata.mkdir(parents=True)
    translation = metadata / "translation.json"
    translation.write_text(
        '{"translation":[{"dst":"一"},{"dst":"二"},{"dst":"三"}]}',
        encoding="utf-8",
    )
    active = 0
    max_active = 0
    lock = __import__("threading").Lock()

    def fake_synthesize(text, settings=None):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        __import__("time").sleep(0.05)
        with lock:
            active -= 1
        return AudioSegment.silent(duration=50, frame_rate=24000)

    monkeypatch.setattr(volcengine_tts, "synthesize_speech", fake_synthesize)
    settings = {
        "api_key": "api-secret",
        "speaker": "zh_female_demo",
        "resource_id": "seed-tts-2.0",
        "concurrency": "2",
        "endpoint": "https://example.com",
        "sample_rate": "24000",
        "speech_rate": "0",
        "uid": "tester",
    }
    out = volcengine_tts.generate_tts(translation, session, settings=settings)
    assert len(list(out.glob("*.wav"))) == 3
    assert max_active == 2


def test_create_task_api_accepts_tts_provider(monkeypatch, tmp_path):
    configure_tmp_runtime(monkeypatch, tmp_path)
    client = authenticated_client()
    response = client.post(
        "/api/tasks",
        json={
            "url": "https://www.youtube.com/watch?v=volcttsapi1",
            "tts_provider": "volcengine",
            "audio_mode": "replace",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["tts_provider"] == "volcengine"
    assert body["audio_mode"] == "replace"


def test_split_audio_skips_for_volcengine(monkeypatch, tmp_path):
    configure_db(monkeypatch, tmp_path)
    session = tmp_path / "session"
    session.mkdir()
    task_id = database.create_task(
        "https://www.youtube.com/watch?v=volcsplit01",
        tts_provider="volcengine",
    )
    runner = PipelineRunner(task_id)
    runner.artifacts.session = session
    runner._split_audio(database.get_task(task_id))
    assert runner.artifacts.vocals_dir == session / "segments" / "vocals"
    assert runner.artifacts.vocals_dir.is_dir()


def test_tts_routes_to_volcengine_adapter(monkeypatch, tmp_path):
    configure_db(monkeypatch, tmp_path)
    session = tmp_path / "session"
    metadata = session / "metadata"
    metadata.mkdir(parents=True)
    translation = metadata / "translation.json"
    translation.write_text('{"translation":[{"dst":"你好"}]}', encoding="utf-8")
    out_dir = session / "segments" / "tts"
    called: dict[str, object] = {}

    def fake_generate(translation_file, session_path, progress_callback=None, settings=None):
        called["translation"] = translation_file
        called["session"] = session_path
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "0001.wav").write_bytes(b"RIFF")
        if progress_callback:
            progress_callback(100, "done")
        return out_dir

    monkeypatch.setattr(
        "backend.app.adapters.volcengine_tts.generate_tts",
        fake_generate,
    )

    task_id = database.create_task(
        "https://www.youtube.com/watch?v=volcttsroute01",
        tts_provider="volcengine",
    )
    runner = PipelineRunner(task_id)
    runner.artifacts.session = session
    runner.artifacts.translation_file = translation
    runner._tts(database.get_task(task_id))

    assert called["translation"] == translation
    assert called["session"] == session
    assert runner.artifacts.tts_dir == out_dir


def test_split_tts_text_keeps_short_text():
    assert volcengine_tts._split_tts_text("你好，世界。") == ["你好，世界。"]


def test_split_tts_text_breaks_long_chinese_on_punctuation():
    long_text = ("这是一段很长的中文台词，用来触发火山引擎单次请求字数限制。" * 16).strip()
    parts = volcengine_tts._split_tts_text(long_text)
    assert len(parts) > 1
    assert "".join(parts) == long_text
    assert all(
        len(part) <= volcengine_tts.MAX_TTS_TEXT_CHARS
        and len(part.encode("utf-8")) <= volcengine_tts.MAX_TTS_TEXT_BYTES
        for part in parts
    )


def test_synthesize_speech_chunks_long_text(monkeypatch):
    calls: list[str] = []

    def fake_request(text, settings):
        calls.append(text)
        return AudioSegment.silent(duration=50, frame_rate=24000)

    monkeypatch.setattr(volcengine_tts, "_request_speech", fake_request)
    long_text = ("第一句内容足够长以便切开。" * 20) + ("第二句同样很长需要分段。" * 20)
    segment = volcengine_tts.synthesize_speech(
        long_text,
        {
            "speaker": "zh_female_demo",
            "resource_id": "seed-tts-2.0",
            "endpoint": "https://example.test",
            "sample_rate": "24000",
            "speech_rate": "0",
            "uid": "tester",
            "api_key": "k",
        },
    )
    assert len(calls) > 1
    assert "".join(calls) == long_text
    assert len(segment) == 50 * len(calls)


def test_synthesize_chunk_retries_after_text_limit(monkeypatch):
    calls: list[str] = []

    def fake_request(text, settings):
        calls.append(text)
        if len(text) > 12:
            raise RuntimeError("Volcengine TTS failed (40402003): TTSExceededTextLimit:exceed max limit")
        return AudioSegment.silent(duration=40, frame_rate=24000)

    monkeypatch.setattr(volcengine_tts, "_request_speech", fake_request)
    segment = volcengine_tts._synthesize_chunk(
        "这是一句偏长但需要被强制切开的文本内容。",
        {"speaker": "zh_female_demo", "api_key": "k"},
    )
    assert len(calls) > 1
    assert all(len(text) <= 12 for text in calls if text != calls[0])
    assert len(segment) >= 40


def test_synthesize_speech_uses_api_key_header(monkeypatch, tmp_path):

    configure_db(monkeypatch, tmp_path)
    settings = {
        "api_key": "api-secret",
        "app_id": "",
        "access_key": "",
        "resource_id": "seed-tts-2.0",
        "speaker": "zh_female_demo",
        "endpoint": "https://openspeech.bytedance.com/api/v3/tts/unidirectional",
        "sample_rate": "24000",
        "speech_rate": "0",
        "uid": "tester",
    }

    audio = AudioSegment.silent(duration=200, frame_rate=24000)
    buffer = io.BytesIO()
    audio.export(buffer, format="mp3")
    mp3_bytes = buffer.getvalue()
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200
        text = '{"code":0,"data":"%s"}\n{"code":20000000}' % base64.b64encode(
            mp3_bytes
        ).decode()

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, endpoint, headers=None, json=None):
            captured["endpoint"] = endpoint
            captured["headers"] = headers
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(volcengine_tts.httpx, "Client", FakeClient)
    segment = volcengine_tts.synthesize_speech("你好", settings)
    assert isinstance(segment, AudioSegment)
    assert captured["headers"]["X-Api-Key"] == "api-secret"
    assert captured["headers"]["X-Api-Resource-Id"] == "seed-tts-2.0"
    assert captured["json"]["req_params"]["speaker"] == "zh_female_demo"
