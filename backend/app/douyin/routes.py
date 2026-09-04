from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from .. import database
from . import auth
from .meta import generate_douyin_meta
from .publisher import PublishMeta, create_job, jobs, run_publish_job
from .staging import prepare_douyin_staging, staging_dir

router = APIRouter(prefix="/api/douyin", tags=["douyin"])


class SettingsBody(BaseModel):
    default_tags: str | None = None
    headless_publish: bool | None = None
    publish_timeout_sec: int | None = Field(default=None, ge=60, le=3600)


class LoginBody(BaseModel):
    timeout_sec: int = Field(default=300, ge=60, le=900)


class GenerateBody(BaseModel):
    task_id: str


class PublishItem(BaseModel):
    task_id: str | None = None
    title: str
    tags: str = ""
    video_path: str
    cover_path: str | None = None


class PublishBody(BaseModel):
    items: list[PublishItem]


class StageFromTaskBody(BaseModel):
    task_id: str


@router.get("/auth/status")
def auth_status() -> dict[str, Any]:
    status = auth.get_login_status(headed_probe=False)
    session = auth.login_session_status()
    return {**status, "login_session": session}


@router.post("/auth/login")
def auth_login(payload: LoginBody | None = None) -> dict[str, Any]:
    body = payload or LoginBody()
    return auth.start_interactive_login(timeout_sec=body.timeout_sec)


@router.get("/auth/login/status")
def auth_login_status() -> dict[str, Any]:
    return auth.login_session_status()


@router.delete("/auth/logout")
def auth_logout() -> dict[str, Any]:
    auth.logout()
    return {"ok": True, "logged_in": False}


@router.get("/settings")
def get_settings() -> dict[str, Any]:
    return auth.settings_public()


@router.post("/settings")
def update_settings(payload: SettingsBody) -> dict[str, Any]:
    return auth.settings_public(
        auth.save_settings(payload.model_dump(exclude_none=True)),
    )


@router.post("/stage-from-task")
def stage_from_task(payload: StageFromTaskBody) -> dict[str, Any]:
    task = database.get_task(payload.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    final_video = Path(str(task.get("final_video_path") or ""))
    if not final_video.exists():
        raise HTTPException(status_code=409, detail="Task final video is missing.")
    session = Path(str(task.get("session_path") or "")) if task.get("session_path") else None
    package = prepare_douyin_staging(
        task_id=task["id"],
        title=task.get("title"),
        final_video=final_video,
        session=session if session and session.exists() else None,
    )
    return {
        "stem": package.stem,
        "video_path": str(package.video),
        "cover_path": str(package.cover) if package.cover else None,
        "subtitle_path": str(package.subtitle) if package.subtitle else None,
        "staging_dir": str(staging_dir()),
    }


@router.post("/generate")
async def generate(payload: GenerateBody) -> dict[str, Any]:
    task = database.get_task(payload.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    final_video = Path(str(task.get("final_video_path") or ""))
    if not final_video.exists():
        raise HTTPException(status_code=409, detail="Task final video is missing.")
    session = Path(str(task.get("session_path") or "")) if task.get("session_path") else None
    package = prepare_douyin_staging(
        task_id=task["id"],
        title=task.get("title"),
        final_video=final_video,
        session=session if session and session.exists() else None,
    )
    subtitle_text = ""
    if package.subtitle and package.subtitle.exists():
        from ..bilibili.media_scan import read_srt

        subtitle_text = read_srt(package.subtitle)
    meta = await generate_douyin_meta(
        filename=package.video.name,
        subtitle_text=subtitle_text,
        source_url=str(task.get("url") or ""),
        original_title=str(task.get("title") or "").strip() or None,
    )
    return {
        "task_id": task["id"],
        "title": meta["title"],
        "tags": meta["tag_str"],
        "video_path": str(package.video),
        "cover_path": str(package.cover) if package.cover else None,
    }


@router.post("/publish")
async def publish(payload: PublishBody, background: BackgroundTasks) -> dict[str, Any]:
    if not payload.items:
        raise HTTPException(status_code=422, detail="items is required")
    status = auth.get_login_status(headed_probe=False)
    if not status.get("logged_in"):
        raise HTTPException(status_code=401, detail="抖音未登录，请先在设置中扫码登录")

    created = []
    for item in payload.items:
        video_path = Path(item.video_path)
        if not video_path.exists():
            raise HTTPException(status_code=404, detail=f"视频不存在: {item.video_path}")
        job = create_job()
        meta = PublishMeta(
            title=item.title,
            tags=item.tags,
            video_path=video_path,
            cover_path=Path(item.cover_path) if item.cover_path else None,
        )

        async def _run(job_id: str = job.id, publish_meta: PublishMeta = meta) -> None:
            current = jobs[job_id]
            try:
                await run_publish_job(publish_meta, current)
            except Exception:
                pass

        background.add_task(_run)
        created.append(job.to_dict())
    return {"jobs": created}


@router.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job.to_dict()
