from __future__ import annotations

import queue
import threading
import asyncio

from backend.app import database, worker


def test_lifespan_recovers_interrupted_jobs_exactly_once(monkeypatch, tmp_path):
    from backend.app import main, package_db
    from backend.tests.test_settings_and_api import configure_tmp_runtime

    original_start = worker.start
    original_enqueue = worker.enqueue
    configure_tmp_runtime(monkeypatch, tmp_path)
    task_id = database.create_task("https://www.youtube.com/watch?v=abcdefghijk")
    database.update_task(task_id, status="running")
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO task_packages (id, status, source_root, output_suffix, direction, created_at) "
            "VALUES ('interrupted', 'running', ?, 'Translate', 'en-zh', '2020-01-01')",
            (str(tmp_path),),
        )
    work_queue = queue.Queue()

    class DormantThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(main, "ensure_runtime_dirs", lambda: None)
    monkeypatch.setattr(worker, "start", original_start)
    monkeypatch.setattr(worker, "enqueue", original_enqueue)
    monkeypatch.setattr(worker, "_thread", None)
    monkeypatch.setattr(worker, "_queue", work_queue)
    monkeypatch.setattr(worker.threading, "Thread", DormantThread)

    async def startup():
        async with main.lifespan(main.app):
            assert database.get_task(task_id)["status"] == "queued"
            assert package_db.get_package("interrupted")["status"] == "queued"
            assert work_queue.get_nowait() == ("task", task_id)
            assert work_queue.get_nowait() == ("package", "interrupted")
            assert work_queue.empty()

    asyncio.run(startup())


def test_start_recovers_old_queued_jobs_without_list_limits(monkeypatch, tmp_path):
    from backend.app import package_db

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "recovery.sqlite")
    database.init_db()
    with database.connect() as conn:
        conn.executemany(
            "INSERT INTO tasks (id, url, status, created_at) VALUES (?, ?, ?, ?)",
            [("old-task", "local://old", "queued", "2020-01-01")]
            + [(f"done-task-{i}", "local://done", "succeeded", "2021-01-01") for i in range(101)],
        )
        conn.executemany(
            "INSERT INTO task_packages (id, status, source_root, output_suffix, direction, created_at) "
            "VALUES (?, ?, ?, 'Translate', 'en-zh', ?)",
            [("old-package", "queued", str(tmp_path), "2020-01-01")]
            + [(f"done-package-{i}", "succeeded", str(tmp_path), "2021-01-01") for i in range(501)],
        )
    work_queue = queue.Queue()

    class DormantThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(worker, "_thread", None)
    monkeypatch.setattr(worker, "_queue", work_queue)
    monkeypatch.setattr(worker.threading, "Thread", DormantThread)
    worker.start(lambda _: None, lambda _: None)
    assert work_queue.get_nowait() == ("task", "old-task")
    assert work_queue.get_nowait() == ("package", "old-package")
    assert work_queue.empty()
    assert package_db.pending_package_ids() == ["old-package"]


def test_worker_picks_up_pending_and_new_tasks(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "worker.sqlite")
    database.init_db()
    pre_queued = [
        database.create_task(f"https://www.youtube.com/watch?v=v{i:011d}") for i in range(2)
    ]

    executed: list[str] = []
    target = len(pre_queued) + 1
    done = threading.Event()

    def runner(task_id: str) -> None:
        executed.append(task_id)
        if len(executed) == target:
            done.set()

    monkeypatch.setattr(worker, "_thread", None)
    worker.start(runner)
    worker.enqueue("late-task")

    assert done.wait(timeout=2.0)
    assert executed[:2] == pre_queued
    assert executed[-1] == "late-task"


def test_worker_isolates_runner_exception_and_processes_next_task(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "worker-isolation.sqlite")
    database.init_db()
    failed_task = database.create_task(
        "https://www.youtube.com/watch?v=failedtask1",
        task_id="failedtask1",
    )
    successful_task = database.create_task(
        "https://www.youtube.com/watch?v=successtask",
        task_id="successtask",
    )
    work_queue: queue.Queue[tuple[str, str]] = queue.Queue()
    monkeypatch.setattr(worker, "_queue", work_queue)

    executed: list[str] = []
    second_finished = threading.Event()

    def runner(task_id: str) -> None:
        executed.append(task_id)
        if task_id == failed_task:
            raise RuntimeError("first runner exploded")
        database.update_task(
            task_id,
            status="succeeded",
            current_stage="done",
            completed_at=database.now_iso(),
        )
        second_finished.set()

    def run_package(_package_id: str) -> None:
        raise AssertionError("package runner should not be invoked")

    thread = threading.Thread(target=worker._loop, args=(runner, run_package), daemon=True)
    thread.start()
    work_queue.put(("task", failed_task))
    work_queue.put(("task", successful_task))

    assert second_finished.wait(timeout=2.0)
    queue_joined = threading.Event()

    def join_queue() -> None:
        work_queue.join()
        queue_joined.set()

    threading.Thread(target=join_queue, daemon=True).start()
    assert queue_joined.wait(timeout=2.0)

    failed = database.get_task(failed_task)
    failed_stages = {stage["name"]: stage for stage in failed["stages"]}
    succeeded = database.get_task(successful_task)
    log_content = database.log_path(failed_task).read_text(encoding="utf-8")

    assert executed == [failed_task, successful_task]
    assert thread.is_alive()
    assert failed["status"] == "failed"
    assert failed["error_message"] == "first runner exploded"
    assert failed_stages["download"]["status"] == "failed"
    assert "Worker caught an unhandled runner exception" in log_content
    assert "RuntimeError: first runner exploded" in log_content
    assert succeeded["status"] == "succeeded"


def test_worker_continues_when_failure_reporter_also_raises(monkeypatch, caplog):
    work_queue: queue.Queue[tuple[str, str]] = queue.Queue()
    monkeypatch.setattr(worker, "_queue", work_queue)

    def fail_reporter(_task_id: str, _exc: Exception) -> None:
        raise RuntimeError("reporter exploded")

    monkeypatch.setattr(worker, "_record_runner_failure", fail_reporter)
    processed: list[str] = []
    second_finished = threading.Event()

    def runner(task_id: str) -> None:
        processed.append(task_id)
        if task_id == "first":
            raise RuntimeError("runner exploded")
        second_finished.set()

    def run_package(_package_id: str) -> None:
        raise AssertionError("package runner should not be invoked")

    thread = threading.Thread(target=worker._loop, args=(runner, run_package), daemon=True)
    thread.start()
    work_queue.put(("task", "first"))
    work_queue.put(("task", "second"))

    assert second_finished.wait(timeout=2.0)
    queue_joined = threading.Event()

    def join_queue() -> None:
        work_queue.join()
        queue_joined.set()

    threading.Thread(target=join_queue, daemon=True).start()
    assert queue_joined.wait(timeout=2.0)

    assert thread.is_alive()
    assert processed == ["first", "second"]
    assert "Unhandled worker runner exception for task first" in caplog.text
    assert "Failed to record runner exception for task first" in caplog.text
