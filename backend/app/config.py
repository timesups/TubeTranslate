from __future__ import annotations

import os
import threading
from pathlib import Path

from dotenv import load_dotenv

from . import runtime_security

REPO_ROOT = Path(__file__).resolve().parents[2]
runtime_security.apply_private_umask()


def _load_runtime_environment(repo_root: Path) -> None:
    runtime_security.prepare_repository_root(repo_root)
    runtime_security.secure_secret_aliases(repo_root / ".env", repo_root / "env.txt")
    load_dotenv(repo_root / ".env")


_load_runtime_environment(REPO_ROOT)

DATA_DIR = REPO_ROOT / "data"
COOKIE_DIR = DATA_DIR / "cookies"
DB_PATH = DATA_DIR / "youdub.sqlite"
YOUTUBE_COOKIE_PATH = COOKIE_DIR / "youtube.txt"
WORKFOLDER = Path(os.getenv("WORKFOLDER", str(REPO_ROOT / "workfolder"))).expanduser()
LOG_DIR = DATA_DIR / "logs"
MODEL_CACHE_DIR = Path(os.getenv("MODEL_CACHE_DIR", str(DATA_DIR / "modelscope"))).expanduser()
OUTPUT_DIR = (os.getenv("OUTPUT_DIR") or "").strip()

_RUNTIME_SECURITY_LOCK = threading.Lock()
_RUNTIME_SECURITY_SIGNATURE: tuple[str, ...] | None = None


def ensure_runtime_dirs() -> None:
    global _RUNTIME_SECURITY_SIGNATURE
    signature = tuple(
        os.path.abspath(os.fspath(path))
        for path in (
            DATA_DIR,
            COOKIE_DIR,
            WORKFOLDER,
            LOG_DIR,
            MODEL_CACHE_DIR,
            DB_PATH,
            REPO_ROOT / ".env",
            REPO_ROOT / "env.txt",
        )
    )
    with _RUNTIME_SECURITY_LOCK:
        if _RUNTIME_SECURITY_SIGNATURE == signature:
            return

        runtime_security.validate_model_cache_location(
            MODEL_CACHE_DIR,
            private_roots=(DATA_DIR, WORKFOLDER),
            protected_paths=(
                COOKIE_DIR,
                LOG_DIR,
                DB_PATH,
                REPO_ROOT / ".env",
                REPO_ROOT / "env.txt",
            ),
        )
        for directory in (DATA_DIR, COOKIE_DIR, WORKFOLDER, LOG_DIR):
            runtime_security.ensure_private_directory(directory)
        runtime_security.ensure_model_cache_directory(MODEL_CACHE_DIR)
        runtime_security.migrate_private_runtime(
            private_roots=(DATA_DIR, WORKFOLDER),
            exclude_roots=(MODEL_CACHE_DIR,),
            ephemeral_files=runtime_security.sqlite_sidecar_paths(DB_PATH),
        )
        runtime_security.secure_secret_aliases(
            REPO_ROOT / ".env", REPO_ROOT / "env.txt"
        )
        runtime_security.secure_sqlite_files(DB_PATH)
        _RUNTIME_SECURITY_SIGNATURE = signature


def device() -> str:
    configured = os.getenv("DEVICE") or os.getenv("CUDA_DEVICE")
    if configured:
        return configured
    return "cuda"


def openai_defaults() -> dict[str, str]:
    return {
        "base_url": os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE") or "https://api.openai.com/v1",
        "api_key": os.getenv("OPENAI_API_KEY", ""),
        "model": os.getenv("OPENAI_MODEL") or os.getenv("OPENAI_MODEL_NAME") or "gpt-4o-mini",
        "translate_concurrency": os.getenv("OPENAI_TRANSLATE_CONCURRENCY", "8"),
    }


def ffmpeg_binary() -> str:
    return os.getenv("FFMPEG_PATH", "").strip() or "ffmpeg"


def ffprobe_binary() -> str:
    return os.getenv("FFPROBE_PATH", "").strip() or "ffprobe"


def merge_video_encoder() -> str:
    """Video encoder for merge_video: auto, copy, x264, nvenc, qsv, or amf."""
    value = (os.getenv("MERGE_VIDEO_ENCODER") or "auto").strip().lower()
    allowed = {"auto", "copy", "x264", "nvenc", "qsv", "amf"}
    return value if value in allowed else "auto"


def merge_video_crf() -> int:
    raw = (os.getenv("MERGE_VIDEO_CRF") or "23").strip()
    try:
        crf = int(raw)
    except ValueError:
        return 23
    return max(0, min(51, crf))


def merge_video_nvenc_preset() -> str:
    value = (os.getenv("MERGE_VIDEO_NVENC_PRESET") or "p4").strip().lower()
    allowed = {f"p{i}" for i in range(1, 8)}
    return value if value in allowed else "p4"


def _ffmpeg_bin_directories() -> list[Path]:
    """Resolve directories that contain FFmpeg shared DLLs (Windows TorchCodec)."""
    candidates: list[Path] = []

    for env_name in ("FFMPEG_PATH", "FFPROBE_PATH"):
        raw = os.getenv(env_name, "").strip().strip('"')
        if not raw:
            continue
        path = Path(raw).expanduser()
        if path.is_file():
            candidates.append(path.parent)
        elif path.is_dir():
            candidates.append(path)
        elif path.parent.is_dir():
            # Env often points at ffmpeg.exe before the file is validated.
            candidates.append(path.parent)

    # Also accept a bin dir already present on PATH (start.bat / system PATH).
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        path = Path(entry)
        if (path / "avutil-60.dll").exists() or (path / "avcodec-62.dll").exists():
            candidates.append(path)
            continue
        # Broader match for other FFmpeg major versions.
        try:
            if any(path.glob("avutil-*.dll")) and any(path.glob("avcodec-*.dll")):
                candidates.append(path)
        except OSError:
            continue

    seen: set[str] = set()
    unique: list[Path] = []
    for path in candidates:
        key = os.path.normcase(str(path.resolve())) if path.exists() else os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


_FFMPEG_DLL_DIRS_REGISTERED = False


def ensure_ffmpeg_dll_search_path() -> list[str]:
    """Make FFmpeg full-shared DLLs visible to TorchCodec on Windows.

    Python 3.8+ on Windows does not search PATH for dependent DLLs of
    extension modules. TorchCodec needs ``os.add_dll_directory`` pointing at
    the FFmpeg full-shared ``bin`` folder before ``import torchcodec``.
    """
    global _FFMPEG_DLL_DIRS_REGISTERED
    directories = [path for path in _ffmpeg_bin_directories() if path.is_dir()]
    registered: list[str] = []
    if not directories:
        return registered

    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    prepend: list[str] = []
    for directory in directories:
        directory_str = str(directory)
        registered.append(directory_str)
        if os.name == "nt":
            add_dll_directory = getattr(os, "add_dll_directory", None)
            if callable(add_dll_directory):
                try:
                    add_dll_directory(directory_str)
                except (FileNotFoundError, OSError):
                    continue
        if directory_str not in path_entries and directory_str not in prepend:
            prepend.append(directory_str)

    if prepend:
        os.environ["PATH"] = os.pathsep.join([*prepend, *path_entries])

    _FFMPEG_DLL_DIRS_REGISTERED = True
    return registered


# Register as early as possible so Demucs / TorchCodec can load FFmpeg DLLs.
ensure_ffmpeg_dll_search_path()


def ytdlp_defaults() -> dict[str, str]:
    return {
        "proxy_port": os.getenv("YTDLP_PROXY_PORT", ""),
    }


def output_defaults() -> dict[str, str]:
    return {
        "output_dir": OUTPUT_DIR,
    }


def volcengine_tts_defaults() -> dict[str, str]:
    return {
        "app_id": os.getenv("VOLCENGINE_TTS_APP_ID", ""),
        "access_key": os.getenv("VOLCENGINE_TTS_ACCESS_KEY", ""),
        "api_key": os.getenv("VOLCENGINE_TTS_API_KEY", ""),
        "resource_id": os.getenv("VOLCENGINE_TTS_RESOURCE_ID", "seed-tts-2.0"),
        "speaker": os.getenv(
            "VOLCENGINE_TTS_SPEAKER",
            "zh_female_shuangkuaisisi_moon_bigtts",
        ),
        "endpoint": os.getenv(
            "VOLCENGINE_TTS_ENDPOINT",
            "https://openspeech.bytedance.com/api/v3/tts/unidirectional",
        ),
        "sample_rate": os.getenv("VOLCENGINE_TTS_SAMPLE_RATE", "24000"),
        "speech_rate": os.getenv("VOLCENGINE_TTS_SPEECH_RATE", "0"),
        "concurrency": os.getenv("VOLCENGINE_TTS_CONCURRENCY", "4"),
        "uid": os.getenv("VOLCENGINE_TTS_UID", "youdub-webui"),
    }


def azure_tts_defaults() -> dict[str, str]:
    return {
        "subscription_key": os.getenv("AZURE_TTS_SUBSCRIPTION_KEY", ""),
        "region": os.getenv("AZURE_TTS_REGION", "eastasia"),
        "voice": os.getenv("AZURE_TTS_VOICE", "zh-CN-XiaoxiaoNeural"),
        "locale": os.getenv("AZURE_TTS_LOCALE", "zh-CN"),
        "endpoint": os.getenv("AZURE_TTS_ENDPOINT", ""),
        "output_format": os.getenv(
            "AZURE_TTS_OUTPUT_FORMAT",
            "audio-24khz-48kbitrate-mono-mp3",
        ),
        "speech_rate": os.getenv("AZURE_TTS_SPEECH_RATE", "0"),
        "concurrency": os.getenv("AZURE_TTS_CONCURRENCY", "4"),
    }
