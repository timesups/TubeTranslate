from __future__ import annotations

import shutil
from fnmatch import fnmatch
from pathlib import Path

from . import config
from .config import package_allowed_roots, package_max_items

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
    allowed = package_allowed_roots()
    if allowed:
        if not any(_is_under_root(resolved, root) for root in allowed):
            roots = "; ".join(str(root) for root in allowed)
            raise ValueError(f"source_dir must be under PACKAGE_ALLOWED_ROOTS: {roots}")
    return resolved


def _is_under_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _matches_glob(path: Path, globs: tuple[str, ...]) -> bool:
    name = path.name
    return any(fnmatch(name, pattern) for pattern in globs)


def scan_source_dir(
    source_dir: Path,
    *,
    glob: str | None = None,
    recursive: bool = False,
    skip_if_export_exists: bool = False,
    output_suffix: str = "",
) -> list[dict[str, object]]:
    patterns = _parse_glob(glob)
    max_items = package_max_items()
    files: list[Path] = []
    iterator = source_dir.rglob("*") if recursive else source_dir.iterdir()
    for entry in sorted(iterator, key=lambda path: str(path).lower()):
        if not entry.is_file():
            continue
        if not _matches_glob(entry, patterns):
            continue
        files.append(entry.resolve())
        if len(files) > max_items:
            raise ValueError(f"At most {max_items} videos are allowed per package.")
    if not files:
        raise ValueError("No matching video files were found in source_dir.")

    items: list[dict[str, object]] = []
    for path in files:
        relative = _relative_path(source_dir, path)
        export_path = export_destination(path, output_suffix)
        will_skip = skip_if_export_exists and export_path.exists()
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


def export_destination(source_path: Path, suffix: str) -> Path:
    cleaned_suffix = suffix.strip() or config.package_output_suffix()
    return source_path.with_name(f"{source_path.stem}{cleaned_suffix}{source_path.suffix}")


def uniquify_destination(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    index = 2
    while True:
        candidate = parent / f"{stem}({index}){suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def export_package_item(
    *,
    final_video: Path,
    source_path: Path,
    output_suffix: str,
    session: Path | None = None,
) -> Path:
    _ = session
    source = source_path.resolve()
    destination = export_destination(source, output_suffix)
    destination = uniquify_destination(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(final_video, destination)
    return destination
