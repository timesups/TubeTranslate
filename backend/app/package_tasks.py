from __future__ import annotations

import math
import os
import shutil
import tempfile
from fnmatch import fnmatch
from pathlib import Path

from .adapters.local_subtitles import parse_subtitle_file
from .adapters.export_video import resolve_bilingual_subtitle
from .config import package_allowed_roots, package_export_dir_name, package_max_items
from . import resource_limits

DEFAULT_VIDEO_GLOBS = ("*.mp4", "*.mov", "*.mkv", "*.m4v", "*.webm", "*.avi", "*.flv", "*.wmv")


def validate_source_dir(source_dir: str) -> Path:
    cleaned = source_dir.strip().strip('"').strip("'")
    if not cleaned:
        raise ValueError("source_dir is required.")
    if "\x00" in cleaned:
        raise ValueError("source_dir must not contain null bytes.")
    path = Path(cleaned).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"source_dir does not exist: {cleaned}") from exc
    if not resolved.is_dir():
        raise ValueError(f"source_dir is not a directory: {cleaned}")
    _ensure_under_allowed_roots(resolved, label="source_dir")
    return resolved


def validate_video_file(video_path: str) -> Path:
    cleaned = video_path.strip().strip('"').strip("'")
    if not cleaned:
        raise ValueError("video path is required.")
    if "\x00" in cleaned:
        raise ValueError("video path must not contain null bytes.")
    path = Path(cleaned).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"video file does not exist: {cleaned}") from exc
    if not resolved.is_file():
        raise ValueError(f"video path is not a file: {cleaned}")
    if not _matches_glob(resolved, DEFAULT_VIDEO_GLOBS):
        raise ValueError(f"unsupported video file type: {resolved.name}")
    _ensure_under_allowed_roots(resolved, label="video path")
    return resolved


def validate_subtitle_file(subtitle_path: str) -> Path:
    cleaned = subtitle_path.strip().strip('"').strip("'")
    if not cleaned:
        raise ValueError("subtitle path is required.")
    if "\x00" in cleaned:
        raise ValueError("subtitle path must not contain null bytes.")
    path = Path(cleaned).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"subtitle file does not exist: {cleaned}") from exc
    if not resolved.is_file():
        raise ValueError(f"subtitle path is not a file: {cleaned}")
    if resolved.suffix.lower() not in {".srt", ".vtt"}:
        raise ValueError("Only .srt and .vtt subtitle files are supported.")
    _ensure_under_allowed_roots(resolved, label="subtitle path")
    try:
        parse_subtitle_file(resolved)
    except UnicodeDecodeError as exc:
        raise ValueError("Invalid subtitle file encoding.") from exc
    except ValueError as exc:
        raise ValueError(f"Invalid subtitle file: {exc}") from exc
    return resolved


def _ensure_under_allowed_roots(path: Path, *, label: str) -> None:
    allowed = package_allowed_roots()
    if not allowed:
        return
    if not any(_is_under_root(path, root) for root in allowed):
        roots = "; ".join(str(root) for root in allowed)
        raise ValueError(f"{label} must be under PACKAGE_ALLOWED_ROOTS: {roots}")


def _is_under_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _matches_glob(path: Path, globs: tuple[str, ...]) -> bool:
    name = path.name
    return any(fnmatch(name, pattern) for pattern in globs)


def _is_inside_translate_dir(path: Path, source_root: Path) -> bool:
    """Skip videos already under a Translate output folder."""
    translate_name = package_export_dir_name()
    try:
        relative = path.resolve().relative_to(source_root.resolve())
    except ValueError:
        return any(part == translate_name for part in path.parts)
    return any(part == translate_name for part in relative.parts[:-1])


def scan_source_dir(
    source_dir: Path,
    *,
    glob: str | None = None,
    recursive: bool = False,
    skip_if_export_exists: bool = False,
    output_suffix: str = "",
) -> list[dict[str, object]]:
    _ = output_suffix  # kept for API compatibility; exports use Translate/ instead
    patterns = _parse_glob(glob)
    max_items = package_max_items()
    files: list[Path] = []
    iterator = source_dir.rglob("*") if recursive else source_dir.iterdir()
    for entry in sorted(iterator, key=lambda path: str(path).lower()):
        if not entry.is_file():
            continue
        if not _matches_glob(entry, patterns):
            continue
        resolved = entry.resolve()
        if _is_inside_translate_dir(resolved, source_dir):
            continue
        _ensure_under_allowed_roots(resolved, label="video path")
        files.append(resolved)
        if len(files) > max_items:
            raise ValueError(f"At most {max_items} videos are allowed per package.")
    if not files:
        raise ValueError("No matching video files were found in source_dir.")

    items: list[dict[str, object]] = []
    for path in files:
        relative = _relative_path(source_dir, path)
        export_path = export_destination(path)
        will_skip = skip_if_export_exists and is_complete_export(export_path, path)
        items.append(
            {
                "source_path": str(path),
                "relative_path": relative,
                "title": path.stem,
                "size_bytes": path.stat().st_size,
                "export_path": str(export_path),
                "will_skip": will_skip,
            }
        )
    return items


def _parse_glob(glob: str | None) -> tuple[str, ...]:
    if not glob or not glob.strip():
        return DEFAULT_VIDEO_GLOBS
    patterns = tuple(part.strip() for part in glob.split(",") if part.strip())
    if not patterns:
        return DEFAULT_VIDEO_GLOBS
    return patterns


def _relative_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def common_source_root(video_files: list[Path]) -> Path:
    if not video_files:
        raise ValueError("video_paths must not be empty.")
    if len(video_files) == 1:
        return video_files[0].parent
    try:
        return Path(os.path.commonpath([str(path) for path in video_files]))
    except ValueError as exc:
        raise ValueError("video_paths must share a common parent directory.") from exc


def build_items_from_video_paths(
    entries: list[dict[str, str | None]],
    *,
    skip_if_export_exists: bool = False,
) -> tuple[Path, list[dict[str, object]]]:
    """Build package items from explicit video (+ optional source-language SRT) paths."""
    max_items = package_max_items()
    if not entries:
        raise ValueError("video_paths must not be empty.")
    if len(entries) > max_items:
        raise ValueError(f"At most {max_items} videos are allowed per package.")

    video_files: list[Path] = []
    prepared: list[tuple[Path, Path | None]] = []
    for index, entry in enumerate(entries, start=1):
        raw_path = str(entry.get("path") or "").strip()
        if not raw_path:
            raise ValueError(f"video_paths[{index - 1}].path is required.")
        video = validate_video_file(raw_path)
        subtitle_raw = str(entry.get("subtitle") or entry.get("subtitle_path") or "").strip()
        subtitle = validate_subtitle_file(subtitle_raw) if subtitle_raw else None
        video_files.append(video)
        prepared.append((video, subtitle))

    source_root = common_source_root(video_files)
    items: list[dict[str, object]] = []
    for video, subtitle in prepared:
        relative = _relative_path(source_root, video)
        export_path = export_destination(video)
        will_skip = skip_if_export_exists and is_complete_export(export_path, video)
        items.append(
            {
                "source_path": str(video),
                "relative_path": relative,
                "title": video.stem,
                "size_bytes": video.stat().st_size,
                "export_path": str(export_path),
                "will_skip": will_skip,
                "subtitle_path": str(subtitle) if subtitle else None,
            }
        )
    return source_root, items


def export_destination(source_path: Path, suffix: str = "") -> Path:
    """Place the translated file in a sibling Translate/ folder with the same name."""
    _ = suffix  # legacy API argument; no longer used for naming
    source = source_path.resolve()
    _ensure_under_allowed_roots(source, label="video path")
    destination = (source.parent / package_export_dir_name() / source.name).resolve()
    _ensure_under_allowed_roots(destination, label="export path")
    return destination


def is_complete_export(destination: Path, source_path: Path) -> bool:
    """Validate existing exports, including files created before atomic export writes."""
    from .adapters.local_video import _probe_media

    _ensure_under_allowed_roots(destination.resolve(), label="export path")
    if not destination.is_file() or destination.stat().st_size == 0:
        return False
    try:
        duration = _probe_media(source_path).get("duration")
        if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
            return False
        probe = _probe_media(destination)
        exported_duration = probe.get("duration")
        return bool(
            probe.get("video_codec")
            and isinstance(exported_duration, (int, float))
            and math.isfinite(exported_duration)
            and exported_duration >= duration * 0.95
        )
    except (OSError, RuntimeError, ValueError):
        return False


def uniquify_destination(path: Path) -> Path:
    if not path.exists() and not path.with_suffix(".srt").exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    index = 2
    while True:
        candidate = parent / f"{stem}({index}){suffix}"
        if not candidate.exists() and not candidate.with_suffix(".srt").exists():
            return candidate
        index += 1


def export_package_item(
    *,
    final_video: Path,
    source_path: Path,
    output_suffix: str = "",
    session: Path | None = None,
) -> Path:
    # Keep name selection and commit together, including colliding subtitle sidecars.
    with resource_limits.slot(resource_limits.EXPORT):
        return _export_package_item(final_video=final_video, source_path=source_path,
                                    output_suffix=output_suffix, session=session)


def _export_package_item(
    *, final_video: Path, source_path: Path, output_suffix: str = "", session: Path | None = None,
) -> Path:
    source = source_path.resolve()
    destination = export_destination(source, output_suffix)
    destination = uniquify_destination(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # The video name becomes visible only after both copies have completed.
    temporary: list[Path] = []
    subtitle_destination = destination.with_suffix(".srt")
    _ensure_under_allowed_roots(subtitle_destination.resolve(), label="subtitle export path")
    try:
        def stage_copy(source_file: Path) -> Path:
            fd, name = tempfile.mkstemp(prefix=".youdub-", suffix=".partial", dir=destination.parent)
            os.close(fd)
            path = Path(name)
            temporary.append(path)
            shutil.copy2(source_file, path)
            if path.stat().st_size != source_file.stat().st_size:
                raise OSError("Export copy is incomplete.")
            return path

        video_temp = stage_copy(final_video)
        subtitle = resolve_bilingual_subtitle(final_video, session)
        subtitle_temp = stage_copy(subtitle) if subtitle is not None else None
        if subtitle_temp is not None:
            subtitle_temp.replace(subtitle_destination)
        video_temp.replace(destination)
    finally:
        for path in temporary:
            path.unlink(missing_ok=True)
    return destination
