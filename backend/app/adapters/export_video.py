from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from ..sanitize import sanitize_text
from .ffmpeg import write_chinese_srt


@dataclass(frozen=True)
class ExportResult:
    video: Path
    subtitle: Path | None = None


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


def export_final_video(
    final_video: Path,
    *,
    task_id: str,
    title: str | None,
    output_dir: str | None,
    session: Path | None = None,
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

    subtitle_destination: Path | None = None
    subtitle_source = resolve_chinese_subtitle(final_video, session=session)
    if subtitle_source is not None and subtitle_source.exists():
        subtitle_destination = dest_dir / f"{stem}.srt"
        shutil.copy2(subtitle_source, subtitle_destination)

    return ExportResult(video=video_destination, subtitle=subtitle_destination)
