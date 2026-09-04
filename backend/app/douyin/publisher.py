from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import auth

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


@dataclass
class PublishMeta:
    title: str
    tags: str = ""
    video_path: Path | None = None
    cover_path: Path | None = None


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


def _fill_title(page: Any, title: str) -> None:
    candidates = [
        page.locator("[data-placeholder*='标题']"),
        page.locator("input[placeholder*='标题']"),
        page.locator("textarea[placeholder*='标题']"),
        page.locator(".editor-kit-container [contenteditable='true']").first,
        page.locator("[contenteditable='true']").first,
    ]
    for locator in candidates:
        try:
            if locator.count() == 0:
                continue
            target = locator.first
            if not target.is_visible(timeout=1000):
                continue
            target.click(timeout=3000)
            page.keyboard.press("Control+A")
            page.keyboard.type(title[:55], delay=20)
            return
        except Exception:
            continue
    raise RuntimeError("未能定位抖音标题输入框（创作者中心页面可能已改版）")


def _set_tags(page: Any, tags: str) -> None:
    cleaned = [part.strip().lstrip("#") for part in tags.replace("，", ",").split(",") if part.strip()]
    if not cleaned:
        return
    # Best-effort: append hashtags into the description/title editor.
    try:
        editor = page.locator("[contenteditable='true']").first
        if editor.count() and editor.is_visible(timeout=1000):
            editor.click()
            page.keyboard.press("End")
            for tag in cleaned[:5]:
                page.keyboard.type(f" #{tag}", delay=15)
    except Exception:
        pass


def _click_publish(page: Any) -> None:
    candidates = [
        page.get_by_role("button", name="发布"),
        page.locator("button:has-text('发布')"),
        page.locator("text=发布").last,
    ]
    for locator in candidates:
        try:
            if locator.count() == 0:
                continue
            btn = locator.first
            if btn.is_enabled(timeout=2000):
                btn.click(timeout=5000)
                return
        except Exception:
            continue
    raise RuntimeError("未能点击发布按钮（创作者中心页面可能已改版）")


def publish_video_sync(
    meta: PublishMeta,
    *,
    progress_callback: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    settings = auth.load_settings()
    timeout_sec = int(settings.get("publish_timeout_sec") or 600)
    headless = bool(settings.get("headless_publish"))

    if not auth.has_storage_state():
        raise RuntimeError("抖音未登录；请先在设置中扫码登录创作者中心")
    video_path = meta.video_path
    if video_path is None or not Path(video_path).exists():
        raise RuntimeError(f"视频文件不存在: {video_path}")
    title = (meta.title or "").strip()
    if not title:
        raise RuntimeError("标题不能为空")

    def report(progress: float, message: str) -> None:
        if progress_callback:
            progress_callback(progress, message)

    from playwright.sync_api import sync_playwright

    report(5, "启动浏览器…")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(
            storage_state=str(auth.STORAGE_STATE_PATH),
            user_agent=auth.USER_AGENT,
            locale="zh-CN",
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()
        try:
            report(10, "打开创作者上传页…")
            page.goto(auth.CREATOR_UPLOAD, wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_timeout(2000)
            if not auth._looks_logged_in(page):
                raise RuntimeError("抖音登录态失效，请重新登录")

            report(20, "选择视频文件…")
            file_input = page.locator("input[type='file']").first
            if file_input.count() == 0:
                raise RuntimeError("未找到上传控件 input[type=file]")
            file_input.set_input_files(str(video_path))

            report(40, "等待视频处理…")
            deadline = time.time() + timeout_sec
            while time.time() < deadline:
                # Title field appearing usually means upload processed enough to edit.
                try:
                    if page.locator("[contenteditable='true'], input[placeholder*='标题'], textarea[placeholder*='标题']").first.is_visible(timeout=500):
                        break
                except Exception:
                    pass
                page.wait_for_timeout(2000)
            else:
                raise RuntimeError("等待视频上传处理超时")

            report(70, "填写标题与话题…")
            _fill_title(page, title)
            _set_tags(page, meta.tags or str(settings.get("default_tags") or ""))

            report(85, "点击发布…")
            _click_publish(page)
            page.wait_for_timeout(4000)

            report(100, "发布流程已提交")
            return {
                "title": title,
                "video_path": str(video_path),
                "page_url": page.url,
                "message": "已在创作者中心提交发布（请在抖音 App/网页确认审核结果）",
            }
        finally:
            context.close()
            browser.close()


async def run_publish_job(
    meta: PublishMeta,
    job: JobState,
    *,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()

    def on_progress(progress: float, message: str) -> None:
        job.status = "uploading"
        job.progress = progress
        job.message = message
        if progress_callback:
            result = progress_callback(job.to_dict())
            if asyncio.iscoroutine(result):
                asyncio.run_coroutine_threadsafe(result, loop)

    await _notify(job, status="running", progress=1, message="准备发布到抖音…")
    try:
        result = await asyncio.to_thread(publish_video_sync, meta, progress_callback=on_progress)
        await _notify(job, status="succeeded", progress=100, message="发布完成", result=result, error=None)
        return result
    except Exception as exc:
        await _notify(job, status="failed", message=str(exc), error=str(exc))
        raise
