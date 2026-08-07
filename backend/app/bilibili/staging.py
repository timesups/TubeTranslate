from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..adapters.export_video import export_basename, resolve_chinese_subtitle
from ..config import ffmpeg_binary
from .deepseek_meta import load_settings

logger = logging.getLogger(__name__)

COVER_CANDIDATES = (
    "cover_source.jpg",
    "cover_source.jpeg",
    "cover_source.png",
    "cover_source.webp",
)


@dataclass(frozen=True)
class StagingPackage:
    stem: str
    video: Path
    subtitle: Path | None
    cover: Path | None


def staging_dir() -> Path:
    settings = load_settings()
    path = Path(str(settings.get("video_dir") or "")).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def find_session_cover(session: Path | None) -> Path | None:
    if session is None:
        return None
    media = session / "media"
    if not media.is_dir():
        return None
    for name in COVER_CANDIDATES:
        path = media / name
        if path.exists() and path.stat().st_size > 0:
            return path
    matches = sorted(
        path
        for path in media.glob("cover_source.*")
        if path.is_file() and path.stat().st_size > 0
    )
    return matches[0] if matches else None


def _extract_cover(video: Path, cover: Path) -> None:
    cover.parent.mkdir(parents=True, exist_ok=True)
    for seek in ("1", "0"):
        cmd = [
            ffmpeg_binary(),
            "-y",
            "-ss",
            seek,
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(cover),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode == 0 and cover.exists() and cover.stat().st_size > 0:
            return
    stderr = (result.stderr or result.stdout or "").strip()[-400:]
    raise RuntimeError(f"Failed to extract cover with ffmpeg: {stderr}")


def extract_cover_to_session(video: Path, session: Path) -> Path | None:
    """Write media/cover_source.jpg from a video frame (local uploads / fallback)."""
    dest = session / "media" / "cover_source.jpg"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    try:
        _extract_cover(video, dest)
    except Exception:
        logger.exception("Failed to extract cover from %s", video)
        if dest.exists():
            dest.unlink(missing_ok=True)
        return None
    return dest if dest.exists() and dest.stat().st_size > 0 else None


def _copy_cover_as_jpeg(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        shutil.copy2(source, dest)
        return
    try:
        from PIL import Image

        with Image.open(source) as image:
            image.convert("RGB").save(dest, format="JPEG", quality=92, optimize=True)
    except Exception:
        # Last resort: let ffmpeg re-encode still image.
        cmd = [
            ffmpeg_binary(),
            "-y",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(dest),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0 or not dest.exists() or dest.stat().st_size <= 0:
            raise RuntimeError(f"Failed to convert cover image: {source}")


def prepare_task_staging(
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
        logger.exception("Cover preparation failed for task %s", task_id)
        if cover_dest.exists():
            cover_dest.unlink(missing_ok=True)
        cover_path = None

    return StagingPackage(
        stem=stem,
        video=video_dest,
        subtitle=subtitle_dest,
        cover=cover_path,
    )
