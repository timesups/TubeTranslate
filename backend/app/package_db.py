from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from . import config
from .database import connect, now_iso
from .stages import PACKAGE_STAGES, PACKAGE_STAGE_NAMES
from .youtube import LOCAL_UPLOAD_DIRECTIONS

PACKAGE_STATUSES = frozenset({"queued", "running", "paused", "partial", "succeeded", "failed"})
ITEM_STATUSES = frozenset({"pending", "queued", "running", "succeeded", "failed", "skipped"})


def normalize_direction(value: str) -> str:
    cleaned = (value or "").strip().lower()
    if cleaned not in LOCAL_UPLOAD_DIRECTIONS:
        raise ValueError(f"direction must be one of: {', '.join(sorted(LOCAL_UPLOAD_DIRECTIONS))}")
    return cleaned


def init_package_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS task_packages (
          id TEXT PRIMARY KEY,
          name TEXT,
          status TEXT NOT NULL,
          source_root TEXT NOT NULL,
          output_suffix TEXT NOT NULL,
          export_subtitle INTEGER NOT NULL DEFAULT 1,
          direction TEXT NOT NULL,
          execution_mode TEXT NOT NULL DEFAULT 'auto',
          audio_mode TEXT NOT NULL DEFAULT 'replace',
          tts_provider TEXT NOT NULL DEFAULT 'azure',
          continue_on_error INTEGER NOT NULL DEFAULT 1,
          skip_if_export_exists INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          started_at TEXT,
          completed_at TEXT,
          error_message TEXT
        );

        CREATE TABLE IF NOT EXISTS task_package_items (
          id TEXT PRIMARY KEY,
          package_id TEXT NOT NULL,
          sort_index INTEGER NOT NULL,
          source_path TEXT NOT NULL,
          relative_path TEXT,
          title TEXT,
          status TEXT NOT NULL,
          current_stage TEXT,
          session_path TEXT,
          final_video_path TEXT,
          exported_video_path TEXT,
          exported_subtitle_path TEXT,
          error_message TEXT,
          created_at TEXT NOT NULL,
          started_at TEXT,
          completed_at TEXT,
          FOREIGN KEY (package_id) REFERENCES task_packages(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS task_package_item_stages (
          item_id TEXT NOT NULL,
          name TEXT NOT NULL,
          label TEXT NOT NULL,
          status TEXT NOT NULL,
          progress INTEGER,
          started_at TEXT,
          completed_at TEXT,
          last_message TEXT,
          error_message TEXT,
          PRIMARY KEY (item_id, name),
          FOREIGN KEY (item_id) REFERENCES task_package_items(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_task_package_items_package
        ON task_package_items(package_id, sort_index);
        """
    )


def log_path(package_id: str) -> Path:
    from .database import log_path as task_log_path

    return task_log_path(f"package-{package_id}")


def item_log_path(item_id: str) -> Path:
    from .database import log_path as task_log_path

    return task_log_path(f"package-item-{item_id}")


def create_package(
    *,
    name: str,
    source_root: str,
    output_suffix: str,
    direction: str,
    execution_mode: str,
    audio_mode: str,
    tts_provider: str,
    export_subtitle: bool,
    continue_on_error: bool,
    skip_if_export_exists: bool,
    items: list[dict[str, Any]],
) -> str:
    package_id = str(uuid.uuid4())
    created_at = now_iso()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO task_packages (
              id, name, status, source_root, output_suffix, export_subtitle, direction,
              execution_mode, audio_mode, tts_provider, continue_on_error,
              skip_if_export_exists, created_at
            )
            VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                package_id,
                name.strip() or None,
                source_root,
                output_suffix,
                1 if export_subtitle else 0,
                normalize_direction(direction),
                execution_mode,
                audio_mode,
                tts_provider,
                1 if continue_on_error else 0,
                1 if skip_if_export_exists else 0,
                created_at,
            ),
        )
        for index, item in enumerate(items, start=1):
            item_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO task_package_items (
                  id, package_id, sort_index, source_path, relative_path, title,
                  status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    item_id,
                    package_id,
                    index,
                    item["source_path"],
                    item.get("relative_path"),
                    item.get("title"),
                    created_at,
                ),
            )
            conn.executemany(
                """
                INSERT INTO task_package_item_stages (item_id, name, label, status)
                VALUES (?, ?, ?, 'pending')
                """,
                [(item_id, stage.name, stage.label) for stage in PACKAGE_STAGES],
            )
    return package_id


def get_package(package_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM task_packages WHERE id = ?", (package_id,)).fetchone()
        if not row:
            return None
        items = conn.execute(
            """
            SELECT * FROM task_package_items
            WHERE package_id = ?
            ORDER BY sort_index ASC, rowid ASC
            """,
            (package_id,),
        ).fetchall()
    package = _serialize_package(dict(row))
    package["items"] = [_serialize_item_with_stages(dict(item)) for item in items]
    return package


def get_package_item(item_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM task_package_items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return None
    return _serialize_item_with_stages(dict(row))


def list_packages(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT p.*,
              (SELECT COUNT(*) FROM task_package_items i WHERE i.package_id = p.id) AS item_count,
              (SELECT COUNT(*) FROM task_package_items i WHERE i.package_id = p.id AND i.status = 'succeeded') AS succeeded_count,
              (SELECT COUNT(*) FROM task_package_items i WHERE i.package_id = p.id AND i.status = 'failed') AS failed_count
            FROM task_packages p
            ORDER BY p.created_at DESC, p.rowid DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [_serialize_package(dict(row)) for row in rows]


def update_package(package_id: str, **fields: Any) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [package_id]
    with connect() as conn:
        conn.execute(f"UPDATE task_packages SET {assignments} WHERE id = ?", values)


def update_package_item(item_id: str, **fields: Any) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [item_id]
    with connect() as conn:
        conn.execute(f"UPDATE task_package_items SET {assignments} WHERE id = ?", values)


def update_package_item_stage(item_id: str, name: str, **fields: Any) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [item_id, name]
    with connect() as conn:
        conn.execute(
            f"UPDATE task_package_item_stages SET {assignments} WHERE item_id = ? AND name = ?",
            values,
        )


def queue_package_for_continue(package_id: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE task_packages
            SET status = 'queued', error_message = NULL, completed_at = NULL
            WHERE id = ?
            """,
            (package_id,),
        )


def reset_failed_package_items(package_id: str) -> int:
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE task_package_items
            SET status = 'pending', current_stage = NULL, error_message = NULL,
                started_at = NULL, completed_at = NULL
            WHERE package_id = ? AND status = 'failed'
            """,
            (package_id,),
        )
        failed_items = conn.execute(
            "SELECT id FROM task_package_items WHERE package_id = ? AND status = 'pending'",
            (package_id,),
        ).fetchall()
        for row in failed_items:
            conn.execute(
                """
                UPDATE task_package_item_stages
                SET status = 'pending', started_at = NULL, completed_at = NULL,
                    progress = NULL, last_message = NULL, error_message = NULL
                WHERE item_id = ? AND status IN ('failed', 'running')
                """,
                (row["id"],),
            )
        conn.execute(
            """
            UPDATE task_packages
            SET status = 'queued', error_message = NULL, completed_at = NULL
            WHERE id = ?
            """,
            (package_id,),
        )
    return cursor.rowcount


def delete_package(package_id: str) -> bool:
    with connect() as conn:
        cursor = conn.execute("DELETE FROM task_packages WHERE id = ?", (package_id,))
    return cursor.rowcount > 0


def reclaim_interrupted_packages() -> list[str]:
    message = "Backend restarted before the package completed."
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id FROM task_packages
            WHERE status IN ('running', 'paused')
            ORDER BY created_at ASC, rowid ASC
            """
        ).fetchall()
        package_ids = [str(row["id"]) for row in rows]
        for package_id in package_ids:
            conn.execute(
                """
                UPDATE task_package_items
                SET status = CASE WHEN status = 'running' THEN 'pending' ELSE status END,
                    error_message = CASE WHEN status = 'running' THEN ? ELSE error_message END
                WHERE package_id = ? AND status = 'running'
                """,
                (message, package_id),
            )
            conn.execute(
                """
                UPDATE task_package_item_stages
                SET status = 'pending', error_message = NULL
                WHERE item_id IN (
                  SELECT id FROM task_package_items WHERE package_id = ? AND status = 'pending'
                ) AND status IN ('failed', 'running')
                """,
                (package_id,),
            )
            conn.execute(
                """
                UPDATE task_packages
                SET status = 'queued', error_message = ?
                WHERE id = ?
                """,
                (message, package_id),
            )
    return package_ids


def _serialize_package(data: dict[str, Any]) -> dict[str, Any]:
    if "export_subtitle" in data:
        data["export_subtitle"] = bool(data["export_subtitle"])
    if "continue_on_error" in data:
        data["continue_on_error"] = bool(data["continue_on_error"])
    if "skip_if_export_exists" in data:
        data["skip_if_export_exists"] = bool(data["skip_if_export_exists"])
    return data


def _serialize_item_with_stages(data: dict[str, Any]) -> dict[str, Any]:
    item_id = data["id"]
    with connect() as conn:
        stages = conn.execute(
            """
            SELECT * FROM task_package_item_stages
            WHERE item_id = ?
            ORDER BY CASE name
            """
            + " ".join(
                f"WHEN '{stage.name}' THEN {index}"
                for index, stage in enumerate(PACKAGE_STAGES, start=1)
            )
            + " END",
            (item_id,),
        ).fetchall()
    data["stages"] = [dict(stage) for stage in stages]
    return data


def synthesize_task_dict(item: dict[str, Any], package: dict[str, Any]) -> dict[str, Any]:
    from urllib.parse import quote

    filename = quote(Path(item["source_path"]).name)
    direction = package["direction"]
    return {
        "id": item["id"],
        "url": f"local://file/{item['id']}?direction={direction}&filename={filename}",
        "title": item.get("title"),
        "status": item.get("status"),
        "current_stage": item.get("current_stage"),
        "session_path": item.get("session_path"),
        "final_video_path": item.get("final_video_path"),
        "execution_mode": package.get("execution_mode"),
        "audio_mode": package.get("audio_mode"),
        "tts_provider": package.get("tts_provider"),
        "bilibili_auto_publish": False,
        "bilibili_generate_meta": False,
        "stages": item.get("stages") or [],
    }
