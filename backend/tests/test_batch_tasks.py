from __future__ import annotations

from backend.app import database, main
from backend.tests.test_settings_and_api import authenticated_client, configure_tmp_runtime


def test_create_tasks_batch_creates_multiple_and_reports_partial_errors(monkeypatch, tmp_path):
    configure_tmp_runtime(monkeypatch, tmp_path)
    enqueued: list[str] = []
    monkeypatch.setattr(main.worker, "enqueue", lambda task_id: enqueued.append(task_id))
    client = authenticated_client()

    response = client.post(
        "/api/tasks/batch",
        json={
            "urls": [
                "https://www.youtube.com/watch?v=batchvid001",
                "https://www.youtube.com/watch?v=batchvid002",
                "https://example.com/not-supported",
                "https://youtu.be/batchvid001",
            ],
            "execution_mode": "manual",
            "audio_mode": "replace",
            "tts_provider": "azure",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["created"]) == 2
    assert len(body["existing"]) == 1
    assert len(body["errors"]) == 1
    assert body["errors"][0]["url"] == "https://example.com/not-supported"
    assert sorted(enqueued) == ["batchvid001", "batchvid002"]
    assert body["created"][0]["task"]["execution_mode"] == "manual"
    assert body["created"][0]["task"]["audio_mode"] == "replace"
    assert body["created"][0]["task"]["tts_provider"] == "azure"


def test_create_tasks_batch_marks_already_existing(monkeypatch, tmp_path):
    configure_tmp_runtime(monkeypatch, tmp_path)
    enqueued: list[str] = []
    monkeypatch.setattr(main.worker, "enqueue", lambda task_id: enqueued.append(task_id))
    database.create_task(
        "https://www.youtube.com/watch?v=existbatch1",
        task_id="existbatch1",
    )
    client = authenticated_client()

    response = client.post(
        "/api/tasks/batch",
        json={
            "urls": [
                "https://www.youtube.com/watch?v=existbatch1",
                "https://www.youtube.com/watch?v=newbatch001",
            ]
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["created"]) == 1
    assert body["created"][0]["task"]["id"] == "newbatch001"
    assert len(body["existing"]) == 1
    assert body["existing"][0]["task"]["id"] == "existbatch1"
    assert enqueued == ["newbatch001"]


def test_create_tasks_batch_rejects_empty(monkeypatch, tmp_path):
    configure_tmp_runtime(monkeypatch, tmp_path)
    client = authenticated_client()
    response = client.post("/api/tasks/batch", json={"urls": ["  ", ""]})
    assert response.status_code == 422
