"""Single-thread FIFO worker for standalone tasks and package jobs."""

from __future__ import annotations

import logging
import queue
import threading
import traceback
from typing import Callable

from . import database, runtime_security


_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
_thread: threading.Thread | None = None
_lock = threading.Lock()
logger = logging.getLogger(__name__)


def enqueue(task_id: str) -> None:
    _queue.put(("task", task_id))


def enqueue_package(package_id: str) -> None:
    _queue.put(("package", package_id))


def _append_failure_log(task_id: str, traceback_text: str) -> None:
    path = database.log_path(task_id)
    timestamp = database.now_iso()
    with runtime_security.open_private_append_text(path) as handle:
        handle.write(f"[{timestamp}] Worker caught an unhandled runner exception\n")
        for line in traceback_text.rstrip().splitlines():
            handle.write(f"[{timestamp}] {line}\n")


def _record_runner_failure(task_id: str, exc: Exception) -> None:
    error_message = str(exc).strip() or type(exc).__name__
    traceback_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

    task = None
    try:
        task = database.get_task(task_id)
    except Exception:
        logger.exception("Failed to load task %s after runner exception", task_id)

    if task is not None:
        completed_at = database.now_iso()
        try:
            database.update_task(
                task_id,
                status="failed",
                error_message=error_message,
                completed_at=completed_at,
            )
        except Exception:
            logger.exception("Failed to mark task %s as failed", task_id)

        failed_stage = task.get("current_stage")
        if failed_stage and failed_stage != "done":
            try:
                database.update_stage(
                    task_id,
                    failed_stage,
                    status="failed",
                    completed_at=completed_at,
                    error_message=error_message,
                    last_message="Failed",
                )
            except Exception:
                logger.exception("Failed to mark task %s stage %s as failed", task_id, failed_stage)

    try:
        _append_failure_log(task_id, traceback_text)
    except Exception:
        logger.exception("Failed to write runner exception log for task %s", task_id)


def _record_package_failure(package_id: str, exc: Exception) -> None:
    from . import package_db

    error_message = str(exc).strip() or type(exc).__name__
    traceback_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        package_db.update_package(
            package_id,
            status="failed",
            error_message=error_message,
            completed_at=database.now_iso(),
        )
    except Exception:
        logger.exception("Failed to mark package %s as failed", package_id)
    try:
        path = package_db.log_path(package_id)
        timestamp = database.now_iso()
        with runtime_security.open_private_append_text(path) as handle:
            handle.write(f"[{timestamp}] Worker caught an unhandled runner exception\n")
            for line in traceback_text.rstrip().splitlines():
                handle.write(f"[{timestamp}] {line}\n")
    except Exception:
        logger.exception("Failed to write runner exception log for package %s", package_id)


def _loop(run_task: Callable[[str], None], run_package: Callable[[str], None]) -> None:
    while True:
        kind, job_id = _queue.get()
        try:
            if kind == "package":
                run_package(job_id)
            else:
                run_task(job_id)
        except Exception as exc:
            logger.error(
                "Unhandled worker runner exception for %s %s",
                kind,
                job_id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            try:
                if kind == "package":
                    _record_package_failure(job_id, exc)
                else:
                    _record_runner_failure(job_id, exc)
            except Exception:
                logger.exception("Failed to record runner exception for %s %s", kind, job_id)
        finally:
            _queue.task_done()


def start(run_task: Callable[[str], None], run_package: Callable[[str], None] | None = None) -> None:
    global _thread
    package_runner = run_package
    if package_runner is None:
        from .package_pipeline import run_package as default_run_package

        package_runner = default_run_package
    with _lock:
        if _thread is not None:
            return
        _thread = threading.Thread(target=_loop, args=(run_task, package_runner), daemon=True)
        _thread.start()
    pending_tasks = [t for t in database.list_tasks() if t["status"] == "queued"]
    for task in reversed(pending_tasks):
        _queue.put(("task", task["id"]))
    from . import package_db

    pending_packages = [p for p in package_db.list_packages(limit=500) if p["status"] in ("queued", "partial")]
    for package in reversed(pending_packages):
        _queue.put(("package", package["id"]))
