from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..config import ffmpeg_binary, ffprobe_binary
from ..sanitize import sanitize_text
from ..sources import SourceConfig
from ..youtube import local_upload_task_id

_COPY_VIDEO_CODECS = frozenset({"h264", "hevc", "vp9", "av1"})
_COPY_AUDIO_CODECS = frozenset({"aac", "mp3", "opus", "flac", "vorbis"})
_DURATION_TOLERANCE = 0.95


def upload_dir(workfolder: Path, task_id: str) -> Path:
    return workfolder / "_uploads" / task_id


def uploaded_video_dir(workfolder: Path, task_id: str) -> Path:
    return upload_dir(workfolder, task_id) / "video"


def remove_upload(workfolder: Path, task_id: str) -> None:
    target = upload_dir(workfolder, task_id)
    if target.exists():
        shutil.rmtree(target)


def _single_file(root: Path, task_id: str, label: str) -> Path:
    files = sorted(path for path in root.iterdir() if path.is_file())
    if not files:
        raise FileNotFoundError(f"Local upload {label} is missing for task {task_id}.")
    if len(files) > 1:
        raise RuntimeError(f"Local upload has multiple {label} files for task {task_id}.")
    return files[0]


def _uploaded_video_file(workfolder: Path, task_id: str) -> Path:
    root = upload_dir(workfolder, task_id)
    if not root.exists():
        raise FileNotFoundError(f"Local upload is missing for task {task_id}.")

    video_root = uploaded_video_dir(workfolder, task_id)
    if video_root.exists():
        return _single_file(video_root, task_id, "video")
    return _single_file(root, task_id, "video")


def _title_from_url(url: str, source_file: Path) -> str:
    query = parse_qs(urlparse(url.strip()).query)
    filename = (query.get("filename") or [""])[0].strip()
    if filename:
        return Path(filename).stem or source_file.stem
    return source_file.stem


def _session_path(workfolder: Path, task_id: str, title: str) -> Path:
    safe_title = sanitize_text(title) or "local-video"
    return workfolder / "local" / f"{safe_title}__{task_id}"


def _probe_media(path: Path) -> dict[str, object]:
    result = subprocess.run(
        [
            ffprobe_binary(),
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name:format=duration",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"ffprobe failed for {path.name}: {detail or result.returncode}")
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe returned invalid JSON for {path.name}") from exc

    video_codec: str | None = None
    audio_codec: str | None = None
    for stream in payload.get("streams") or []:
        if not isinstance(stream, dict):
            continue
        codec_type = str(stream.get("codec_type") or "")
        codec_name = str(stream.get("codec_name") or "").lower() or None
        if codec_type == "video" and video_codec is None:
            video_codec = codec_name
        elif codec_type == "audio" and audio_codec is None:
            audio_codec = codec_name

    duration = None
    raw_duration = (payload.get("format") or {}).get("duration")
    if raw_duration not in (None, ""):
        try:
            duration = float(raw_duration)
        except (TypeError, ValueError):
            duration = None

    return {
        "video_codec": video_codec,
        "audio_codec": audio_codec,
        "duration": duration,
    }


def _can_stream_copy(probe: dict[str, object]) -> bool:
    video_codec = probe.get("video_codec")
    audio_codec = probe.get("audio_codec")
    if video_codec not in _COPY_VIDEO_CODECS:
        return False
    if audio_codec is None:
        return True
    return audio_codec in _COPY_AUDIO_CODECS


def _is_usable_output(path: Path, expected_duration: float | None) -> bool:
    if not path.exists() or path.stat().st_size <= 0:
        return False
    try:
        probe = _probe_media(path)
    except RuntimeError:
        return False
    duration = probe.get("duration")
    if not isinstance(duration, (int, float)) or duration <= 0:
        return False
    if isinstance(expected_duration, (int, float)) and expected_duration > 0:
        if float(duration) < float(expected_duration) * _DURATION_TOLERANCE:
            return False
    return True


def _run_ffmpeg(command: list[str], output_file: Path) -> None:
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        if output_file.exists():
            output_file.unlink(missing_ok=True)
        detail = ((exc.stderr or exc.stdout or "").strip() or f"exit {exc.returncode}")[-2000:]
        raise RuntimeError(f"ffmpeg failed converting local upload: {detail}") from exc


def _transcode_to_mp4(source_file: Path, video_file: Path, *, stream_copy: bool) -> None:
    if stream_copy:
        command = [
            ffmpeg_binary(),
            "-y",
            "-i",
            str(source_file),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(video_file),
        ]
    else:
        command = [
            ffmpeg_binary(),
            "-y",
            "-i",
            str(source_file),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(video_file),
        ]
    _run_ffmpeg(command, video_file)


def import_local_video(url: str, workfolder: Path, source: SourceConfig) -> tuple[Path, dict]:
    from .local_subtitles import uploaded_subtitle_file

    task_id = local_upload_task_id(url)
    if not task_id:
        raise ValueError("Invalid local upload URL.")

    source_file = _uploaded_video_file(workfolder, task_id)
    subtitle_file = uploaded_subtitle_file(workfolder, task_id)
    title = _title_from_url(url, source_file)
    session = _session_path(workfolder, task_id, title)
    media_dir = session / "media"
    metadata_dir = session / "metadata"
    media_dir.mkdir(parents=True, exist_ok=True)
    metadata_dir.mkdir(parents=True, exist_ok=True)

    video_file = media_dir / "video_source.mp4"
    info = {
        "id": task_id,
        "title": title,
        "source": "local",
        "webpage_url": url,
        "original_path": str(source_file),
        "asr_language": source.asr_language,
        "target_language": source.target_language,
    }
    if subtitle_file:
        info["subtitle_path"] = str(subtitle_file)
    metadata_file = metadata_dir / "local_info.json"
    metadata_file.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")

    source_probe = _probe_media(source_file)
    expected_duration = source_probe.get("duration")
    expected = expected_duration if isinstance(expected_duration, (int, float)) else None
    if _is_usable_output(video_file, expected):
        from ..bilibili.staging import extract_cover_to_session

        extract_cover_to_session(video_file, session)
        return session, info
    if video_file.exists():
        video_file.unlink(missing_ok=True)

    _transcode_to_mp4(
        source_file,
        video_file,
        stream_copy=_can_stream_copy(source_probe),
    )
    if not _is_usable_output(video_file, expected):
        if video_file.exists():
            video_file.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg finished without producing a complete media/video_source.mp4")

    from ..bilibili.staging import extract_cover_to_session

    extract_cover_to_session(video_file, session)
    return session, info
