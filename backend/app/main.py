from __future__ import annotations

import logging
import os
import re
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, SecretStr

from . import auth, database, runtime_security, worker
from .adapters.local_subtitles import parse_srt, uploaded_subtitle_dir
from .adapters.local_video import remove_upload, uploaded_video_dir
from .adapters.openai_client import validate_openai_base_url
from .adapters.openai_translate import list_models as list_openai_models
from .config import WORKFOLDER, YOUTUBE_COOKIE_PATH, ensure_runtime_dirs, package_export_dir_name
from .package_tasks import scan_source_dir, validate_source_dir
from . import package_db
from .pipeline import run_task
from .runtime_checks import validate_runtime_device
from .sanitize import sanitize_text
from .sources import detect_source
from .stage_reset import remove_stage_artifacts
from .stages import STAGE_NAMES
from .youtube import LOCAL_UPLOAD_DIRECTIONS, is_local_upload_url, validate_video_url
from .bilibili.routes import router as bilibili_router
from .douyin.routes import router as douyin_router

ALLOWED_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".flv", ".wmv"}
ALLOWED_SUBTITLE_SUFFIXES = {".srt"}
LOCAL_UPLOAD_CHUNK_SIZE = 1024 * 1024
MAX_LOCAL_UPLOAD_BYTES = int(os.getenv("LOCAL_UPLOAD_MAX_BYTES", str(4 * 1024 * 1024 * 1024)))
MAX_LOCAL_SUBTITLE_BYTES = int(os.getenv("LOCAL_SUBTITLE_MAX_BYTES", str(20 * 1024 * 1024)))

logger = logging.getLogger(__name__)

TaskListStatus = Literal["all", "queued", "running", "paused", "succeeded", "failed"]
TaskListExecutionMode = Literal["all", "auto", "manual"]
TaskListSort = Literal[
    "created_desc",
    "created_asc",
    "started_desc",
    "started_asc",
    "completed_desc",
    "completed_asc",
    "status_asc",
    "status_desc",
    "title_asc",
    "title_desc",
]


def mask_secret(value: str) -> str:
    if not value:
        return ""
    return "********"


class TaskCreate(BaseModel):
    url: str
    execution_mode: str = "auto"
    audio_mode: str = "replace"
    tts_provider: str = "azure"
    bilibili_tid: int = 229
    bilibili_auto_publish: bool = True
    bilibili_generate_meta: bool = True
    douyin_auto_publish: bool = False
    douyin_generate_meta: bool = True


class TaskBatchCreate(BaseModel):
    urls: list[str]
    execution_mode: str = "auto"
    audio_mode: str = "replace"
    tts_provider: str = "azure"
    bilibili_tid: int = 229
    bilibili_auto_publish: bool = True
    bilibili_generate_meta: bool = True
    douyin_auto_publish: bool = False
    douyin_generate_meta: bool = True


class TaskPackageScan(BaseModel):
    source_dir: str
    glob: str | None = None
    recursive: bool = False
    output_suffix: str | None = None
    skip_if_export_exists: bool = True


class TaskPackageCreate(TaskPackageScan):
    name: str = ""
    direction: str = "en-zh"
    execution_mode: str = "auto"
    audio_mode: str = "replace"
    tts_provider: str = "azure"
    export_subtitle: bool = False
    continue_on_error: bool = True


class TaskPackageContinue(BaseModel):
    execution_mode: str | None = None


class TaskBatchDelete(BaseModel):
    task_ids: list[str]


class PackageBatchRequest(BaseModel):
    package_ids: list[str]


class ContinueTaskRequest(BaseModel):
    execution_mode: str | None = None


MAX_BATCH_TASK_URLS = 50
MAX_BATCH_DELETE_TASKS = 200
MAX_BATCH_RESUME_TASKS = 200
MAX_BATCH_CLEANUP_TASKS = 200
MAX_BATCH_PACKAGE_OPS = 200


class YouTubeCookieUpdate(BaseModel):
    content: str


class OpenAISettingsUpdate(BaseModel):
    base_url: str
    api_key: str = ""
    clear_api_key: bool = False
    model: str
    translate_concurrency: str = ""


class OpenAIModelsRequest(BaseModel):
    base_url: str = ""
    api_key: str = ""


class YtdlpSettingsUpdate(BaseModel):
    proxy_port: str = ""


class OutputSettingsUpdate(BaseModel):
    output_dir: str = ""


class AzureTtsSettingsUpdate(BaseModel):
    subscription_key: str = ""
    clear_subscription_key: bool = False
    region: str = "eastasia"
    voice: str = "zh-CN-XiaoxiaoNeural"
    locale: str = "zh-CN"
    endpoint: str = ""
    output_format: str = "audio-24khz-48kbitrate-mono-mp3"
    speech_rate: str = "0"
    concurrency: str = "4"


class AzureTtsVoicesRequest(BaseModel):
    region: str = ""
    subscription_key: str = ""
    endpoint: str = ""


class AzureTtsValidateKeysRequest(BaseModel):
    region: str = ""
    subscription_key: str = ""
    endpoint: str = ""


class LoginRequest(BaseModel):
    password: SecretStr


def normalize_proxy_port(value: str) -> str:
    proxy_port = value.strip()
    if not proxy_port:
        return ""
    if not proxy_port.isdigit():
        raise HTTPException(status_code=422, detail="Proxy port must be numeric.")
    port = int(proxy_port)
    if port < 1 or port > 65535:
        raise HTTPException(status_code=422, detail="Proxy port must be between 1 and 65535.")
    return str(port)


def normalize_output_dir(value: str) -> str:
    from .adapters.export_video import resolve_output_dir

    cleaned = value.strip().strip('"').strip("'")
    if not cleaned:
        return ""
    try:
        resolved = resolve_output_dir(cleaned)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if resolved is None:
        return ""
    return str(resolved)


def normalize_translate_concurrency(value: str) -> str:
    concurrency = value.strip()
    if not concurrency:
        return ""
    if not all("0" <= char <= "9" for char in concurrency):
        raise HTTPException(status_code=422, detail="Translate concurrency must be numeric.")
    workers = int(concurrency)
    if workers < 1 or workers > 200:
        raise HTTPException(
            status_code=422, detail="Translate concurrency must be between 1 and 200."
        )
    return concurrency


def normalize_tts_concurrency(value: str) -> str:
    concurrency = value.strip()
    if not concurrency:
        return ""
    if not all("0" <= char <= "9" for char in concurrency):
        raise HTTPException(status_code=422, detail="TTS concurrency must be numeric.")
    workers = int(concurrency)
    if workers < 1 or workers > 200:
        raise HTTPException(
            status_code=422, detail="TTS concurrency must be between 1 and 200."
        )
    return concurrency


def normalize_azure_tts_concurrency(value: str) -> str:
    return normalize_tts_concurrency(value)


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_runtime_dirs()
    auth.validate_auth_configuration()
    database.init_db()
    database.delete_expired_auth_sessions(database.now_iso())
    database.backfill_titles_from_metadata()
    interrupted = database.reclaim_interrupted_tasks()
    from . import package_db

    interrupted_packages = package_db.reclaim_interrupted_packages()
    worker.start(run_task)
    for task_id in interrupted:
        worker.enqueue(task_id)
    for package_id in interrupted_packages:
        worker.enqueue_package(package_id)
    yield


app = FastAPI(
    title="YouDub API",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.include_router(bilibili_router)
app.include_router(douyin_router)


@app.exception_handler(RequestValidationError)
async def redact_login_validation_error(
    request: Request, exc: RequestValidationError
) -> Response:
    if request.url.path == "/api/auth/login":
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid credentials."},
            headers={"Cache-Control": "no-store"},
        )
    return await request_validation_exception_handler(request, exc)


DEFAULT_CORS_ORIGIN_REGEX = (
    r"^https?://("
    r"localhost|"
    r"127\.0\.0\.1|"
    r"\[::1\]"
    r"):3000$"
)


def cors_origins() -> list[str]:
    defaults = ["http://localhost:3000", "http://127.0.0.1:3000"]
    configured = os.getenv("CORS_ALLOW_ORIGINS", "")
    extra = [origin.strip() for origin in configured.split(",") if origin.strip()]
    if "*" in extra:
        raise RuntimeError("CORS_ALLOW_ORIGINS cannot contain '*' when credentials are enabled.")
    return [*defaults, *extra]


def cors_origin_regex() -> str:
    configured = os.getenv("CORS_ALLOW_ORIGIN_REGEX", "").strip()
    return configured or DEFAULT_CORS_ORIGIN_REGEX


_cors_origins = cors_origins()
_cors_origin_regex = cors_origin_regex()

app.add_middleware(
    auth.AuthMiddleware,
    allowed_origins=_cors_origins,
    allowed_origin_regex=_cors_origin_regex,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_origin_regex=_cors_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _clear_replaced_login_cookie(
    response: Response,
    old_token: str,
    settings: auth.AuthSettings | None = None,
) -> None:
    if not old_token:
        return
    if settings is not None:
        auth.clear_session_cookie(response, settings)
        return
    response.delete_cookie(
        key=auth.SESSION_COOKIE_NAME,
        path=auth.SESSION_COOKIE_PATH,
        httponly=True,
    )


@app.post("/api/auth/login")
def login(payload: LoginRequest, request: Request) -> JSONResponse:
    old_token = request.cookies.get(auth.SESSION_COOKIE_NAME, "")
    auth.revoke_session_token(old_token)
    client_host = request.client.host if request.client else "unknown"

    try:
        settings = auth.validate_auth_configuration()
    except auth.AuthConfigurationError:
        response = JSONResponse(
            status_code=503,
            content={"detail": "Authentication is not configured."},
            headers={"Cache-Control": "no-store"},
        )
        _clear_replaced_login_cookie(response, old_token)
        return response

    rate_limit = auth.reserve_login_attempt(client_host)
    if not rate_limit.allowed:
        response = JSONResponse(
            status_code=429,
            content={"detail": "Too many login attempts."},
            headers={
                "Cache-Control": "no-store",
                "Retry-After": str(rate_limit.retry_after_seconds),
            },
        )
        _clear_replaced_login_cookie(response, old_token, settings)
        return response

    try:
        password_matches = auth.verify_password(
            payload.password.get_secret_value(), settings
        )
    except auth.AuthConfigurationError:
        auth.clear_login_attempts(client_host)
        response = JSONResponse(
            status_code=503,
            content={"detail": "Authentication is not configured."},
            headers={"Cache-Control": "no-store"},
        )
        _clear_replaced_login_cookie(response, old_token, settings)
        return response

    if not password_matches:
        response = JSONResponse(
            status_code=401,
            content={"detail": "Invalid credentials."},
            headers={"Cache-Control": "no-store"},
        )
        _clear_replaced_login_cookie(response, old_token, settings)
        return response

    auth.clear_login_attempts(client_host)
    token, session = auth.create_session(settings)
    response = JSONResponse(
        content={
            "authenticated": True,
            "csrf_token": session.csrf_token,
            "expires_at": session.expires_at,
        },
        headers={"Cache-Control": "no-store"},
    )
    auth.set_session_cookie(response, token, settings, session.expires_at)
    return response


@app.get("/api/auth/session")
def auth_session(request: Request) -> JSONResponse:
    session: auth.AuthenticatedSession = request.state.auth_session
    return JSONResponse(
        content={
            "authenticated": True,
            "csrf_token": session.csrf_token,
            "expires_at": session.expires_at,
        },
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/auth/logout", status_code=204)
def logout(request: Request) -> Response:
    session: auth.AuthenticatedSession = request.state.auth_session
    settings: auth.AuthSettings = request.state.auth_settings
    database.delete_auth_session(session.token_hash)
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    auth.clear_session_cookie(response, settings)
    return response


def _ensure_runtime_ready() -> None:
    try:
        validate_runtime_device()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def normalize_execution_mode(value: str) -> str:
    try:
        return database.normalize_execution_mode(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def normalize_audio_mode(value: str) -> str:
    try:
        return database.normalize_audio_mode(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def normalize_tts_provider(value: str) -> str:
    try:
        return database.normalize_tts_provider(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def normalize_bilibili_tid(value: int | str) -> int:
    try:
        return database.normalize_bilibili_tid(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def normalize_bilibili_auto_publish(value: bool | int | str) -> bool:
    try:
        return database.normalize_bilibili_auto_publish(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def normalize_bilibili_generate_meta(
    value: bool | int | str,
    *,
    bilibili_auto_publish: bool,
) -> bool:
    try:
        return database.resolve_bilibili_generate_meta(
            value,
            bilibili_auto_publish=bilibili_auto_publish,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def normalize_douyin_auto_publish(value: bool | int | str) -> bool:
    try:
        return database.normalize_douyin_auto_publish(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def normalize_douyin_generate_meta(
    value: bool | int | str,
    *,
    douyin_auto_publish: bool,
) -> bool:
    try:
        return database.resolve_douyin_generate_meta(
            value,
            douyin_auto_publish=douyin_auto_publish,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/tasks", status_code=201)
def create_task(payload: TaskCreate) -> dict:
    try:
        validated_url = validate_video_url(payload.url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    existing_id = database.find_task_by_video_id(validated_url.video_id)
    if existing_id:
        return database.get_task(existing_id)

    _ensure_runtime_ready()
    auto_publish = normalize_bilibili_auto_publish(payload.bilibili_auto_publish)
    douyin_auto = normalize_douyin_auto_publish(payload.douyin_auto_publish)
    task_id = database.create_task(
        validated_url.url,
        task_id=validated_url.video_id,
        execution_mode=normalize_execution_mode(payload.execution_mode),
        audio_mode=normalize_audio_mode(payload.audio_mode),
        tts_provider=normalize_tts_provider(payload.tts_provider),
        bilibili_tid=normalize_bilibili_tid(payload.bilibili_tid),
        bilibili_auto_publish=auto_publish,
        bilibili_generate_meta=normalize_bilibili_generate_meta(
            payload.bilibili_generate_meta,
            bilibili_auto_publish=auto_publish,
        ),
        douyin_auto_publish=douyin_auto,
        douyin_generate_meta=normalize_douyin_generate_meta(
            payload.douyin_generate_meta,
            douyin_auto_publish=douyin_auto,
        ),
    )
    worker.enqueue(task_id)
    return database.get_task(task_id)


def _normalize_batch_urls(urls: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        url = raw.strip()
        if not url or url in seen:
            continue
        seen.add(url)
        normalized.append(url)
    return normalized


@app.post("/api/tasks/batch")
def create_tasks_batch(payload: TaskBatchCreate) -> dict:
    urls = _normalize_batch_urls(payload.urls)
    if not urls:
        raise HTTPException(status_code=422, detail="At least one URL is required.")
    if len(urls) > MAX_BATCH_TASK_URLS:
        raise HTTPException(
            status_code=422,
            detail=f"At most {MAX_BATCH_TASK_URLS} URLs are allowed per batch.",
        )

    execution_mode = normalize_execution_mode(payload.execution_mode)
    audio_mode = normalize_audio_mode(payload.audio_mode)
    tts_provider = normalize_tts_provider(payload.tts_provider)
    bilibili_tid = normalize_bilibili_tid(payload.bilibili_tid)
    bilibili_auto_publish = normalize_bilibili_auto_publish(payload.bilibili_auto_publish)
    bilibili_generate_meta = normalize_bilibili_generate_meta(
        payload.bilibili_generate_meta,
        bilibili_auto_publish=bilibili_auto_publish,
    )
    douyin_auto_publish = normalize_douyin_auto_publish(payload.douyin_auto_publish)
    douyin_generate_meta = normalize_douyin_generate_meta(
        payload.douyin_generate_meta,
        douyin_auto_publish=douyin_auto_publish,
    )

    created: list[dict] = []
    existing: list[dict] = []
    errors: list[dict] = []
    seen_video_ids: dict[str, dict] = {}
    pending_create: list[tuple[str, object]] = []
    pending_video_ids: set[str] = set()
    duplicate_after_create: list[tuple[str, str]] = []

    for url in urls:
        try:
            validated_url = validate_video_url(url)
        except ValueError as exc:
            errors.append({"url": url, "detail": str(exc)})
            continue

        prior = seen_video_ids.get(validated_url.video_id)
        if prior is not None:
            existing.append({"url": url, "task": prior})
            continue

        existing_id = database.find_task_by_video_id(validated_url.video_id)
        if existing_id:
            task = database.get_task(existing_id)
            seen_video_ids[validated_url.video_id] = task
            existing.append({"url": url, "task": task})
            continue

        if validated_url.video_id in pending_video_ids:
            duplicate_after_create.append((url, validated_url.video_id))
            continue

        pending_video_ids.add(validated_url.video_id)
        pending_create.append((url, validated_url))

    if pending_create:
        _ensure_runtime_ready()
        for url, validated_url in pending_create:
            task_id = database.create_task(
                validated_url.url,
                task_id=validated_url.video_id,
                execution_mode=execution_mode,
                audio_mode=audio_mode,
                tts_provider=tts_provider,
                bilibili_tid=bilibili_tid,
                bilibili_auto_publish=bilibili_auto_publish,
                bilibili_generate_meta=bilibili_generate_meta,
                douyin_auto_publish=douyin_auto_publish,
                douyin_generate_meta=douyin_generate_meta,
            )
            worker.enqueue(task_id)
            task = database.get_task(task_id)
            seen_video_ids[validated_url.video_id] = task
            created.append({"url": url, "task": task})

    for url, video_id in duplicate_after_create:
        task = seen_video_ids.get(video_id)
        if task is not None:
            existing.append({"url": url, "task": task})

    return {
        "created": created,
        "existing": existing,
        "errors": errors,
    }


def _normalize_package_export_dir(value: str | None = None) -> str:
    folder = (value if value is not None else package_export_dir_name()).strip() or package_export_dir_name()
    if any(ch in folder for ch in ('/', '\\', ':', '*', '?', '"', '<', '>', '|')):
        raise HTTPException(status_code=422, detail="export directory name contains invalid characters.")
    return folder


@app.post("/api/task-packages/scan")
def scan_task_package(payload: TaskPackageScan) -> dict:
    try:
        source_dir = validate_source_dir(payload.source_dir)
        export_dir = _normalize_package_export_dir(package_export_dir_name())
        files = scan_source_dir(
            source_dir,
            glob=payload.glob,
            recursive=payload.recursive,
            skip_if_export_exists=payload.skip_if_export_exists,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "source_dir": str(source_dir),
        "output_suffix": export_dir,
        "count": len(files),
        "files": files,
    }


@app.post("/api/task-packages", status_code=201)
def create_task_package(payload: TaskPackageCreate) -> dict:
    try:
        source_dir = validate_source_dir(payload.source_dir)
        export_dir = _normalize_package_export_dir(package_export_dir_name())
        direction = package_db.normalize_direction(payload.direction)
        execution_mode = database.normalize_execution_mode(payload.execution_mode)
        audio_mode = database.normalize_audio_mode(payload.audio_mode)
        tts_provider = database.normalize_tts_provider(payload.tts_provider)
        files = scan_source_dir(
            source_dir,
            glob=payload.glob,
            recursive=payload.recursive,
            skip_if_export_exists=payload.skip_if_export_exists,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    runnable_items = [item for item in files if not item.get("will_skip")]
    if not runnable_items and payload.skip_if_export_exists:
        raise HTTPException(status_code=422, detail="All matching files already have exported outputs.")

    _ensure_runtime_ready()
    package_name = payload.name.strip() or source_dir.name
    package_id = package_db.create_package(
        name=package_name,
        source_root=str(source_dir),
        output_suffix=export_dir,
        direction=direction,
        execution_mode=execution_mode,
        audio_mode=audio_mode,
        tts_provider=tts_provider,
        export_subtitle=payload.export_subtitle,
        continue_on_error=payload.continue_on_error,
        skip_if_export_exists=payload.skip_if_export_exists,
        items=[
            {
                "source_path": item["source_path"],
                "relative_path": item.get("relative_path"),
                "title": item.get("title"),
            }
            for item in files
        ],
    )
    worker.enqueue_package(package_id)
    package = package_db.get_package(package_id)
    if package is None:
        raise RuntimeError(f"Package {package_id} was not persisted.")
    return package


@app.get("/api/task-packages")
def list_task_packages(limit: int = Query(50, ge=1, le=200)) -> dict:
    packages = package_db.list_packages(limit=limit)
    return {"packages": packages}


@app.get("/api/task-packages/{package_id}")
def get_task_package(package_id: str) -> dict:
    package = package_db.get_package(package_id)
    if package is None:
        raise HTTPException(status_code=404, detail="Package not found.")
    return package


@app.post("/api/task-packages/{package_id}/continue")
def continue_task_package(package_id: str, payload: TaskPackageContinue | None = None) -> dict:
    package = package_db.get_package(package_id)
    if package is None:
        raise HTTPException(status_code=404, detail="Package not found.")
    if package["status"] not in ("paused", "partial", "failed"):
        raise HTTPException(status_code=409, detail="Package is not waiting to continue.")
    if payload and payload.execution_mode is not None:
        package_db.update_package(
            package_id,
            execution_mode=database.normalize_execution_mode(payload.execution_mode),
        )
    package_db.queue_package_for_continue(package_id)
    worker.enqueue_package(package_id)
    refreshed = package_db.get_package(package_id)
    return refreshed or package


@app.post("/api/task-packages/{package_id}/pause")
def pause_task_package(package_id: str) -> dict:
    package = package_db.get_package(package_id)
    if package is None:
        raise HTTPException(status_code=404, detail="Package not found.")
    if not package_db.request_package_pause(package_id):
        raise HTTPException(
            status_code=409,
            detail="Only queued or running packages can be paused.",
        )
    refreshed = package_db.get_package(package_id)
    return refreshed or package


@app.post("/api/task-packages/{package_id}/retry-failed")
def retry_failed_task_package(package_id: str) -> dict:
    package = package_db.get_package(package_id)
    if package is None:
        raise HTTPException(status_code=404, detail="Package not found.")
    if package["status"] in ("running", "queued"):
        raise HTTPException(
            status_code=409,
            detail="Cannot retry failed items while the package is still queued or running.",
        )
    reset_count = package_db.reset_failed_package_items(package_id)
    if reset_count <= 0:
        raise HTTPException(status_code=409, detail="No failed items to retry.")
    worker.enqueue_package(package_id)
    refreshed = package_db.get_package(package_id)
    payload = refreshed or package
    payload["retried_count"] = reset_count
    return payload


def _purge_package(package_id: str) -> bool:
    package = package_db.get_package(package_id)
    if package is None:
        return False
    for item in package.get("items") or []:
        item_log = package_db.item_log_path(item["id"])
        if item_log.exists():
            item_log.unlink(missing_ok=True)
    session_root = WORKFOLDER / "packages" / package_id
    if session_root.exists():
        shutil.rmtree(session_root, ignore_errors=True)
    log_file = package_db.log_path(package_id)
    if log_file.exists():
        log_file.unlink(missing_ok=True)
    return package_db.delete_package(package_id)


def _cleanup_package_files(package_id: str) -> dict[str, Any]:
    """Remove on-disk artifacts for a package while keeping the DB record and log."""
    package = package_db.get_package(package_id)
    if package is None:
        raise ValueError("Package not found.")
    removed: list[str] = []
    warnings: list[str] = []
    session_root = WORKFOLDER / "packages" / package_id
    if session_root.exists():
        try:
            shutil.rmtree(session_root)
            removed.append(str(session_root))
        except OSError as exc:
            warnings.append(f"session remove failed: {exc}")
            raise RuntimeError(
                f"无法删除批处理会话目录（文件可能被占用）：{session_root} ({exc})"
            ) from exc
    for item in package.get("items") or []:
        item_log = package_db.item_log_path(item["id"])
        if item_log.exists():
            try:
                item_log.unlink(missing_ok=True)
                removed.append(str(item_log))
            except OSError as exc:
                warnings.append(f"item log remove failed ({item['id']}): {exc}")
        package_db.update_package_item(
            item["id"],
            session_path=None,
            final_video_path=None,
        )
    return {"id": package_id, "removed": removed, "warnings": warnings}


@app.delete("/api/task-packages/{package_id}")
def delete_task_package(package_id: str) -> dict:
    if not _purge_package(package_id):
        raise HTTPException(status_code=404, detail="Package not found.")
    return {"deleted": True, "id": package_id}


@app.post("/api/task-packages/batch-delete")
def batch_delete_task_packages(payload: PackageBatchRequest) -> dict:
    raw_ids = [str(package_id).strip() for package_id in payload.package_ids if str(package_id).strip()]
    if not raw_ids:
        raise HTTPException(status_code=422, detail="package_ids must not be empty.")
    if len(raw_ids) > MAX_BATCH_PACKAGE_OPS:
        raise HTTPException(
            status_code=422,
            detail=f"At most {MAX_BATCH_PACKAGE_OPS} packages can be deleted at once.",
        )
    package_ids = list(dict.fromkeys(raw_ids))
    deleted: list[str] = []
    missing: list[str] = []
    failed: list[dict[str, str]] = []
    for package_id in package_ids:
        try:
            if _purge_package(package_id):
                deleted.append(package_id)
            else:
                missing.append(package_id)
        except Exception as exc:
            logger.exception("Failed to delete package %s during batch delete", package_id)
            failed.append({"id": package_id, "reason": str(exc)})
    return {"deleted": deleted, "skipped": [], "missing": missing, "failed": failed}


@app.post("/api/task-packages/batch-cleanup-files")
def batch_cleanup_package_files(payload: PackageBatchRequest) -> dict:
    raw_ids = [str(package_id).strip() for package_id in payload.package_ids if str(package_id).strip()]
    if not raw_ids:
        raise HTTPException(status_code=422, detail="package_ids must not be empty.")
    if len(raw_ids) > MAX_BATCH_PACKAGE_OPS:
        raise HTTPException(
            status_code=422,
            detail=f"At most {MAX_BATCH_PACKAGE_OPS} packages can be cleaned at once.",
        )
    package_ids = list(dict.fromkeys(raw_ids))
    cleaned: list[str] = []
    skipped: list[dict[str, str]] = []
    missing: list[str] = []
    failed: list[dict[str, str]] = []
    for package_id in package_ids:
        package = package_db.get_package(package_id)
        if not package:
            missing.append(package_id)
            continue
        if package["status"] in ("running", "queued"):
            skipped.append({"id": package_id, "reason": package["status"]})
            continue
        try:
            _cleanup_package_files(package_id)
            cleaned.append(package_id)
        except Exception as exc:
            logger.exception("Failed to clean files for package %s during batch cleanup", package_id)
            failed.append({"id": package_id, "reason": str(exc)})
    return {"cleaned": cleaned, "skipped": skipped, "missing": missing, "failed": failed}


@app.post("/api/task-packages/batch-retry-failed")
def batch_retry_failed_task_packages(payload: PackageBatchRequest) -> dict:
    raw_ids = [str(package_id).strip() for package_id in payload.package_ids if str(package_id).strip()]
    if not raw_ids:
        raise HTTPException(status_code=422, detail="package_ids must not be empty.")
    if len(raw_ids) > MAX_BATCH_PACKAGE_OPS:
        raise HTTPException(
            status_code=422,
            detail=f"At most {MAX_BATCH_PACKAGE_OPS} packages can be retried at once.",
        )
    package_ids = list(dict.fromkeys(raw_ids))
    retried: list[str] = []
    skipped: list[dict[str, str]] = []
    missing: list[str] = []
    failed: list[dict[str, str]] = []
    candidates: list[str] = []
    for package_id in package_ids:
        package = package_db.get_package(package_id)
        if not package:
            missing.append(package_id)
            continue
        if package["status"] in ("running", "queued"):
            skipped.append({"id": package_id, "reason": package["status"]})
            continue
        failed_count = int(package.get("failed_count") or 0)
        if failed_count <= 0:
            skipped.append({"id": package_id, "reason": "no_failed_items"})
            continue
        candidates.append(package_id)
    if candidates:
        _ensure_runtime_ready()
    for package_id in candidates:
        try:
            reset_count = package_db.reset_failed_package_items(package_id)
            if reset_count <= 0:
                skipped.append({"id": package_id, "reason": "no_failed_items"})
                continue
            worker.enqueue_package(package_id)
            retried.append(package_id)
        except Exception as exc:
            logger.exception("Failed to retry package %s during batch retry", package_id)
            failed.append({"id": package_id, "reason": str(exc)})
    return {"retried": retried, "skipped": skipped, "missing": missing, "failed": failed}


def _clean_upload_filename(filename: str | None) -> str:
    original = Path(filename or "").name.strip()
    if not original:
        raise HTTPException(status_code=422, detail="Video filename is required.")
    suffix = Path(original).suffix.lower()
    if suffix not in ALLOWED_VIDEO_SUFFIXES:
        raise HTTPException(status_code=422, detail="Unsupported video file type.")
    safe_stem = sanitize_text(Path(original).stem) or "video"
    return f"{safe_stem}{suffix}"


def _clean_subtitle_filename(filename: str | None) -> str:
    original = Path(filename or "").name.strip()
    if not original:
        raise HTTPException(status_code=422, detail="Subtitle filename is required.")
    suffix = Path(original).suffix.lower()
    if suffix not in ALLOWED_SUBTITLE_SUFFIXES:
        raise HTTPException(status_code=422, detail="Only .srt subtitle files are supported.")
    safe_stem = sanitize_text(Path(original).stem) or "subtitles"
    return f"{safe_stem}{suffix}"


def _save_uploaded_file(file: UploadFile, destination: Path, *, max_bytes: int, too_large_detail: str) -> int:
    total = 0
    created = False
    try:
        with runtime_security.open_private_binary_exclusive(destination) as handle:
            created = True
            while True:
                chunk = file.file.read(LOCAL_UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(status_code=413, detail=too_large_detail)
                handle.write(chunk)
    except Exception:
        if created:
            runtime_security.remove_private_file(destination, missing_ok=True)
        raise
    if total == 0:
        runtime_security.remove_private_file(destination, missing_ok=True)
        raise HTTPException(status_code=422, detail="Uploaded file is empty.")
    return total


def _validate_uploaded_srt(path: Path) -> None:
    try:
        parse_srt(path.read_text(encoding="utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid SRT subtitle file encoding.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid SRT subtitle file: {exc}") from exc


def _rollback_local_upload(task_id: str) -> None:
    try:
        remove_upload(WORKFOLDER, task_id)
    except Exception:
        logger.exception("Failed to remove local upload files for task %s", task_id)
    try:
        database.delete_task(task_id)
    except Exception:
        logger.exception("Failed to remove local upload database record for task %s", task_id)


@app.post("/api/tasks/upload", status_code=201)
def upload_local_video(
    direction: str = Form("en-zh"),
    file: UploadFile = File(...),
    subtitle_file: UploadFile | None = File(None),
    execution_mode: str = Form("auto"),
    audio_mode: str = Form("replace"),
    tts_provider: str = Form("azure"),
    bilibili_tid: int = Form(229),
    bilibili_auto_publish: str = Form("true"),
    bilibili_generate_meta: str = Form("true"),
    douyin_auto_publish: str = Form("false"),
    douyin_generate_meta: str = Form("true"),
) -> dict:
    if direction not in LOCAL_UPLOAD_DIRECTIONS:
        raise HTTPException(status_code=422, detail="Unsupported local video direction.")

    original_name = Path(file.filename or "").name.strip()
    stored_name = _clean_upload_filename(original_name)
    stored_subtitle_name = None
    if subtitle_file is not None:
        stored_subtitle_name = _clean_subtitle_filename(subtitle_file.filename)
    normalized_execution_mode = normalize_execution_mode(execution_mode)
    normalized_audio_mode = normalize_audio_mode(audio_mode)
    normalized_tts_provider = normalize_tts_provider(tts_provider)
    normalized_bilibili_tid = normalize_bilibili_tid(bilibili_tid)
    normalized_auto_publish = normalize_bilibili_auto_publish(bilibili_auto_publish)
    normalized_generate_meta = normalize_bilibili_generate_meta(
        bilibili_generate_meta,
        bilibili_auto_publish=normalized_auto_publish,
    )
    normalized_douyin_auto = normalize_douyin_auto_publish(douyin_auto_publish)
    normalized_douyin_meta = normalize_douyin_generate_meta(
        douyin_generate_meta,
        douyin_auto_publish=normalized_douyin_auto,
    )
    _ensure_runtime_ready()

    task_id = str(uuid.uuid4())
    try:
        _save_uploaded_file(
            file,
            uploaded_video_dir(WORKFOLDER, task_id) / stored_name,
            max_bytes=MAX_LOCAL_UPLOAD_BYTES,
            too_large_detail="Uploaded video is too large.",
        )
        if subtitle_file is not None and stored_subtitle_name is not None:
            subtitle_path = uploaded_subtitle_dir(WORKFOLDER, task_id) / stored_subtitle_name
            _save_uploaded_file(
                subtitle_file,
                subtitle_path,
                max_bytes=MAX_LOCAL_SUBTITLE_BYTES,
                too_large_detail="Uploaded subtitle is too large.",
            )
            _validate_uploaded_srt(subtitle_path)

        url = f"local://upload/{task_id}?direction={direction}&filename={quote(original_name)}"
        database.create_task(
            url,
            task_id=task_id,
            execution_mode=normalized_execution_mode,
            audio_mode=normalized_audio_mode,
            tts_provider=normalized_tts_provider,
            bilibili_tid=normalized_bilibili_tid,
            bilibili_auto_publish=normalized_auto_publish,
            bilibili_generate_meta=normalized_generate_meta,
            douyin_auto_publish=normalized_douyin_auto,
            douyin_generate_meta=normalized_douyin_meta,
        )
        database.update_task(task_id, title=Path(original_name).stem)
        task = database.get_task(task_id)
        if task is None:
            raise RuntimeError(f"Local upload task {task_id} was not persisted.")
        worker.enqueue(task_id)
        return task
    except Exception:
        _rollback_local_upload(task_id)
        raise


@app.get("/api/tasks/current")
def current_task() -> dict | None:
    return database.get_current_task()


@app.get("/api/tasks")
def list_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    q: str = Query("", max_length=200),
    status: TaskListStatus = "all",
    execution_mode: TaskListExecutionMode = "all",
    sort: TaskListSort = "created_desc",
) -> dict:
    return database.list_tasks_page(
        page=page,
        page_size=page_size,
        query=q,
        status=status,
        execution_mode=execution_mode,
        sort=sort,
    )


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: str) -> dict:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    return task


def _is_inside_workfolder(path: Path) -> bool:
    workfolder = WORKFOLDER.resolve()
    try:
        path.resolve().relative_to(workfolder)
    except ValueError:
        return False
    return True


def _purge_task(task: dict) -> None:
    session_path = task.get("session_path")
    if session_path:
        session_dir = Path(session_path)
        if session_dir.exists() and _is_inside_workfolder(session_dir):
            shutil.rmtree(session_dir)
    log_file = database.log_path(task["id"])
    if log_file.exists():
        log_file.unlink()
    database.delete_task(task["id"])


_CLEANUP_NAMED_SUFFIXES = (
    ".mp4",
    ".srt",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".txt",
    ".bilibili.txt",
)


def _unlink_quiet(path: Path) -> str | None:
    try:
        if not path.is_file():
            return None
        path.unlink(missing_ok=True)
        return str(path)
    except OSError as exc:
        logger.warning("Skip removing %s: %s", path, exc)
        return None


def _remove_task_named_files(
    directory: Path | None,
    task_id: str,
    *,
    title: str | None = None,
) -> list[str]:
    """Delete export/staging files for a task by known names (no full directory listing)."""
    from .adapters.export_video import export_basename

    if directory is None or not directory.is_dir():
        return []

    stems = {export_basename(task_id=task_id, title=title), f"video__{task_id}", task_id}
    removed: list[str] = []
    seen: set[Path] = set()
    for stem in stems:
        for suffix in _CLEANUP_NAMED_SUFFIXES:
            path = directory / f"{stem}{suffix}"
            if path in seen:
                continue
            seen.add(path)
            deleted = _unlink_quiet(path)
            if deleted:
                removed.append(deleted)
    return removed


def _cleanup_task_files(task: dict) -> dict[str, Any]:
    """Remove on-disk artifacts for a task while keeping the DB record and log.

    Intentionally does not touch the configured output/export directory.
    """
    from .bilibili.staging import staging_dir

    task_id = task["id"]
    title = task.get("title")
    removed: list[str] = []
    warnings: list[str] = []

    session_path = task.get("session_path")
    session_dir = Path(session_path) if session_path else None
    if session_dir is not None:
        try:
            exists = session_dir.exists()
        except OSError as exc:
            warnings.append(f"session check failed: {exc}")
            exists = False
        if exists and _is_inside_workfolder(session_dir):
            try:
                shutil.rmtree(session_dir)
                removed.append(str(session_dir))
            except OSError as exc:
                warnings.append(f"session remove failed: {exc}")
                raise RuntimeError(
                    f"无法删除会话目录（文件可能被占用）：{session_dir} ({exc})"
                ) from exc

    final_video_path = task.get("final_video_path")
    if final_video_path:
        final_video = Path(final_video_path)
        if _is_inside_workfolder(final_video):
            deleted = _unlink_quiet(final_video)
            if deleted:
                removed.append(deleted)

    if is_local_upload_url(task.get("url") or ""):
        upload_root = uploaded_video_dir(WORKFOLDER, task_id).parent
        try:
            upload_exists = upload_root.exists()
        except OSError as exc:
            warnings.append(f"upload check failed: {exc}")
            upload_exists = False
        if upload_exists and _is_inside_workfolder(upload_root):
            try:
                shutil.rmtree(upload_root)
                removed.append(str(upload_root))
            except OSError as exc:
                warnings.append(f"upload remove failed: {exc}")
                raise RuntimeError(
                    f"无法删除本地上传目录（文件可能被占用）：{upload_root} ({exc})"
                ) from exc

    try:
        removed.extend(_remove_task_named_files(staging_dir(), task_id, title=title))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to clean Bilibili staging files for task %s", task_id)
        warnings.append(f"staging cleanup failed: {exc}")

    database.update_task(task_id, session_path=None, final_video_path=None)
    return {"id": task_id, "removed": removed, "warnings": warnings}


@app.delete("/api/tasks/{task_id}", status_code=204)
def delete_task(task_id: str) -> Response:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    if task["status"] == "running":
        raise HTTPException(status_code=409, detail="Cannot delete a running task.")
    _purge_task(task)
    if is_local_upload_url(task["url"]):
        remove_upload(WORKFOLDER, task["id"])
    return Response(status_code=204)


@app.post("/api/tasks/{task_id}/cleanup-files")
def cleanup_task_files(task_id: str) -> dict:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    if task["status"] == "running":
        raise HTTPException(status_code=409, detail="Cannot clean up files for a running task.")
    try:
        summary = _cleanup_task_files(task)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    refreshed = database.get_task(task_id)
    if not refreshed:
        raise HTTPException(status_code=404, detail="Task not found.")
    return {**refreshed, "cleanup": summary}


@app.post("/api/tasks/batch-delete")
def batch_delete_tasks(payload: TaskBatchDelete) -> dict:
    raw_ids = [str(task_id).strip() for task_id in payload.task_ids if str(task_id).strip()]
    if not raw_ids:
        raise HTTPException(status_code=422, detail="task_ids must not be empty.")
    if len(raw_ids) > MAX_BATCH_DELETE_TASKS:
        raise HTTPException(
            status_code=422,
            detail=f"At most {MAX_BATCH_DELETE_TASKS} tasks can be deleted at once.",
        )

    # Preserve first-seen order while de-duplicating.
    task_ids = list(dict.fromkeys(raw_ids))
    deleted: list[str] = []
    skipped: list[dict[str, str]] = []
    missing: list[str] = []
    failed: list[dict[str, str]] = []

    for task_id in task_ids:
        task = database.get_task(task_id)
        if not task:
            missing.append(task_id)
            continue
        if task["status"] == "running":
            skipped.append({"id": task_id, "reason": "running"})
            continue
        try:
            _purge_task(task)
            if is_local_upload_url(task["url"]):
                remove_upload(WORKFOLDER, task["id"])
            deleted.append(task_id)
        except Exception as exc:
            logger.exception("Failed to delete task %s during batch delete", task_id)
            failed.append({"id": task_id, "reason": str(exc)})

    return {
        "deleted": deleted,
        "skipped": skipped,
        "missing": missing,
        "failed": failed,
    }


@app.post("/api/tasks/batch-cleanup-files")
def batch_cleanup_task_files(payload: TaskBatchDelete) -> dict:
    raw_ids = [str(task_id).strip() for task_id in payload.task_ids if str(task_id).strip()]
    if not raw_ids:
        raise HTTPException(status_code=422, detail="task_ids must not be empty.")
    if len(raw_ids) > MAX_BATCH_CLEANUP_TASKS:
        raise HTTPException(
            status_code=422,
            detail=f"At most {MAX_BATCH_CLEANUP_TASKS} tasks can be cleaned at once.",
        )

    task_ids = list(dict.fromkeys(raw_ids))
    cleaned: list[str] = []
    skipped: list[dict[str, str]] = []
    missing: list[str] = []
    failed: list[dict[str, str]] = []

    for task_id in task_ids:
        task = database.get_task(task_id)
        if not task:
            missing.append(task_id)
            continue
        if task["status"] == "running":
            skipped.append({"id": task_id, "reason": "running"})
            continue
        try:
            _cleanup_task_files(task)
            cleaned.append(task_id)
        except Exception as exc:
            logger.exception("Failed to clean files for task %s during batch cleanup", task_id)
            failed.append({"id": task_id, "reason": str(exc)})

    return {
        "cleaned": cleaned,
        "skipped": skipped,
        "missing": missing,
        "failed": failed,
    }


@app.post("/api/tasks/batch-resume")
def batch_resume_tasks(payload: TaskBatchDelete) -> dict:
    raw_ids = [str(task_id).strip() for task_id in payload.task_ids if str(task_id).strip()]
    if not raw_ids:
        raise HTTPException(status_code=422, detail="task_ids must not be empty.")
    if len(raw_ids) > MAX_BATCH_RESUME_TASKS:
        raise HTTPException(
            status_code=422,
            detail=f"At most {MAX_BATCH_RESUME_TASKS} tasks can be resumed at once.",
        )

    # Preserve first-seen order while de-duplicating.
    task_ids = list(dict.fromkeys(raw_ids))
    resumed: list[str] = []
    skipped: list[dict[str, str]] = []
    missing: list[str] = []
    failed: list[dict[str, str]] = []

    candidates: list[str] = []
    for task_id in task_ids:
        task = database.get_task(task_id)
        if not task:
            missing.append(task_id)
            continue
        if task["status"] != "failed":
            skipped.append({"id": task_id, "reason": task["status"]})
            continue
        candidates.append(task_id)

    if candidates:
        _ensure_runtime_ready()

    for task_id in candidates:
        try:
            database.reset_failed_for_resume(task_id)
            worker.enqueue(task_id)
            resumed.append(task_id)
        except Exception as exc:
            logger.exception("Failed to resume task %s during batch resume", task_id)
            failed.append({"id": task_id, "reason": str(exc)})

    return {
        "resumed": resumed,
        "skipped": skipped,
        "missing": missing,
        "failed": failed,
    }


@app.post("/api/tasks/{task_id}/rerun")
def rerun_task(task_id: str) -> dict:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    if task["status"] == "running":
        raise HTTPException(status_code=409, detail="Cannot rerun a running task.")

    _ensure_runtime_ready()
    url = task["url"]
    execution_mode = task.get("execution_mode") or database.DEFAULT_EXECUTION_MODE
    audio_mode = task.get("audio_mode") or database.DEFAULT_AUDIO_MODE
    tts_provider = task.get("tts_provider") or database.DEFAULT_TTS_PROVIDER
    bilibili_tid = task.get("bilibili_tid") or database.DEFAULT_BILIBILI_TID
    bilibili_auto_publish = database.normalize_bilibili_auto_publish(
        task.get("bilibili_auto_publish")
    )
    bilibili_generate_meta = database.resolve_bilibili_generate_meta(
        task.get("bilibili_generate_meta"),
        bilibili_auto_publish=bilibili_auto_publish,
    )
    douyin_auto_publish = database.normalize_douyin_auto_publish(
        task.get("douyin_auto_publish")
    )
    douyin_generate_meta = database.resolve_douyin_generate_meta(
        task.get("douyin_generate_meta"),
        douyin_auto_publish=douyin_auto_publish,
    )
    _purge_task(task)
    new_id = database.create_task(
        url,
        task_id=task_id,
        execution_mode=execution_mode,
        audio_mode=audio_mode,
        tts_provider=tts_provider,
        bilibili_tid=bilibili_tid,
        bilibili_auto_publish=bilibili_auto_publish,
        bilibili_generate_meta=bilibili_generate_meta,
        douyin_auto_publish=douyin_auto_publish,
        douyin_generate_meta=douyin_generate_meta,
    )
    worker.enqueue(new_id)
    return database.get_task(new_id)


@app.post("/api/tasks/{task_id}/stages/{stage_name}/redo")
def redo_stage(task_id: str, stage_name: str) -> dict:
    if stage_name not in STAGE_NAMES:
        raise HTTPException(status_code=404, detail="Stage not found.")
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    if (task.get("execution_mode") or database.DEFAULT_EXECUTION_MODE) != "manual":
        raise HTTPException(status_code=409, detail="Only manual tasks support per-stage redo.")
    if task["status"] in {"running", "queued"}:
        raise HTTPException(status_code=409, detail="Task is already running or queued.")
    stage = next((item for item in task["stages"] if item["name"] == stage_name), None)
    if not stage:
        raise HTTPException(status_code=404, detail="Stage not found.")
    if stage["status"] not in {"succeeded", "failed"}:
        raise HTTPException(status_code=409, detail="Only completed or failed stages can be redone.")
    _ensure_runtime_ready()
    session_path = task.get("session_path")
    if session_path:
        remove_stage_artifacts(Path(session_path), stage_name, detect_source(task["url"]))
    database.reset_stages_from(task_id, stage_name)
    worker.enqueue(task_id)
    return database.get_task(task_id)


@app.post("/api/tasks/{task_id}/continue")
def continue_task(task_id: str, payload: ContinueTaskRequest | None = None) -> dict:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    if task["status"] != "paused":
        raise HTTPException(status_code=409, detail="Only paused tasks can be continued.")
    if payload and payload.execution_mode is not None:
        database.update_task(task_id, execution_mode=normalize_execution_mode(payload.execution_mode))
    _ensure_runtime_ready()
    database.queue_task_for_continue(task_id)
    worker.enqueue(task_id)
    return database.get_task(task_id)


@app.post("/api/tasks/{task_id}/pause")
def pause_task(task_id: str) -> dict:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    if not database.request_task_pause(task_id):
        raise HTTPException(
            status_code=409,
            detail="Only queued or running tasks can be paused.",
        )
    refreshed = database.get_task(task_id)
    return refreshed or task


@app.post("/api/tasks/{task_id}/resume")
def resume_task(task_id: str) -> dict:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    if task["status"] != "failed":
        raise HTTPException(status_code=409, detail="Only failed tasks can be resumed.")
    _ensure_runtime_ready()
    database.reset_failed_for_resume(task_id)
    worker.enqueue(task_id)
    return database.get_task(task_id)


@app.get("/api/tasks/{task_id}/log", response_class=PlainTextResponse)
def task_log(task_id: str) -> str:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    path = database.log_path(task_id)
    return path.read_text(encoding="utf-8") if path.exists() else ""


@app.get("/api/tasks/{task_id}/artifact/final-video")
def final_video(task_id: str, download: bool = False) -> FileResponse:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    final_path = task.get("final_video_path")
    if not final_path or not Path(final_path).exists():
        raise HTTPException(status_code=404, detail="Final video is not available.")
    name = Path(final_path).name
    if download:
        return FileResponse(final_path, media_type="video/mp4", filename=name)
    headers = {"Content-Disposition": f'inline; filename="{name}"'}
    return FileResponse(final_path, media_type="video/mp4", headers=headers)


@app.get("/api/cookies/youtube")
def get_youtube_cookie() -> dict:
    metadata = runtime_security.private_file_stat(YOUTUBE_COOKIE_PATH)
    exists = metadata is not None
    size = metadata.st_size if metadata else 0
    updated_at = metadata.st_mtime if metadata else None
    return {"exists": exists, "size": size, "updated_at": updated_at, "content": ""}


@app.post("/api/cookies/youtube")
def save_youtube_cookie(payload: YouTubeCookieUpdate) -> dict:
    content = payload.content.strip()
    if content:
        runtime_security.atomic_write_private_text(YOUTUBE_COOKIE_PATH, content + "\n")
    else:
        runtime_security.remove_private_file(YOUTUBE_COOKIE_PATH, missing_ok=True)
    return get_youtube_cookie()


@app.get("/api/settings/openai")
def get_openai_settings() -> dict:
    settings = database.get_openai_settings()
    return {
        "base_url": settings["base_url"],
        "api_key": mask_secret(settings["api_key"]),
        "has_api_key": bool(settings["api_key"]),
        "model": settings["model"],
        "translate_concurrency": settings["translate_concurrency"],
    }


@app.post("/api/settings/openai")
def save_openai_settings(payload: OpenAISettingsUpdate) -> dict:
    try:
        database.save_openai_settings(
            payload.base_url,
            payload.api_key,
            payload.model,
            normalize_translate_concurrency(payload.translate_concurrency),
            clear_api_key=payload.clear_api_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return get_openai_settings()


@app.post("/api/settings/openai/models")
def get_openai_models(payload: OpenAIModelsRequest) -> dict:
    settings = database.get_openai_settings()
    try:
        saved_base_url = validate_openai_base_url(settings["base_url"])
        requested_base_url = payload.base_url.strip()
        base_url = (
            validate_openai_base_url(requested_base_url)
            if requested_base_url
            else saved_base_url
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    requested_api_key = payload.api_key.strip()
    if requested_base_url and base_url != saved_base_url and not requested_api_key:
        raise HTTPException(
            status_code=422,
            detail="An API key is required when testing a different OpenAI base URL.",
        )
    api_key = requested_api_key or settings["api_key"]
    if not api_key:
        raise HTTPException(status_code=400, detail="OpenAI API key is not configured.")
    try:
        models = list_openai_models(base_url=base_url, api_key=api_key)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail="Failed to fetch models from the OpenAI-compatible API.",
        ) from exc
    return {"models": models}


@app.get("/api/settings/ytdlp")
def get_ytdlp_settings() -> dict:
    return database.get_ytdlp_settings()


@app.post("/api/settings/ytdlp")
def save_ytdlp_settings(payload: YtdlpSettingsUpdate) -> dict:
    database.save_ytdlp_settings(normalize_proxy_port(payload.proxy_port))
    return get_ytdlp_settings()


@app.get("/api/settings/output")
def get_output_settings() -> dict:
    return database.get_output_settings()


@app.post("/api/settings/output")
def save_output_settings(payload: OutputSettingsUpdate) -> dict:
    database.save_output_settings(normalize_output_dir(payload.output_dir))
    return get_output_settings()


@app.get("/api/settings/azure-tts")
def get_azure_tts_settings() -> dict:
    from .adapters.azure_tts import parse_subscription_keys

    settings = database.get_azure_tts_settings()
    key_count = len(parse_subscription_keys(settings["subscription_key"]))
    return {
        "subscription_key": mask_secret(settings["subscription_key"]),
        "has_subscription_key": key_count > 0,
        "key_count": key_count,
        "region": settings["region"],
        "voice": settings["voice"],
        "locale": settings["locale"],
        "endpoint": settings["endpoint"],
        "output_format": settings["output_format"],
        "speech_rate": settings["speech_rate"],
        "concurrency": settings["concurrency"],
    }


@app.post("/api/settings/azure-tts")
def save_azure_tts_settings(payload: AzureTtsSettingsUpdate) -> dict:
    speech_rate = payload.speech_rate.strip()
    if speech_rate and speech_rate not in {"0"} and not re.fullmatch(r"[+-]?\d+%?", speech_rate):
        raise HTTPException(
            status_code=422,
            detail="Speech rate must be an integer or percentage like +10%.",
        )
    if speech_rate and not speech_rate.endswith("%"):
        try:
            rate = int(speech_rate)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Speech rate must be numeric.") from exc
        if rate < -50 or rate > 100:
            raise HTTPException(
                status_code=422,
                detail="Speech rate must be between -50 and 100.",
            )
    concurrency = normalize_azure_tts_concurrency(payload.concurrency)
    if not payload.region.strip() and not payload.endpoint.strip():
        raise HTTPException(status_code=422, detail="Azure region or endpoint is required.")
    if not payload.voice.strip():
        raise HTTPException(status_code=422, detail="Azure voice is required.")
    database.save_azure_tts_settings(
        subscription_key=payload.subscription_key,
        clear_subscription_key=payload.clear_subscription_key,
        region=payload.region,
        voice=payload.voice,
        locale=payload.locale,
        endpoint=payload.endpoint,
        output_format=payload.output_format,
        speech_rate=speech_rate,
        concurrency=concurrency,
    )
    return get_azure_tts_settings()


@app.post("/api/settings/azure-tts/voices")
def list_azure_tts_voices(payload: AzureTtsVoicesRequest) -> dict:
    settings = database.get_azure_tts_settings()
    subscription_key = payload.subscription_key.strip()
    if not subscription_key or set(subscription_key) == {"*"}:
        subscription_key = settings["subscription_key"]
    region = payload.region.strip() or settings["region"]
    endpoint = payload.endpoint.strip() or settings["endpoint"]
    try:
        from .adapters.azure_tts import list_voices

        voices = list_voices(
            region=region,
            subscription_key=subscription_key,
            endpoint=endpoint,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to list Azure voices: {exc}",
        ) from exc
    return {"voices": voices}


@app.post("/api/settings/azure-tts/validate-keys")
def validate_azure_tts_keys(payload: AzureTtsValidateKeysRequest) -> dict:
    settings = database.get_azure_tts_settings()
    subscription_key = payload.subscription_key.strip()
    if not subscription_key or set(subscription_key) == {"*"}:
        subscription_key = settings["subscription_key"]
    region = payload.region.strip() or settings["region"]
    endpoint = payload.endpoint.strip() or settings["endpoint"]
    if not region and not endpoint:
        raise HTTPException(status_code=422, detail="Azure region or endpoint is required.")
    try:
        from .adapters.azure_tts import validate_subscription_keys

        return validate_subscription_keys(
            region=region,
            subscription_key=subscription_key,
            endpoint=endpoint,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to validate Azure keys: {exc}",
        ) from exc
