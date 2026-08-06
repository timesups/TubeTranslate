from __future__ import annotations

from backend.app import database
from backend.tests.test_settings_and_api import configure_tmp_runtime


def test_reclaim_interrupted_tasks_requeues_running_and_restart_failures(monkeypatch, tmp_path):
    configure_tmp_runtime(monkeypatch, tmp_path)
    database.init_db()

    running = database.create_task("https://www.youtube.com/watch?v=abcdefghijk")
    queued = database.create_task("https://www.youtube.com/watch?v=bcdefghijkl")
    restart_failed = database.create_task("https://www.youtube.com/watch?v=cdefghijklm")
    other_failed = database.create_task("https://www.youtube.com/watch?v=defghijklmn")

    database.update_task(running, status="running", current_stage="download", started_at=database.now_iso())
    database.update_stage(running, "download", status="running", started_at=database.now_iso())
    database.update_task(queued, status="queued")
    database.update_task(
        restart_failed,
        status="failed",
        current_stage="download",
        error_message="Backend restarted before the task completed.",
        completed_at=database.now_iso(),
    )
    database.update_stage(
        restart_failed,
        "download",
        status="failed",
        error_message="Backend restarted before the task completed.",
        completed_at=database.now_iso(),
    )
    database.update_task(
        other_failed,
        status="failed",
        current_stage="asr",
        error_message="boom",
        completed_at=database.now_iso(),
    )

    reclaimed = database.reclaim_interrupted_tasks()
    assert set(reclaimed) == {running, queued, restart_failed}

    assert database.get_task(running)["status"] == "queued"
    assert database.get_task(queued)["status"] == "queued"
    assert database.get_task(restart_failed)["status"] == "queued"
    assert database.get_task(restart_failed)["error_message"] is None
    assert database.get_task(other_failed)["status"] == "failed"

    download = next(stage for stage in database.get_task(running)["stages"] if stage["name"] == "download")
    assert download["status"] == "pending"
    assert download["error_message"] is None
