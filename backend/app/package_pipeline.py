from __future__ import annotations

import traceback
from pathlib import Path

from . import database, package_db, runtime_security
from .config import WORKFOLDER
from .devices import device_plan_summary
from .package_tasks import export_destination, export_package_item
from .pipeline import PipelineArtifacts, PipelineRunner, _require, _require_existing
from .runtime_checks import validate_runtime_device
from .sources import detect_source
from .stages import PACKAGE_STAGES


class PackageDeletedError(Exception):
    """Raised when a package record is removed while the worker is still running."""


def _write_package_log(package_id: str, message: str) -> None:
    path = package_db.log_path(package_id)
    timestamp = database.now_iso()
    with runtime_security.open_private_append_text(path) as handle:
        for line in message.rstrip().splitlines() or [""]:
            handle.write(f"[{timestamp}] {line}\n")


def _write_item_log(item_id: str, message: str) -> None:
    path = package_db.item_log_path(item_id)
    timestamp = database.now_iso()
    with runtime_security.open_private_append_text(path) as handle:
        for line in message.rstrip().splitlines() or [""]:
            handle.write(f"[{timestamp}] {line}\n")


class PackageItemPipelineRunner(PipelineRunner):
    def __init__(self, item: dict, package: dict):
        self.package = package
        self.item = item
        super().__init__(item["id"])

    def _refresh(self) -> dict:
        loaded_item = package_db.get_package_item(self.item["id"])
        loaded_package = package_db.get_package(self.item["package_id"])
        if loaded_item is None or loaded_package is None:
            raise PackageDeletedError(self.item["package_id"])
        self.item = loaded_item
        self.package = loaded_package
        return package_db.synthesize_task_dict(self.item, self.package)

    def log(self, message: str) -> None:
        _write_item_log(self.item["id"], message)

    def stage_message(self, stage: str, message: str) -> None:
        package_db.update_package_item_stage(self.item["id"], stage, last_message=message)
        self.log(f"[{stage}] {message}")

    def stage_progress(self, stage: str, progress: int, message: str, *, force: bool = False) -> None:
        package_db.raise_if_package_pause_requested(
            self.package["id"],
            item_id=self.item["id"],
        )
        bounded = max(0, min(100, int(progress)))
        from time import monotonic

        now = monotonic()
        previous = self._progress_state.get(stage)
        if previous and not force and bounded < 100:
            last_progress, last_at = previous
            if bounded <= last_progress:
                return
            if now - last_at < 2:
                return
        package_db.update_package_item_stage(
            self.item["id"],
            stage,
            progress=bounded,
            last_message=message,
        )
        self._progress_state[stage] = (bounded, now)

    def _stage_status(self, stage: str) -> str | None:
        item = package_db.get_package_item(self.item["id"])
        for entry in item.get("stages") or [] if item else []:
            if entry["name"] == stage:
                return entry["status"]
        return None

    def _download(self, task: dict) -> None:
        from .adapters.local_video import import_path_video

        source = detect_source(task["url"])
        session, info = import_path_video(
            Path(self.item["source_path"]),
            WORKFOLDER,
            self.package["id"],
            self.item["id"],
            source,
            title=self.item.get("title") or info.get("title"),
        )
        self.artifacts.session = session
        self.artifacts.video_file = session / "media" / "video_source.mp4"
        title = (info.get("title") or "").strip() or None
        package_db.update_package_item(
            self.item["id"],
            session_path=str(session),
            title=title,
        )
        self.stage_message("download", f"[local-file] {title or Path(self.item['source_path']).name} -> {session}")

    def _merge_video(self, task: dict) -> None:
        super()._merge_video(task)
        final_video = _require(self.artifacts.final_video, "final_video")
        exported_video = export_package_item(
            final_video=final_video,
            source_path=Path(self.item["source_path"]),
            session=self.artifacts.session,
        )
        package_db.update_package_item(
            self.item["id"],
            final_video_path=str(final_video),
            exported_video_path=str(exported_video),
            exported_subtitle_path=None,
        )
        self.stage_message("merge_video", f"Exported -> {exported_video}")

    def run(self) -> None:
        task = self._refresh()
        status = self.item["status"]
        if status not in ("pending", "queued", "paused"):
            if status == "skipped":
                return
            return

        if status in ("pending", "queued"):
            package_db.update_package_item(
                self.item["id"],
                status="running",
                started_at=self.item.get("started_at") or database.now_iso(),
                error_message=None,
            )
            self.log("Item started")
        else:
            package_db.update_package_item(self.item["id"], status="running")
            self.log("Item continued")

        execution_mode = self.package.get("execution_mode") or database.DEFAULT_EXECUTION_MODE
        try:
            validate_runtime_device()
            self.log(f"Device plan: {device_plan_summary()}")
            for stage in PACKAGE_STAGES:
                if not package_db.package_exists(self.package["id"]):
                    raise PackageDeletedError(self.package["id"])
                package_db.raise_if_package_pause_requested(
                    self.package["id"],
                    item_id=self.item["id"],
                )
                if self._stage_status(stage.name) == "succeeded":
                    package_db.update_package_item(self.item["id"], current_stage=stage.name)
                    package_db.update_package_item_stage(self.item["id"], stage.name, progress=100)
                    self._restore_cached_stage(stage.name, self._refresh())
                    self.log(f"[{stage.name}] Reused cached output")
                    continue
                self._run_package_stage(stage.name)
                package_db.raise_if_package_pause_requested(
                    self.package["id"],
                    item_id=self.item["id"],
                )
                if execution_mode == "manual" and stage != PACKAGE_STAGES[-1]:
                    package_db.update_package_item(self.item["id"], status="paused")
                    package_db.update_package(self.package["id"], status="paused")
                    self.log(f"Paused after [{stage.name}], waiting for manual continue")
                    return
            package_db.update_package_item(
                self.item["id"],
                status="succeeded",
                current_stage="done",
                completed_at=database.now_iso(),
            )
            self.log("Item succeeded")
        except PackageDeletedError:
            raise
        except database.PauseRequested:
            self.log("Paused by user")
            raise
        except Exception as exc:
            refreshed = package_db.get_package_item(self.item["id"]) or self.item
            failed_stage = refreshed.get("current_stage")
            if failed_stage and failed_stage != "done":
                package_db.update_package_item_stage(
                    self.item["id"],
                    failed_stage,
                    status="failed",
                    completed_at=database.now_iso(),
                    error_message=str(exc),
                    last_message="Failed",
                )
            package_db.update_package_item(
                self.item["id"],
                status="failed",
                error_message=str(exc),
                completed_at=database.now_iso(),
            )
            self.log("Item failed")
            self.log(traceback.format_exc())
            raise

    def _run_package_stage(self, stage: str) -> None:
        self._progress_state.pop(stage, None)
        task = self._refresh()
        package_db.update_package_item(self.item["id"], current_stage=stage)
        package_db.update_package_item_stage(
            self.item["id"],
            stage,
            status="running",
            progress=0,
            started_at=database.now_iso(),
            completed_at=None,
            error_message=None,
        )
        self.stage_message(stage, "Started")
        self._stage_handlers[stage](task)
        if stage == "merge_video" and self.artifacts.final_video is not None:
            package_db.update_package_item(
                self.item["id"],
                final_video_path=str(self.artifacts.final_video),
            )
        package_db.update_package_item_stage(
            self.item["id"],
            stage,
            status="succeeded",
            progress=100,
            completed_at=database.now_iso(),
            last_message="Completed",
        )
        self.log(f"[{stage}] Completed")


def _finalize_package_status(package_id: str) -> None:
    package = package_db.get_package(package_id)
    if package is None:
        return
    items = package.get("items") or []
    if not items:
        package_db.update_package(
            package_id,
            status="failed",
            error_message="Package has no items.",
            completed_at=database.now_iso(),
        )
        return

    succeeded = sum(1 for item in items if item["status"] == "succeeded")
    skipped = sum(1 for item in items if item["status"] == "skipped")
    failed = sum(1 for item in items if item["status"] == "failed")
    pending = sum(1 for item in items if item["status"] in {"pending", "queued", "paused", "running"})
    total = len(items)

    if pending > 0:
        return

    if failed == 0 and succeeded + skipped == total:
        status = "succeeded"
        error_message = None
    elif succeeded > 0 or skipped > 0:
        status = "partial"
        error_message = f"{failed} of {total} items failed."
    else:
        status = "failed"
        error_message = f"All {total} items failed."

    package_db.update_package(
        package_id,
        status=status,
        error_message=error_message,
        completed_at=database.now_iso(),
    )


def run_package(package_id: str) -> None:
    package = package_db.get_package(package_id)
    if package is None:
        return
    if package["status"] == "paused":
        return
    if package["status"] not in ("queued", "running", "partial"):
        return

    try:
        _run_package_body(package_id, package)
    except PackageDeletedError:
        return
    except database.PauseRequested:
        _write_package_log(package_id, "Paused by user")
        return


def _run_package_body(package_id: str, package: dict) -> None:
    if package["status"] in ("queued", "partial"):
        updates = {"status": "running"}
        if not package.get("started_at"):
            updates["started_at"] = database.now_iso()
        package_db.update_package(package_id, **updates)
        _write_package_log(package_id, "Package started")
    else:
        package_db.update_package(package_id, status="running")
        _write_package_log(package_id, "Package continued")

    continue_on_error = bool(package.get("continue_on_error"))
    try:
        validate_runtime_device()
        for item in package.get("items") or []:
            if not package_db.package_exists(package_id):
                raise PackageDeletedError(package_id)
            package_db.raise_if_package_pause_requested(package_id)
            refreshed = package_db.get_package(package_id) or package
            if refreshed["status"] == "paused":
                _write_package_log(package_id, "Package paused")
                return
            current = package_db.get_package_item(item["id"]) or item
            if current["status"] in ("succeeded", "skipped"):
                continue
            if current["status"] == "failed" and not continue_on_error:
                _write_package_log(package_id, f"Stopped after failed item {current['id']}")
                _finalize_package_status(package_id)
                return
            if current["status"] == "failed" and continue_on_error:
                continue
            if package.get("skip_if_export_exists"):
                export_path = export_destination(Path(current["source_path"]))
                if export_path.exists():
                    package_db.update_package_item(
                        current["id"],
                        status="skipped",
                        current_stage="done",
                        exported_video_path=str(export_path),
                        completed_at=database.now_iso(),
                        error_message=None,
                    )
                    for stage in PACKAGE_STAGES:
                        package_db.update_package_item_stage(
                            current["id"],
                            stage.name,
                            status="succeeded",
                            progress=100,
                            last_message="Skipped because export already exists",
                        )
                    _write_item_log(current["id"], f"Skipped existing export -> {export_path}")
                    continue
            runner = PackageItemPipelineRunner(current, package)
            try:
                runner.run()
            except PackageDeletedError:
                raise
            except database.PauseRequested:
                raise
            except Exception:
                if not continue_on_error:
                    _finalize_package_status(package_id)
                    raise
            package = package_db.get_package(package_id) or package
            if package["status"] == "paused":
                _write_package_log(package_id, "Package paused after item")
                return
        _finalize_package_status(package_id)
        _write_package_log(package_id, "Package finished")
    except PackageDeletedError:
        raise
    except database.PauseRequested:
        raise
    except Exception as exc:
        if not package_db.package_exists(package_id):
            return
        package_db.update_package(
            package_id,
            status="failed",
            error_message=str(exc),
            completed_at=database.now_iso(),
        )
        _write_package_log(package_id, f"Package failed: {exc}")
        _write_package_log(package_id, traceback.format_exc())
        raise
