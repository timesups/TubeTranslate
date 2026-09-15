from __future__ import annotations

import threading
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app import database, main, package_db, package_execution, package_pipeline, package_tasks, resource_limits
from backend.app.adapters import azure_tts, openai_translate
from backend.tests.test_settings_and_api import authenticated_client, configure_tmp_runtime


def create_package(monkeypatch, tmp_path, count=6, **options):
    configure_tmp_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("PACKAGE_ITEM_CONCURRENCY", "3")
    monkeypatch.setattr(package_pipeline, "WORKFOLDER", tmp_path / "workfolder")
    monkeypatch.setattr(package_pipeline, "validate_runtime_device", lambda: None)
    monkeypatch.setattr(package_pipeline, "device_plan_summary", lambda: "test")
    source = tmp_path / "source"
    source.mkdir()
    items = []
    for i in range(count):
        video = source / f"{i}.mp4"
        video.write_bytes(b"source")
        items.append({"source_path": str(video), "relative_path": video.name, "title": str(i)})
    settings = dict(name="parallel", source_root=str(source), output_suffix="Translate", direction="en-zh",
                    execution_mode="auto", audio_mode="replace", tts_provider="azure", export_subtitle=True,
                    continue_on_error=True, skip_if_export_exists=False, items=items)
    settings.update(options)
    return package_db.create_package(**settings)


def start_package(package_id):
    errors = []

    def run():
        try:
            package_pipeline.run_package(package_id)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, errors


def test_automatic_package_runs_three_items_and_rejects_duplicate_run(monkeypatch, tmp_path):
    package_id = create_package(monkeypatch, tmp_path)
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    started = []
    active = 0
    peak = 0

    def run(self):
        nonlocal active, peak
        assert package_db.claim_package_item(self.item["id"])
        with lock:
            started.append(self.item["id"])
            active += 1
            peak = max(active, peak)
            if len(started) == 3:
                entered.set()
        assert release.wait(5)
        package_db.update_package_item(self.item["id"], status="succeeded", current_stage="done")
        with lock:
            active -= 1

    monkeypatch.setattr(package_pipeline.PackageItemPipelineRunner, "run", run)
    thread, errors = start_package(package_id)
    try:
        assert entered.wait(5)
        assert len(started) == 3
        package_pipeline.run_package(package_id)
        assert len(started) == 3
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert errors == []
    assert peak == 3
    assert len(started) == len(set(started)) == 6
    assert package_db.get_package(package_id)["status"] == "succeeded"


@pytest.mark.parametrize("stage", ["download", "asr", "merge_audio", "merge_video"])
def test_heavy_stage_is_serial_but_translation_can_overlap(monkeypatch, tmp_path, stage):
    package_id = create_package(monkeypatch, tmp_path, count=3)
    package = package_db.get_package(package_id)
    runners = [package_pipeline.PackageItemPipelineRunner(item, package) for item in package["items"]]
    first = threading.Event()
    second = threading.Event()
    translated = threading.Event()
    release = threading.Event()

    def held(_task):
        first.set()
        assert release.wait(5)

    runners[0]._stage_handlers[stage] = held
    runners[1]._stage_handlers[stage] = lambda _task: second.set()
    runners[2]._stage_handlers["translate"] = lambda _task: translated.set()
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(runners[0]._run_package_stage, stage)]
        try:
            assert first.wait(5)
            futures.append(pool.submit(runners[1]._run_package_stage, stage))
            futures.append(pool.submit(runners[2]._run_package_stage, "translate"))
            assert translated.wait(5)
            assert not second.wait(0.1)
        finally:
            release.set()
        for future in futures:
            future.result(timeout=5)
    assert second.is_set()


def install_fake_stages(monkeypatch, tmp_path, started, release):
    def download(self, _task):
        session = tmp_path / "workfolder" / "packages" / self.package["id"] / "items" / self.item["id"]
        media = session / "media"
        media.mkdir(parents=True, exist_ok=True)
        video = media / "video_source.mp4"
        video.write_bytes(b"video")
        self.artifacts.session = session
        self.artifacts.video_file = video
        package_db.update_package_item(self.item["id"], session_path=str(session))
        started.set()
        assert release.wait(5)

    def merge(self, _task):
        final = self.artifacts.session / "media" / "video_final.mp4"
        final.write_bytes(b"final")
        self.artifacts.final_video = final

    cls = package_pipeline.PackageItemPipelineRunner
    monkeypatch.setattr(cls, "_download", download)
    monkeypatch.setattr(cls, "_merge_video", merge)
    for name in ["separate", "asr", "asr_fix", "translate", "split_audio", "tts", "merge_audio"]:
        monkeypatch.setattr(cls, f"_{name}", lambda self, _task: None)


def watch_three_claims(monkeypatch):
    entered = threading.Event()
    lock = threading.Lock()
    count = 0
    original = package_db.claim_package_item

    def claim(item_id):
        nonlocal count
        result = original(item_id)
        if result:
            with lock:
                count += 1
                if count == 3:
                    entered.set()
        return result

    monkeypatch.setattr(package_db, "claim_package_item", claim)
    return entered


def test_pause_is_visible_to_all_workers_and_continue_reuses_outputs(monkeypatch, tmp_path):
    package_id = create_package(monkeypatch, tmp_path)
    started, release = threading.Event(), threading.Event()
    install_fake_stages(monkeypatch, tmp_path, started, release)
    claimed = watch_three_claims(monkeypatch)
    thread, errors = start_package(package_id)
    try:
        assert claimed.wait(5) and started.wait(5)
        assert package_db.request_package_pause(package_id)
        # One worker observing pause must not consume the signal for the others.
        with pytest.raises(database.PauseRequested):
            package_db.raise_if_package_pause_requested(package_id)
        assert package_db.get_package(package_id)["pause_requested"]
        with pytest.raises(package_execution.PackageBusyError):
            package_db.queue_package_for_continue(package_id)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and errors == []
    package = package_db.get_package(package_id)
    assert package["status"] == "paused" and not package["pause_requested"]
    assert sum(item["status"] == "paused" for item in package["items"]) == 3
    assert sum(item["status"] == "pending" for item in package["items"]) == 3
    package_db.queue_package_for_continue(package_id)
    package_pipeline.run_package(package_id)
    assert package_db.get_package(package_id)["status"] == "succeeded"


def test_delete_waits_for_active_workers_before_cleaning_files(monkeypatch, tmp_path):
    package_id = create_package(monkeypatch, tmp_path)
    started, release = threading.Event(), threading.Event()
    install_fake_stages(monkeypatch, tmp_path, started, release)
    claimed = watch_three_claims(monkeypatch)
    client = authenticated_client()
    thread, errors = start_package(package_id)
    root = tmp_path / "workfolder" / "packages" / package_id
    try:
        assert claimed.wait(5) and started.wait(5)
        response = client.delete(f"/api/task-packages/{package_id}")
        assert response.status_code == 200
        assert package_db.get_package(package_id) is None
        assert root.exists()
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and errors == []
    assert not root.exists()
    assert not package_db.log_path(package_id).exists()
    assert not package_execution.is_active(package_id)


@pytest.mark.parametrize("continue_on_error", [False, True])
def test_failure_policy_drains_started_items(monkeypatch, tmp_path, continue_on_error):
    package_id = create_package(monkeypatch, tmp_path, continue_on_error=continue_on_error)
    first_id = package_db.get_package(package_id)["items"][0]["id"]
    barrier = threading.Barrier(3)
    lock = threading.Lock()
    started = []

    failure_recorded = threading.Event()
    original_update = package_db.update_package_item

    def update(item_id, **kwargs):
        result = original_update(item_id, **kwargs)
        if item_id == first_id and kwargs.get("status") == "failed":
            failure_recorded.set()
        return result

    monkeypatch.setattr(package_db, "update_package_item", update)

    def run(self):
        package_db.claim_package_item(self.item["id"])
        with lock:
            started.append(self.item["id"])
            initial = len(started) <= 3
        if initial:
            barrier.wait(timeout=5)
        if self.item["id"] == first_id:
            raise RuntimeError("item failed")
        assert failure_recorded.wait(5)
        package_db.update_package_item(self.item["id"], status="succeeded", current_stage="done")

    monkeypatch.setattr(package_pipeline.PackageItemPipelineRunner, "run", run)
    if continue_on_error:
        package_pipeline.run_package(package_id)
        assert len(started) == 6
        assert package_db.get_package(package_id)["status"] == "partial"
    else:
        with pytest.raises(RuntimeError, match="item failed"):
            package_pipeline.run_package(package_id)
        assert len(started) == 3
        package = package_db.get_package(package_id)
        assert package["status"] == "failed"
        assert sum(item["status"] == "succeeded" for item in package["items"]) == 2


def test_manual_package_pauses_after_one_stage_of_one_video(monkeypatch, tmp_path):
    package_id = create_package(monkeypatch, tmp_path, execution_mode="manual")
    started, release = threading.Event(), threading.Event()
    release.set()
    install_fake_stages(monkeypatch, tmp_path, started, release)
    package_pipeline.run_package(package_id)
    package = package_db.get_package(package_id)
    assert package["status"] == "paused"
    assert package["items"][0]["status"] == "paused"
    assert all(item["status"] == "pending" for item in package["items"][1:])


def test_concurrent_exports_preserve_video_and_subtitle_pairs(monkeypatch, tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"source")
    finals = []
    for i in range(3):
        final = tmp_path / f"final-{i}.mp4"
        final.write_bytes(f"video-{i}".encode())
        final.with_suffix(".srt").write_bytes(f"subtitle-{i}".encode())
        finals.append(final)
    monkeypatch.setattr(package_tasks, "resolve_bilingual_subtitle", lambda final, _: final.with_suffix(".srt"))
    with ThreadPoolExecutor(max_workers=3) as pool:
        outputs = list(pool.map(lambda final: package_tasks.export_package_item(final_video=final, source_path=source), finals))
    assert len(set(outputs)) == 3
    for i, output in enumerate(outputs):
        assert output.read_bytes() == f"video-{i}".encode()
        assert output.with_suffix(".srt").read_bytes() == f"subtitle-{i}".encode()


@pytest.mark.parametrize("pause", [False, True])
def test_translation_request_limit_is_shared_across_videos(monkeypatch, pause):
    from backend.app.sources import detect_source

    release, entered = threading.Event(), threading.Event()
    cancelled = threading.Event()
    lock = threading.Lock()
    active = 0
    peak = 0
    calls = 0

    def completion(**kwargs):
        nonlocal active, peak, calls
        with lock:
            calls += 1
            active += 1
            peak = max(peak, active)
            if active == 2:
                entered.set()
        assert release.wait(5)
        with lock:
            active -= 1
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"dst":"你好世界"}'))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completion)))
    monkeypatch.setattr(openai_translate, "_client", lambda *_: client)
    monkeypatch.setattr(resource_limits, "TRANSLATION_REQUESTS", threading.BoundedSemaphore(2))

    def video():
        def check():
            if cancelled.is_set():
                raise database.PauseRequested("paused")

        with resource_limits.check_context(check):
            return openai_translate.translate_batch(["This is a complete sentence."] * 5,
                detect_source("https://www.youtube.com/watch?v=abcdefghijk"), {}, openai_translate.PreprocessResponse(),
                base_url="https://example.com", api_key="test", model="test", concurrency=4)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(video) for _ in range(2)]
        try:
            assert entered.wait(5)
            assert calls == peak == 2
            if pause:
                cancelled.set()
        finally:
            release.set()
        for future in futures:
            if pause:
                with pytest.raises(database.PauseRequested):
                    future.result(timeout=5)
            else:
                assert len(future.result(timeout=5)) == 5
    assert calls == (2 if pause else 10) and peak == 2


@pytest.mark.parametrize("pause", [False, True])
def test_tts_limit_and_pause_are_shared_by_clip_workers(monkeypatch, tmp_path, pause):
    release, entered, cancelled = threading.Event(), threading.Event(), threading.Event()
    lock = threading.Lock()
    active = peak = calls = 0

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, *args, **kwargs):
            nonlocal active, peak, calls
            with lock:
                calls += 1
                active += 1
                peak = max(peak, active)
                if active == 2:
                    entered.set()
            assert release.wait(5)
            with lock:
                active -= 1
            return SimpleNamespace(status_code=200, headers={"content-type": "audio/wav"}, content=b"x" * 1024)

    monkeypatch.setattr(azure_tts.httpx, "Client", Client)
    monkeypatch.setattr(azure_tts, "_decode_audio", lambda *_: azure_tts.AudioSegment.silent(duration=10))
    monkeypatch.setattr(resource_limits, "TTS_REQUESTS", threading.BoundedSemaphore(2))
    settings = {"subscription_key": "test", "region": "eastus", "voice": "zh-CN-XiaoxiaoNeural", "concurrency": "4"}

    def check():
        if cancelled.is_set():
            raise database.PauseRequested("paused")

    def video(index):
        session = tmp_path / str(index)
        session.mkdir()
        translation = session / "translation.json"
        translation.write_text(json.dumps({"translation": [{"dst": "你好世界"}] * 5}), encoding="utf-8")
        with resource_limits.check_context(check):
            return azure_tts.generate_tts(translation, session, settings=settings)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(video, i) for i in range(2)]
        try:
            assert entered.wait(5)
            assert calls == peak == 2
            if pause:
                cancelled.set()
        finally:
            release.set()
        for future in futures:
            if pause:
                with pytest.raises(database.PauseRequested):
                    future.result(timeout=5)
            else:
                assert len(list(future.result(timeout=5).glob("*.wav"))) == 5
    assert peak == 2 and calls == (2 if pause else 10)


def test_item_claim_is_atomic(monkeypatch, tmp_path):
    package_id = create_package(monkeypatch, tmp_path, count=1)
    item_id = package_db.get_package(package_id)["items"][0]["id"]
    with ThreadPoolExecutor(max_workers=3) as pool:
        claims = list(pool.map(lambda _: package_db.claim_package_item(item_id), range(3)))
    assert sum(claims) == 1


@pytest.mark.parametrize("action", ["continue", "retry-failed"])
def test_active_package_changes_return_conflict(monkeypatch, tmp_path, action):
    package_id = create_package(monkeypatch, tmp_path)
    with package_execution.run(package_id), authenticated_client() as client:
        response = client.post(f"/api/task-packages/{package_id}/{action}")
    assert response.status_code == 409


def test_active_package_files_cannot_be_cleaned(monkeypatch, tmp_path):
    package_id = create_package(monkeypatch, tmp_path)
    with package_execution.run(package_id), pytest.raises(package_execution.PackageBusyError):
        main._cleanup_package_files(package_id)
