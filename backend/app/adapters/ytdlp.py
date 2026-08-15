from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import time

import requests
import yt_dlp

from .. import runtime_security
from ..sanitize import sanitize_text
from ..sources import SourceConfig
from ..youtube import extract_video_id, validate_video_url

log = logging.getLogger(__name__)


FORMAT_CANDIDATES = (
    # Minimum 720p; prefer 1080p+ when available. Never fall back to unrestricted "best"
    # (that previously accepted ios progressive 360p and stopped early).
    "bestvideo[height>=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height>=720][ext=mp4]+bestaudio[ext=m4a]/best[height>=720][ext=mp4]",
    "bestvideo[height>=720]+bestaudio/best[height>=720]",
    "bestvideo*[height>=720]+bestaudio/best[height>=720]",
    "bv*[height>=720]+ba/b[height>=720]",
)

MIN_VIDEO_HEIGHT = 720
DOWNLOAD_ATTEMPTS = 5

# Prefer clients more likely to expose >=720p DASH; ios/tv last (often progressive-only).
YOUTUBE_PLAYER_CLIENTS = (
    ("web", "web_safari"),
    ("tv_embedded", "mweb"),
    ("ios", "tv", "android_vr"),
    (),  # empty => do not force player_client; use yt-dlp defaults
)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
YOUTUBE_403_HINT = (
    "YouTube returned HTTP 403 while downloading video data. "
    "Upload a YouTube cookie in Settings, then retry this task from the download stage."
)
YOUTUBE_FORMAT_HINT = (
    f"YouTube did not provide a downloadable format at {MIN_VIDEO_HEIGHT}p or higher. "
    "Upload a YouTube cookie in Settings (or update yt-dlp), then retry from the download stage."
)
YOUTUBE_RESOLUTION_HINT = (
    f"Downloaded video is below the minimum {MIN_VIDEO_HEIGHT}p requirement "
    f"after {DOWNLOAD_ATTEMPTS} attempts."
)


def _decorate_download_error(exc: Exception) -> Exception:
    if _is_http_forbidden(exc):
        return RuntimeError(f"{YOUTUBE_403_HINT} Original error: {exc}")
    if _is_format_unavailable(exc) or "no video formats" in re.sub(
        r"\x1b\[[0-9;]*m", "", str(exc)
    ).lower():
        return RuntimeError(f"{YOUTUBE_FORMAT_HINT} Original error: {exc}")
    if "below the minimum" in str(exc).lower() or "minimum required" in str(exc).lower():
        return RuntimeError(f"{YOUTUBE_RESOLUTION_HINT} Original error: {exc}")
    return exc


def _probe_video_height(video_file: Path) -> int | None:
    from .ffmpeg import probe_video_size

    size = probe_video_size(video_file)
    if size is None:
        return None
    return int(size[1])


def _ensure_min_resolution(video_file: Path, *, min_height: int = MIN_VIDEO_HEIGHT) -> None:
    height = _probe_video_height(video_file)
    if height is None:
        raise RuntimeError(f"Could not probe video resolution after download: {video_file}")
    if height < min_height:
        raise RuntimeError(
            f"Downloaded video is {height}p; minimum required is {min_height}p"
        )


def _bootstrap_bilibili_cookie(cookie_path: Path) -> None:
    response = requests.get(
        "https://www.bilibili.com/",
        headers={"User-Agent": DEFAULT_USER_AGENT, "Referer": "https://www.bilibili.com/"},
        timeout=10,
    )
    response.raise_for_status()
    expires = int(time.time()) + 3600 * 24 * 365
    lines = ["# Netscape HTTP Cookie File", ""]
    cookies = dict(response.cookies)
    cookies.setdefault("SESSDATA", "anonymous_for_webpage_playinfo")
    for name, value in cookies.items():
        lines.append("\t".join([".bilibili.com", "TRUE", "/", "FALSE", str(expires), name, value]))
    runtime_security.atomic_write_private_text(cookie_path, "\n".join(lines) + "\n")


def _proxy_url(proxy_port: str = "") -> str:
    if proxy_port.strip():
        return f"http://127.0.0.1:{proxy_port.strip()}"
    return os.getenv("HTTP_PROXY") or os.getenv("http_proxy") or ""


def _ensure_cookie(source: SourceConfig) -> None:
    cookie_path = source.cookie_path
    if not cookie_path or source.name != "bilibili":
        return
    metadata = runtime_security.private_file_stat(cookie_path)
    if metadata and metadata.st_size > 0:
        return
    _bootstrap_bilibili_cookie(cookie_path)


def _ydl_base(
    source: SourceConfig,
    proxy_port: str = "",
    *,
    player_clients: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Build shared yt-dlp options.

    Do not force a YouTube player_client here by default: metadata extraction and
    download each choose clients explicitly. Binding extract_info to web/web_safari
    alone made format selection fail before the download retry loop could run.
    """
    opts: dict[str, Any] = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        # Fail fast when YouTube is unreachable without the configured local proxy.
        "socket_timeout": 30,
        "js_runtimes": {"node": {}},
        "http_headers": {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.youtube.com/",
        },
    }
    cookie_path = source.cookie_path
    if cookie_path:
        metadata = runtime_security.private_file_stat(cookie_path)
        if metadata and metadata.st_size > 0:
            opts["cookiefile"] = str(cookie_path)
    if source.name == "youtube" and player_clients is not None:
        if player_clients:
            opts["extractor_args"] = {
                "youtube": {"player_client": list(player_clients)},
            }
    if not source.use_proxy:
        opts["proxy"] = ""
        return opts
    proxy = _proxy_url(proxy_port)
    if proxy:
        opts["proxy"] = proxy
    return opts


def _extract_video_info(
    url: str, source: SourceConfig, proxy_port: str = ""
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch metadata only. Use unrestricted format so client fallbacks can succeed."""
    if source.name != "youtube":
        with yt_dlp.YoutubeDL(_ydl_base(source, proxy_port)) as ydl:
            info = ydl.extract_info(url, download=False)
            return info, ydl.sanitize_info(info)

    last_error: Exception | None = None
    for clients in YOUTUBE_PLAYER_CLIENTS:
        opts = {
            **_ydl_base(source, proxy_port, player_clients=clients),
            # Metadata must not require >=720p; that filter belongs to the download stage.
            "format": "best",
            "check_formats": False,
            "ignore_no_formats_error": True,
        }
        label = ",".join(clients) if clients else "default"
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    raise RuntimeError("yt-dlp returned empty video metadata")
                return info, ydl.sanitize_info(info)
        except Exception as exc:
            last_error = exc
            log.warning("extract_info failed clients=%s: %s", label, exc)
            if _is_retryable_download_error(exc):
                continue
            raise _decorate_download_error(exc) from exc
    assert last_error is not None
    raise _decorate_download_error(last_error) from last_error


def _session_path(workfolder: Path, info: dict[str, Any]) -> Path:
    uploader = sanitize_text(str(info.get("uploader") or "unknown"))
    title = sanitize_text(str(info.get("title") or "untitled"))
    video_id = str(info.get("id") or extract_video_id(str(info.get("webpage_url") or "")))
    return workfolder / uploader / f"{title}__{video_id}"


def _is_format_unavailable(exc: Exception) -> bool:
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(exc))
    return "Requested format is not available" in text


def _is_http_forbidden(exc: Exception) -> bool:
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).lower()
    return "http error 403" in text or "403: forbidden" in text


def _is_retryable_download_error(exc: Exception) -> bool:
    if _is_format_unavailable(exc) or _is_http_forbidden(exc):
        return True
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).lower()
    return "unable to download video data" in text or "no video formats" in text


def _remove_partial_outputs(video_file: Path) -> None:
    """Delete the target mp4 and any yt-dlp sidecar/partial next to it."""
    for candidate in video_file.parent.glob(f"{video_file.name}*"):
        if candidate.is_file():
            candidate.unlink(missing_ok=True)


COVER_SOURCE_NAME = "cover_source.jpg"


def _pick_thumbnail_url(info: dict[str, Any]) -> str | None:
    ranked: list[tuple[int, int, str]] = []
    for item in info.get("thumbnails") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        try:
            width = int(item.get("width") or 0)
            height = int(item.get("height") or 0)
        except (TypeError, ValueError):
            width, height = 0, 0
        try:
            preference = int(item.get("preference") or 0)
        except (TypeError, ValueError):
            preference = 0
        ranked.append((width * height, preference, url))
    if ranked:
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return ranked[0][2]
    direct = str(info.get("thumbnail") or "").strip()
    return direct or None


def _proxy_dict(proxy_port: str, source: SourceConfig) -> dict[str, str] | None:
    if not source.use_proxy:
        return None
    proxy = _proxy_url(proxy_port)
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def _save_cover_jpeg(image_bytes: bytes, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image
        import io

        with Image.open(io.BytesIO(image_bytes)) as image:
            rgb = image.convert("RGB")
            rgb.save(dest, format="JPEG", quality=92, optimize=True)
    except Exception:
        # Fall back to raw bytes when Pillow cannot decode (rare for platform thumbs).
        dest.write_bytes(image_bytes)
    if not dest.exists() or dest.stat().st_size <= 0:
        raise RuntimeError("Failed to write original video cover image")


def download_original_cover(
    info: dict[str, Any],
    media_dir: Path,
    source: SourceConfig,
    proxy_port: str = "",
) -> Path | None:
    """Download the platform thumbnail into media/cover_source.jpg when available."""
    dest = media_dir / COVER_SOURCE_NAME
    if dest.exists() and dest.stat().st_size > 0:
        return dest

    url = _pick_thumbnail_url(info)
    if not url:
        return None

    headers = {"User-Agent": DEFAULT_USER_AGENT, "Referer": str(info.get("webpage_url") or url)}
    try:
        response = requests.get(
            url,
            headers=headers,
            proxies=_proxy_dict(proxy_port, source),
            timeout=30,
        )
        response.raise_for_status()
        if not response.content:
            return None
        _save_cover_jpeg(response.content, dest)
    except Exception:
        if dest.exists():
            dest.unlink(missing_ok=True)
        return None
    return dest if dest.exists() and dest.stat().st_size > 0 else None


def _download_attempts(source: SourceConfig) -> list[tuple[str, dict[str, Any] | None]]:
    if source.name != "youtube":
        return [(selector, None) for selector in FORMAT_CANDIDATES]
    attempts: list[tuple[str, dict[str, Any] | None]] = []
    for clients in YOUTUBE_PLAYER_CLIENTS:
        if clients:
            extractor_args: dict[str, Any] | None = {
                "youtube": {"player_client": list(clients)}
            }
        else:
            extractor_args = None
        for selector in FORMAT_CANDIDATES:
            attempts.append((selector, extractor_args))
    return attempts


def _download_once(
    url: str, video_file: Path, source: SourceConfig, proxy_port: str
) -> None:
    last_error: Exception | None = None
    skip_client_key: tuple[str, ...] | None = None
    for format_selector, extractor_args in _download_attempts(source):
        client_key = tuple((extractor_args or {}).get("youtube", {}).get("player_client") or ())
        if skip_client_key is not None and client_key == skip_client_key:
            continue
        download_opts = {
            **_ydl_base(source, proxy_port, player_clients=client_key or None),
            "format": format_selector,
            "merge_output_format": "mp4",
            "outtmpl": str(video_file),
            "retries": 10,
            "fragment_retries": 10,
            "overwrites": True,
            "continuedl": False,
            # Avoid pre-checking every format URL; some clients falsely mark streams dead.
            "check_formats": False,
        }
        if extractor_args is not None:
            download_opts["extractor_args"] = extractor_args
        elif "extractor_args" in download_opts:
            # Empty client tuple = use yt-dlp defaults instead of a forced client.
            download_opts.pop("extractor_args", None)
        try:
            with yt_dlp.YoutubeDL(download_opts) as ydl:
                ydl.download([url])
            if video_file.exists() and video_file.stat().st_size > 0:
                _ensure_min_resolution(video_file)
                return
            last_error = RuntimeError("yt-dlp finished without producing media/video_source.mp4")
            _remove_partial_outputs(video_file)
        except Exception as exc:
            last_error = exc
            _remove_partial_outputs(video_file)
            clients = ",".join(client_key) if client_key else "default"
            log.warning(
                "yt-dlp candidate failed clients=%s format=%s: %s",
                clients,
                format_selector,
                exc,
            )
            if _is_http_forbidden(exc) and client_key:
                skip_client_key = client_key
                continue
            if _is_format_unavailable(exc) and client_key and format_selector == FORMAT_CANDIDATES[-1]:
                # Last format for this client still missing → jump to next client group.
                skip_client_key = client_key
                continue
            skip_client_key = None
            if _is_retryable_download_error(exc) or "minimum required" in str(exc).lower():
                continue
            raise _decorate_download_error(exc) from exc
    if last_error:
        raise _decorate_download_error(last_error) from last_error
    raise RuntimeError("yt-dlp finished without producing media/video_source.mp4")


def _download_with_format_candidates(
    url: str, video_file: Path, source: SourceConfig, proxy_port: str
) -> None:
    last_error: Exception | None = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            _download_once(url, video_file, source, proxy_port)
            return
        except Exception as exc:
            last_error = exc
            _remove_partial_outputs(video_file)
            log.warning(
                "download attempt %d/%d failed (min %dp): %s",
                attempt,
                DOWNLOAD_ATTEMPTS,
                MIN_VIDEO_HEIGHT,
                exc,
            )
            if attempt >= DOWNLOAD_ATTEMPTS:
                break
            time.sleep(min(2 ** (attempt - 1), 8))
    assert last_error is not None
    raise _decorate_download_error(last_error) from last_error


def download_video(
    url: str, workfolder: Path, source: SourceConfig, proxy_port: str = ""
) -> tuple[Path, dict[str, Any]]:
    validated = validate_video_url(url)
    if validated.source != source.name:
        raise ValueError("The submitted URL does not match the selected video source.")
    canonical_url = validated.url
    video_id = validated.video_id
    _ensure_cookie(source)
    info, sanitized = _extract_video_info(canonical_url, source, proxy_port)

    if str(info.get("id", video_id)) != video_id:
        raise ValueError("The resolved video id does not match the submitted URL.")

    session = _session_path(workfolder, info)
    media_dir = session / "media"
    metadata_dir = session / "metadata"
    media_dir.mkdir(parents=True, exist_ok=True)
    metadata_dir.mkdir(parents=True, exist_ok=True)

    video_file = media_dir / "video_source.mp4"
    metadata_file = metadata_dir / "ytdlp_info.json"
    metadata_file.write_text(json.dumps(sanitized, ensure_ascii=False, indent=2), encoding="utf-8")

    cover = download_original_cover(info, media_dir, source, proxy_port)
    if cover is None:
        # Keep going; staging can fall back to a frame grab later.
        pass

    # Never reuse a leftover mp4 from a previous failed download: pipeline only
    # reaches here when the download stage is not succeeded.
    _remove_partial_outputs(video_file)
    _download_with_format_candidates(canonical_url, video_file, source, proxy_port)

    if not video_file.exists() or video_file.stat().st_size == 0:
        raise RuntimeError("yt-dlp finished without producing media/video_source.mp4")

    return session, info