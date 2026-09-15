from __future__ import annotations

import shutil
from pathlib import Path

from ..sanitize import sanitize_text
from .ffmpeg import write_bilingual_srt, write_chinese_srt


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


def resolve_bilingual_subtitle(final_video: Path, session: Path | None = None) -> Path | None:
    session_dir = session or _session_from_final_video(final_video)
    if session_dir is None:
        return None

    sidecar = session_dir / "media" / "video_final.srt"
    if sidecar.exists():
        return sidecar
    existing = session_dir / "metadata" / "subtitles.zh-en.srt"
    if existing.exists():
        return existing

    timings = session_dir / "metadata" / "timings.json"
    if not timings.exists():
        return None
    written = write_bilingual_srt(timings, session_dir)
    if written is None:
        return None
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(written, sidecar)
    return sidecar


def export_basename(*, task_id: str, title: str | None) -> str:
    safe_title = sanitize_text(title or "", fallback="video")
    return f"{safe_title}__{task_id}"
