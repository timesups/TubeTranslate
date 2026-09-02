from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..sanitize import sanitize_text
from .ffmpeg import write_chinese_srt


@dataclass(frozen=True)
class ExportResult:
    video: Path
    subtitle: Path | None = None
    description: Path | None = None


def resolve_output_dir(output_dir: str | None) -> Path | None:
    cleaned = (output_dir or "").strip().strip('"').strip("'")
    if not cleaned:
        return None
    if "\x00" in cleaned:
        raise ValueError("Output directory must not contain null bytes.")
    return Path(cleaned).expanduser()


def _session_from_final_video(final_video: Path) -> Path | None:
    media_dir = final_video.parent
    if media_dir.name != "media":
        return None
    return media_dir.parent


def resolve_chinese_subtitle(final_video: Path, session: Path | None = None) -> Path | None:
    session_dir = session or _session_from_final_video(final_video)
    if session_dir is None:
        return None

    existing = session_dir / "metadata" / "subtitles.zh.srt"
    if existing.exists():
        return existing

    timings = session_dir / "metadata" / "timings.json"
    if not timings.exists():
        return None
    return write_chinese_srt(timings, session_dir)


def export_basename(*, task_id: str, title: str | None) -> str:
    safe_title = sanitize_text(title or "", fallback="video")
    return f"{safe_title}__{task_id}"


def _load_bilibili_meta(session: Path | None, meta_path: Path | None = None) -> dict[str, Any] | None:
    candidates: list[Path] = []
    if meta_path is not None:
        candidates.append(meta_path)
    if session is not None:
        candidates.append(session / "metadata" / "bilibili_meta.json")
    for path in candidates:
        if path is None or not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def format_bilibili_description(meta: dict[str, Any]) -> str:
    title = str(meta.get("title") or "").strip()
    desc = str(meta.get("desc") or "").strip()
    tag = str(meta.get("tag") or "").strip()
    dynamic = str(meta.get("dynamic") or "").strip()
    tid = meta.get("tid")
    lines = [
        f"标题：{title}" if title else "标题：",
        f"分区TID：{tid}" if tid is not None else "",
        f"标签：{tag}" if tag else "标签：",
        f"动态：{dynamic}" if dynamic else "",
        "",
        "简介：",
        desc,
    ]
    return "\n".join(line for line in lines if line is not None).rstrip() + "\n"


def export_final_video(
    final_video: Path,
    *,
    task_id: str,
    title: str | None,
    output_dir: str | None,
    session: Path | None = None,
    bilibili_meta: dict[str, Any] | Path | None = None,
) -> ExportResult | None:
    dest_dir = resolve_output_dir(output_dir)
    if dest_dir is None:
        return None
    if not final_video.exists():
        raise FileNotFoundError(f"Final video not found: {final_video}")

    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = export_basename(task_id=task_id, title=title)
    video_destination = dest_dir / f"{stem}.mp4"
    shutil.copy2(final_video, video_destination)

    description_destination: Path | None = None
    meta: dict[str, Any] | None
    if isinstance(bilibili_meta, dict):
        meta = bilibili_meta
    elif isinstance(bilibili_meta, Path):
        meta = _load_bilibili_meta(session, bilibili_meta)
    else:
        meta = _load_bilibili_meta(session)
    if meta:
        description_destination = dest_dir / f"{stem}.bilibili.txt"
        description_destination.write_text(
            format_bilibili_description(meta),
            encoding="utf-8",
        )
        # Also keep machine-readable copy next to the text description.
        (dest_dir / f"{stem}.bilibili.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return ExportResult(
        video=video_destination,
        subtitle=None,
        description=description_destination,
    )
