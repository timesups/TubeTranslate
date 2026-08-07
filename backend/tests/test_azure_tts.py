from __future__ import annotations

import io

from pydub import AudioSegment

from backend.app import database
from backend.app.adapters import azure_tts
from backend.app.pipeline import PipelineRunner
from backend.tests.test_settings_and_api import authenticated_client, configure_tmp_runtime


def configure_db(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.sqlite")
    database.init_db()


def test_build_ssml_escapes_and_applies_rate():
    ssml = azure_tts.build_ssml(
        '你好 <世界> & "朋友"',
        voice="zh-CN-XiaoxiaoNeural",
        locale="zh-CN",
        speech_rate="10",
    )
    assert "zh-CN-XiaoxiaoNeural" in ssml
    assert "&lt;世界&gt;" in ssml
    assert "&amp;" in ssml
    assert 'rate="+10%"' in ssml


def test_resolve_endpoint_uses_region_or_custom():
    assert (
        azure_tts.resolve_endpoint({"region": "eastus"})
        == "https://eastus.tts.speech.microsoft.com/cognitiveservices/v1"
    )
    assert (
        azure_tts.resolve_endpoint(
            {"endpoint": "https://custom.example/cognitiveservices/v1/"}
        )
        == "https://custom.example/cognitiveservices/v1"
    )


def test_azure_settings_mask_and_preserve_secrets(monkeypatch, tmp_path):
    configure_tmp_runtime(monkeypatch, tmp_path)
    database.save_azure_tts_settings(
        subscription_key="azure-secret-key",
        region="eastasia",
        voice="zh-CN-XiaoxiaoNeural",
        locale="zh-CN",
        endpoint="",
        output_format="audio-24khz-48kbitrate-mono-mp3",
        speech_rate="0",
        concurrency="4",
    )
    client = authenticated_client()
    response = client.get("/api/settings/azure-tts")
    assert response.status_code == 200
    body = response.json()
    assert body["subscription_key"] == "********"
    assert body["has_subscription_key"] is True
    assert body["voice"] == "zh-CN-XiaoxiaoNeural"

    saved = client.post(
        "/api/settings/azure-tts",
        json={
            "subscription_key": "********",
            "region": "japaneast",
            "voice": "zh-CN-YunxiNeural",
            "locale": "zh-CN",
            "endpoint": "",
            "output_format": "audio-24khz-48kbitrate-mono-mp3",
            "speech_rate": "5",
            "concurrency": "6",
        },
    )
    assert saved.status_code == 200
    settings = database.get_azure_tts_settings()
    assert settings["subscription_key"] == "azure-secret-key"
    assert settings["region"] == "japaneast"
    assert settings["voice"] == "zh-CN-YunxiNeural"
    assert settings["concurrency"] == "6"


def test_tts_routes_to_azure_adapter(monkeypatch, tmp_path):
    configure_db(monkeypatch, tmp_path)
    called: dict[str, object] = {}
    session = tmp_path / "session"
    session.mkdir()
    translation = session / "metadata" / "translation.zh.json"
    translation.parent.mkdir(parents=True)
    translation.write_text('{"translation":[]}', encoding="utf-8")
    out_dir = session / "segments" / "tts"
    out_dir.mkdir(parents=True)

    def fake_generate(translation_file, session_path, progress_callback=None):
        called["translation"] = translation_file
        called["session"] = session_path
        return out_dir

    monkeypatch.setattr(
        "backend.app.adapters.azure_tts.generate_tts",
        fake_generate,
    )
    task_id = database.create_task(
        "https://www.youtube.com/watch?v=azurettsroute01",
        tts_provider="azure",
    )
    database.update_task(task_id, session_path=str(session), status="running")
    runner = PipelineRunner(task_id)
    runner.artifacts.session = session
    runner.artifacts.translation_file = translation
    runner._tts(database.get_task(task_id))
    assert called["translation"] == translation
    assert runner.artifacts.tts_dir == out_dir


def test_split_audio_skips_for_azure(monkeypatch, tmp_path):
    configure_db(monkeypatch, tmp_path)
    task_id = database.create_task(
        "https://www.youtube.com/watch?v=azuresplit001",
        tts_provider="azure",
    )
    session = tmp_path / "session"
    session.mkdir()
    database.update_task(task_id, session_path=str(session), status="running")
    runner = PipelineRunner(task_id)
    runner.artifacts.session = session
    runner._split_audio(database.get_task(task_id))
    assert runner.artifacts.vocals_dir == session / "segments" / "vocals"
    assert (session / "segments" / "vocals").is_dir()


def test_synthesize_speech_posts_ssml(monkeypatch, tmp_path):
    settings = {
        "subscription_key": "azure-key",
        "region": "eastasia",
        "voice": "zh-CN-XiaoxiaoNeural",
        "locale": "zh-CN",
        "endpoint": "",
        "output_format": "audio-24khz-48kbitrate-mono-mp3",
        "speech_rate": "0",
        "concurrency": "2",
    }
    audio = AudioSegment.silent(duration=200, frame_rate=24000)
    buffer = io.BytesIO()
    audio.export(buffer, format="mp3")
    mp3_bytes = buffer.getvalue()
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200
        content = mp3_bytes
        text = ""
        reason_phrase = "OK"
        headers = {"content-type": "audio/mpeg"}

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, endpoint, headers=None, content=None):
            captured["endpoint"] = endpoint
            captured["headers"] = headers
            captured["content"] = content
            return FakeResponse()

    monkeypatch.setattr(azure_tts.httpx, "Client", FakeClient)
    segment = azure_tts.synthesize_speech("你好", settings)
    assert isinstance(segment, AudioSegment)
    assert captured["endpoint"] == (
        "https://eastasia.tts.speech.microsoft.com/cognitiveservices/v1"
    )
    assert captured["headers"]["Ocp-Apim-Subscription-Key"] == "azure-key"
    assert b"zh-CN-XiaoxiaoNeural" in captured["content"]
    assert "你好".encode("utf-8") in captured["content"]


def test_synthesize_speech_skips_punctuation_only(monkeypatch):
    called = {"n": 0}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            called["n"] += 1
            raise AssertionError("punctuation-only text should not call Azure")

    monkeypatch.setattr(azure_tts.httpx, "Client", FakeClient)
    segment = azure_tts.synthesize_speech(
        "……！！",
        {"subscription_key": "azure-key", "voice": "zh-CN-XiaoxiaoNeural"},
    )
    assert isinstance(segment, AudioSegment)
    assert len(segment) == azure_tts._SILENT_CLIP_MS
    assert called["n"] == 0


def test_synthesize_speech_skips_tibetan_with_zh_voice(monkeypatch):
    called = {"n": 0}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            called["n"] += 1
            raise AssertionError("Tibetan text should not call zh-CN Azure voice")

    monkeypatch.setattr(azure_tts.httpx, "Client", FakeClient)
    segment = azure_tts.synthesize_speech(
        "ཟ ཟ ཟ ེད ཟ ཟ ཙད ཚའ",
        {
            "subscription_key": "azure-key",
            "voice": "zh-CN-XiaoxiaoNeural",
            "locale": "zh-CN",
        },
    )
    assert isinstance(segment, AudioSegment)
    assert len(segment) == azure_tts._SILENT_CLIP_MS
    assert called["n"] == 0


def test_is_speakable_accepts_chinese_and_english():
    assert azure_tts._is_speakable("你好世界", voice="zh-CN-XiaoxiaoNeural", locale="zh-CN")
    assert azure_tts._is_speakable("Hello world", voice="zh-CN-XiaoxiaoNeural", locale="zh-CN")
    assert not azure_tts._is_speakable("ཟེདཙད", voice="zh-CN-XiaoxiaoNeural", locale="zh-CN")
    assert not azure_tts._is_speakable("……", voice="zh-CN-XiaoxiaoNeural", locale="zh-CN")


def test_request_speech_retries_empty_audio(monkeypatch):
    audio = AudioSegment.silent(duration=200, frame_rate=24000)
    buffer = io.BytesIO()
    audio.export(buffer, format="mp3")
    mp3_bytes = buffer.getvalue()
    attempts = {"n": 0}
    sleeps: list[float] = []

    class FakeResponse:
        def __init__(self, content: bytes):
            self.status_code = 200
            self.content = content
            self.text = ""
            self.reason_phrase = "OK"
            self.headers = {"content-type": "audio/mpeg", "x-request-id": "req-1"}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            attempts["n"] += 1
            if attempts["n"] < 2:
                return FakeResponse(b"")
            return FakeResponse(mp3_bytes)

    monkeypatch.setattr(azure_tts.httpx, "Client", FakeClient)
    monkeypatch.setattr(azure_tts.time, "sleep", sleeps.append)
    segment = azure_tts._request_speech(
        "你好世界",
        {
            "subscription_key": "azure-key",
            "region": "eastasia",
            "voice": "zh-CN-XiaoxiaoNeural",
            "locale": "zh-CN",
            "output_format": "audio-24khz-48kbitrate-mono-mp3",
        },
    )
    assert isinstance(segment, AudioSegment)
    assert attempts["n"] == 2
    assert sleeps == [2.0]


def test_generate_tts_respects_concurrency(monkeypatch, tmp_path):
    translation = tmp_path / "translation.json"
    translation.write_text(
        '{"translation":[{"dst":"一"},{"dst":"二"},{"dst":"三"}]}',
        encoding="utf-8",
    )
    session = tmp_path / "session"
    session.mkdir()
    seen: list[str] = []

    def fake_synthesize(text, settings):
        seen.append(text)
        return AudioSegment.silent(duration=50, frame_rate=24000)

    monkeypatch.setattr(azure_tts, "synthesize_speech", fake_synthesize)
    out = azure_tts.generate_tts(
        translation,
        session,
        settings={"subscription_key": "k", "concurrency": "2", "voice": "zh-CN-XiaoxiaoNeural"},
    )
    assert len(list(out.glob("*.wav"))) == 3
    assert seen == ["一", "二", "三"]
