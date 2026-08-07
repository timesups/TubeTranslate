from __future__ import annotations

import asyncio
import base64
import json
import math
import mimetypes
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from .auth import USER_AGENT, load_cookies

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


@dataclass
class UploadMeta:
    title: str
    tid: int
    tag: str
    desc: str = ""
    copyright: int = 1  # 1 自制 2 转载
    source: str = ""
    cover_path: Path | None = None
    dynamic: str = ""
    no_reprint: int = 0
    open_elec: int = 0


@dataclass
class JobState:
    id: str
    status: str = "queued"
    progress: float = 0.0
    message: str = "等待开始"
    result: dict[str, Any] | None = None
    error: str | None = None
    listeners: list[asyncio.Queue] = field(default_factory=list)

    async def publish(self) -> None:
        payload = self.to_dict()
        for queue in list(self.listeners):
            await queue.put(payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "progress": round(self.progress, 2),
            "message": self.message,
            "result": self.result,
            "error": self.error,
        }


jobs: dict[str, JobState] = {}


def create_job() -> JobState:
    job = JobState(id=uuid.uuid4().hex[:12])
    jobs[job.id] = job
    return job


async def _notify(job: JobState, **kwargs: Any) -> None:
    for key, value in kwargs.items():
        setattr(job, key, value)
    await job.publish()


async def upload_video_file(
    client: httpx.AsyncClient,
    file_path: Path,
    job: JobState,
    progress_start: float = 5.0,
    progress_end: float = 90.0,
) -> dict[str, Any]:
    filename = file_path.name
    filesize = file_path.stat().st_size

    await _notify(job, status="uploading", message="申请上传通道…", progress=progress_start)

    pre = await client.get(
        "https://member.bilibili.com/preupload",
        params={
            "name": filename,
            "size": filesize,
            "r": "upos",
            "profile": "ugcupos/bup",
            "ssl": "0",
            "version": "2.14.0",
            "build": "2140000",
            "webVersion": "2.14.0",
        },
    )
    pre.raise_for_status()
    info = pre.json()
    if "upos_uri" not in info:
        raise RuntimeError(f"预上传失败：{info}")

    endpoint = info.get("endpoint") or "//upos-sz-upcdnbda2.bilivideo.com"
    if endpoint.startswith("//"):
        endpoint = "https:" + endpoint
    elif endpoint.startswith("http"):
        pass
    else:
        endpoint = "https://" + endpoint.lstrip("/")

    upos_uri = info["upos_uri"]
    object_key = upos_uri.replace("upos://", "")
    auth = info["auth"]
    biz_id = info["biz_id"]
    chunk_size = int(info.get("chunk_size") or 4 * 1024 * 1024)
    bili_filename = Path(object_key).stem

    upload_headers = {
        "User-Agent": USER_AGENT,
        "X-Upos-Auth": auth,
        "Origin": "https://member.bilibili.com",
        "Referer": "https://member.bilibili.com/",
    }

    init = await client.post(
        f"{endpoint}/{object_key}",
        params={"uploads": "", "output": "json"},
        headers=upload_headers,
    )
    init.raise_for_status()
    upload_id = init.json()["upload_id"]

    total_chunks = max(1, math.ceil(filesize / chunk_size))
    parts: list[dict[str, Any]] = []

    await _notify(job, message=f"开始分片上传（共 {total_chunks} 片）…")

    with file_path.open("rb") as fp:
        for index in range(total_chunks):
            chunk = fp.read(chunk_size)
            if not chunk:
                break
            start = index * chunk_size
            end = start + len(chunk)
            params = {
                "partNumber": index + 1,
                "uploadId": upload_id,
                "chunk": index,
                "chunks": total_chunks,
                "size": len(chunk),
                "start": start,
                "end": end,
                "total": filesize,
            }
            for attempt in range(3):
                resp = await client.put(
                    f"{endpoint}/{object_key}",
                    params=params,
                    content=chunk,
                    headers=upload_headers,
                    timeout=120.0,
                )
                if resp.status_code == 200:
                    break
                if attempt == 2:
                    raise RuntimeError(f"分片 {index + 1} 上传失败：{resp.status_code} {resp.text}")
                await asyncio.sleep(1.5)

            parts.append({"partNumber": index + 1, "eTag": "etag"})
            ratio = (index + 1) / total_chunks
            progress = progress_start + (progress_end - progress_start) * ratio
            await _notify(
                job,
                progress=progress,
                message=f"上传中 {index + 1}/{total_chunks}（{progress:.0f}%）",
            )

    await _notify(job, message="合并分片…", progress=progress_end)
    finish = await client.post(
        f"{endpoint}/{object_key}",
        params={
            "output": "json",
            "name": filename,
            "profile": "ugcupos/bup",
            "uploadId": upload_id,
            "biz_id": biz_id,
        },
        headers={**upload_headers, "Content-Type": "application/json"},
        content=json.dumps({"parts": parts}),
    )
    finish.raise_for_status()
    finish_data = finish.json()
    if finish_data.get("OK") != 1 and finish_data.get("ok") != 1:
        # 有些线路返回 success / location
        if str(finish_data.get("message", "")).upper() not in {"OK", ""} and finish_data.get("code") not in (0, None):
            raise RuntimeError(f"合并分片失败：{finish_data}")

    return {
        "filename": bili_filename,
        "cid": biz_id,
        "title": file_path.stem[:80],
        "desc": "",
    }


def _cover_data_url(cover_path: Path) -> str:
    mime, _ = mimetypes.guess_type(cover_path.name)
    if not mime or not mime.startswith("image/"):
        suffix = cover_path.suffix.lower().lstrip(".")
        if suffix == "jpg":
            suffix = "jpeg"
        mime = f"image/{suffix or 'jpeg'}"
    encoded = base64.b64encode(cover_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


async def upload_cover(client: httpx.AsyncClient, cover_path: Path, csrf: str) -> str:
    # B 站封面接口要求 application/x-www-form-urlencoded + base64，不是 multipart 文件
    resp = await client.post(
        "https://member.bilibili.com/x/vu/web/cover/up",
        params={"ts": int(time.time() * 1000)},
        data={
            "csrf": csrf,
            "cover": _cover_data_url(cover_path),
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("code") != 0:
        raise RuntimeError(
            f"封面上传失败（{payload.get('code')}）：{payload.get('message') or payload}"
        )
    url = payload["data"]["url"]
    return url.replace("http:", "").replace("https:", "")


async def submit_archive(
    client: httpx.AsyncClient,
    meta: UploadMeta,
    videos: list[dict[str, Any]],
    cover_url: str = "",
) -> dict[str, Any]:
    cookies = load_cookies()
    csrf = cookies.get("bili_jct")
    if not csrf:
        raise RuntimeError("缺少 bili_jct，请重新登录")

    tags = [t.strip() for t in meta.tag.replace("，", ",").split(",") if t.strip()]
    if not tags:
        tags = ["生活"]
    tag_str = ",".join(tags[:10])

    # 投稿接口要求 cover 形如 //i0.hdslb.com/... 或完整 https URL
    if cover_url.startswith("https:"):
        cover_url = cover_url[6:]
    elif cover_url.startswith("http:"):
        cover_url = cover_url[5:]

    normalized_videos = []
    for item in videos:
        normalized_videos.append(
            {
                "filename": item["filename"],
                "title": (item.get("title") or meta.title)[:80],
                "desc": item.get("desc") or "",
                "cid": item["cid"],
            }
        )

    body = {
        "videos": normalized_videos,
        "cover": cover_url,
        "cover43": "",
        "title": meta.title[:80],
        "copyright": int(meta.copyright),
        "tid": int(meta.tid),
        "tag": tag_str,
        "desc_format_id": 9999,
        "desc": meta.desc or "",
        "recreate": -1,
        "dynamic": meta.dynamic or "",
        "interactive": 0,
        "act_reserve_create": 0,
        "no_disturbance": 0,
        "no_reprint": int(meta.no_reprint),
        "subtitle": {"open": 0, "lan": ""},
        "dolby": 0,
        "lossless_music": 0,
        "up_selection_reply": False,
        "up_close_reply": False,
        "up_close_danmu": False,
        "web_os": 3,
        "open_elec": int(meta.open_elec),
    }
    if meta.copyright == 2:
        body["source"] = meta.source

    resp = await client.post(
        "https://member.bilibili.com/x/vu/web/add/v3",
        params={"ts": int(time.time() * 1000), "csrf": csrf},
        content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json;charset=UTF-8"},
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("code") != 0:
        raise RuntimeError(
            f"提交稿件失败（{payload.get('code')}）：{payload.get('message') or payload}"
        )
    return payload["data"]

async def run_upload_job(
    job: JobState,
    video_path: Path,
    meta: UploadMeta,
    *,
    cleanup: bool = True,
) -> None:
    try:
        cookies = load_cookies()
        if not cookies.get("SESSDATA"):
            raise RuntimeError("未登录，请先在设置中扫码登录 B 站")

        async with httpx.AsyncClient(
            cookies=cookies,
            headers={
                "User-Agent": USER_AGENT,
                "Referer": "https://member.bilibili.com/",
                "Origin": "https://member.bilibili.com",
            },
            timeout=60.0,
            follow_redirects=True,
        ) as client:
            await _notify(job, status="uploading", message="开始上传视频…", progress=2)
            video_part = await upload_video_file(client, video_path, job)
            # 分 P 标题用投稿标题更合适
            video_part["title"] = meta.title[:80]

            cover_url = ""
            if meta.cover_path and meta.cover_path.exists():
                await _notify(job, message="上传封面…", progress=92)
                cover_url = await upload_cover(client, meta.cover_path, cookies["bili_jct"])

            await _notify(job, message="提交稿件…", progress=96)
            result = await submit_archive(client, meta, [video_part], cover_url)
            await _notify(
                job,
                status="success",
                progress=100,
                message="投稿成功",
                result={
                    "aid": result.get("aid"),
                    "bvid": result.get("bvid"),
                },
            )
    except Exception as exc:  # noqa: BLE001
        await _notify(
            job,
            status="error",
            message="投稿失败",
            error=str(exc),
        )
    finally:
        if not cleanup:
            return
        # 清理浏览器上传产生的临时文件
        try:
            if video_path.exists():
                video_path.unlink()
        except OSError:
            pass
        if meta.cover_path:
            try:
                if meta.cover_path.exists():
                    meta.cover_path.unlink()
            except OSError:
                pass
