from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

import qrcode
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from .. import database
from . import auth
from .deepseek_meta import (
    generate_bilibili_meta,
    load_settings,
    save_settings,
    settings_public,
)
from .media_scan import read_srt, scan_video_folder
from .partitions import PARTITIONS
from .staging import prepare_task_staging, staging_dir
from .uploader import UploadMeta, create_job, jobs, run_upload_job

router = APIRouter(prefix="/api/bilibili", tags=["bilibili"])


class CookieBody(BaseModel):
    cookie: str = Field(..., min_length=10)


class QrPollBody(BaseModel):
    qrcode_key: str


class SettingsBody(BaseModel):
    default_tid: int | None = None
    default_tag: str | None = None
    default_copyright: int | None = None
    video_dir: str | None = None


class GenerateBody(BaseModel):
    id: str


class PublishItem(BaseModel):
    id: str
    title: str
    desc: str
    tag: str
    dynamic: str = ""
    tid: int | None = None
    copyright: int | None = None
    source: str = ""


class PublishBody(BaseModel):
    items: list[PublishItem]


class StageFromTaskBody(BaseModel):
    task_id: str


def _video_dir() -> Path:
    return staging_dir()


def _find_item(item_id: str) -> dict[str, Any]:
    items = scan_video_folder(_video_dir())
    for item in items:
        if item["id"] == item_id or item["stem"] == item_id or item["name"] == item_id:
            return item
    raise HTTPException(status_code=404, detail=f"未找到视频：{item_id}")


@router.get("/auth/status")
async def auth_status() -> dict[str, Any]:
    return await auth.get_login_status()


@router.post("/auth/cookie")
async def auth_cookie(body: CookieBody) -> dict[str, Any]:
    try:
        return await auth.save_cookie_login(body.cookie)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"登录失败：{exc}") from exc


@router.delete("/auth/logout")
async def auth_logout() -> dict[str, bool]:
    auth.clear_cookies()
    return {"ok": True}


@router.post("/auth/qr/create")
async def auth_qr_create() -> dict[str, Any]:
    try:
        data = await auth.create_qrcode()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    img = qrcode.make(data["url"])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return {
        "qrcode_key": data["qrcode_key"],
        "url": data["url"],
        "image": f"data:image/png;base64,{b64}",
    }


@router.post("/auth/qr/poll")
async def auth_qr_poll(body: QrPollBody) -> dict[str, Any]:
    try:
        return await auth.poll_qrcode(body.qrcode_key)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/partitions")
async def partitions() -> list[dict[str, Any]]:
    seen: set[int] = set()
    result: list[dict[str, Any]] = []
    for item in PARTITIONS:
        tid = item["tid"]
        if tid in seen:
            continue
        seen.add(tid)
        result.append(item)
    return result


@router.get("/settings")
async def get_settings() -> dict[str, Any]:
    return settings_public()


@router.post("/settings")
async def update_settings(body: SettingsBody) -> dict[str, Any]:
    settings = save_settings(body.model_dump(exclude_none=True))
    return settings_public(settings)


@router.get("/ready")
async def ready_list() -> dict[str, Any]:
    folder = _video_dir()
    items = scan_video_folder(folder)
    return {
        "video_dir": str(folder),
        "count": len(items),
        "items": items,
    }


@router.post("/stage-from-task")
async def stage_from_task(body: StageFromTaskBody) -> dict[str, Any]:
    task = database.get_task(body.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    if task.get("status") != "succeeded":
        raise HTTPException(status_code=409, detail="Only succeeded tasks can be staged for publish.")
    final_path = task.get("final_video_path")
    if not final_path:
        raise HTTPException(status_code=409, detail="Task has no final video.")
    session = Path(task["session_path"]) if task.get("session_path") else None
    try:
        package = prepare_task_staging(
            task_id=task["id"],
            title=task.get("title"),
            final_video=Path(final_path),
            session=session,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "id": package.stem,
        "stem": package.stem,
        "video_path": str(package.video),
        "srt_path": str(package.subtitle) if package.subtitle else None,
        "cover_path": str(package.cover) if package.cover else None,
        "ready": package.subtitle is not None and package.cover is not None,
    }


@router.post("/generate")
async def generate(body: GenerateBody) -> dict[str, Any]:
    item = _find_item(body.id)
    if not item.get("srt_path"):
        raise HTTPException(status_code=400, detail="缺少字幕文件，无法生成简介")
    try:
        subtitle = read_srt(Path(item["srt_path"]))
        if not subtitle:
            raise RuntimeError("字幕内容为空")
        meta = await generate_bilibili_meta(filename=item["name"], subtitle_text=subtitle)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    settings = load_settings()
    return {
        "id": item["id"],
        "name": item["name"],
        "cover_path": item.get("cover_path"),
        "srt_path": item.get("srt_path"),
        "title": meta["title"],
        "desc": meta["desc"],
        "tag": meta["tag_str"],
        "dynamic": meta["dynamic"],
        "tid": int(settings.get("default_tid") or 201),
        "copyright": int(settings.get("default_copyright") or 1),
    }


@router.post("/publish")
async def publish(body: PublishBody, background_tasks: BackgroundTasks) -> dict[str, Any]:
    status = await auth.get_login_status()
    if not status.get("logged_in"):
        raise HTTPException(status_code=401, detail="请先登录 B 站账号")
    if not body.items:
        raise HTTPException(status_code=400, detail="没有要投稿的条目")

    settings = load_settings()
    created: list[dict[str, Any]] = []
    queue: list[tuple] = []

    for item in body.items:
        media = _find_item(item.id)
        video_path = Path(media["video_path"])
        if not video_path.exists():
            raise HTTPException(status_code=400, detail=f"视频不存在：{item.id}")
        cover_path = Path(media["cover_path"]) if media.get("cover_path") else None
        if cover_path is None or not cover_path.exists():
            raise HTTPException(status_code=400, detail=f"缺少封面：{item.id}")

        title = item.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail=f"{item.id} 标题为空")
        if not item.desc.strip():
            raise HTTPException(status_code=400, detail=f"{item.id} 简介为空，请先生成")

        copyright_val = int(
            item.copyright if item.copyright is not None else settings.get("default_copyright") or 1
        )
        if copyright_val == 2 and not item.source.strip():
            raise HTTPException(status_code=400, detail=f"{item.id} 转载稿件需要来源")

        job = create_job()
        meta = UploadMeta(
            title=title,
            tid=int(item.tid if item.tid is not None else settings.get("default_tid") or 201),
            tag=item.tag.strip() or str(settings.get("default_tag") or "配音"),
            desc=item.desc.strip(),
            copyright=copyright_val,
            source=item.source.strip(),
            cover_path=cover_path,
            dynamic=item.dynamic.strip(),
        )
        queue.append((job, video_path, meta))
        created.append({"id": item.id, "job_id": job.id, **job.to_dict()})

    async def run_queue() -> None:
        for job, video_path, meta in queue:
            await run_upload_job(job, video_path, meta, cleanup=False)

    background_tasks.add_task(run_queue)
    return {"jobs": created}


@router.get("/jobs/{job_id}")
async def job_status(job_id: str) -> dict[str, Any]:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job.to_dict()
