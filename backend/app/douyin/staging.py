from __future__ import annotations

import logging
import shutil
from pathlib import Path

from ..adapters.export_video import export_basename, resolve_chinese_subtitle
from ..bilibili.staging import (
    StagingPackage,
    _copy_cover_as_jpeg,
    _extract_cover,
    find_session_cover,
)
from .auth import DATA_DIR, ensure_data_dir

logger = logging.getLogger(__name__)


def staging_dir() -> Path:
    ensure_data_dir()
    path = DATA_DIR / "staging"
    path.mkdir(parents=True, exist_ok=True)
    return path


def prepare_douyin_staging(
    *,
    task_id: str,
    title: str | None,
    final_video: Path,
    session: Path | None = None,
) -> StagingPackage:
    if not final_video.exists():
        raise FileNotFoundError(f"Final video not found: {final_video}")

    dest = staging_dir()
    stem = export_basename(task_id=task_id, title=title)
    video_dest = dest / f"{stem}.mp4"
    shutil.copy2(final_video, video_dest)

    subtitle_dest: Path | None = None
    subtitle_source = resolve_chinese_subtitle(final_video, session=session)
    if subtitle_source is not None and subtitle_source.exists():
        subtitle_dest = dest / f"{stem}.srt"
        shutil.copy2(subtitle_source, subtitle_dest)

    cover_dest = dest / f"{stem}.jpg"
    cover_path: Path | None = None
    source_cover = find_session_cover(session)
    try:
        if source_cover is not None:
            _copy_cover_as_jpeg(source_cover, cover_dest)
            cover_path = cover_dest
        else:
            _extract_cover(video_dest, cover_dest)
            cover_path = cover_dest
    except Exception:
        logger.exception("Douyin cover preparation failed for task %s", task_id)
        if cover_dest.exists():
            cover_dest.unlink(missing_ok=True)
        cover_path = None

    return StagingPackage(
        stem=stem,
        video=video_dest,
        subtitle=subtitle_dest,
        cover=cover_path,
    )
