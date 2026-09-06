from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import auth

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None] | None]

TITLE_MAX_LEN = 30
DEBUG_DIR = auth.DATA_DIR / "debug"

_POST_URL_MARKERS = (
    "/creator-micro/content/post/video",
    "/creator-micro/content/publish",
)
_MANAGE_URL_MARKER = "/creator-micro/content/manage"

_TITLE_SELECTORS = [
    'input[placeholder*="填写作品标题"]',
    'input[placeholder*="作品标题"]',
    'input[placeholder*="添加作品标题"]',
    'textarea[placeholder*="填写作品标题"]',
    'textarea[placeholder*="作品标题"]',
]
_DESC_SELECTORS = [
    'div.zone-container[contenteditable="true"]',
    '.editor-kit-editor-container [contenteditable="true"]',
    'div[data-placeholder*="简介"]',
    'div[data-placeholder*="描述"]',
    'div[data-placeholder*="作品"]',
]

# 勿匹配页面底部常驻文案「还在上传中」——只认「取消上传」等真实忙碌控件。
_UPLOAD_BUSY_SELECTORS = (
    "text=取消上传",
    "button:has-text('取消上传')",
    "text=文件解析中，请稍等",
)

_SUCCESS_KEYWORDS = (
    "发布成功",
    "作品发布成功",
    "投稿成功",
    "发布完成",
    "已发布",
    "审核中",
)
_VERIFY_KEYWORDS = (
    "接收短信验证码",
    "短信验证码",
    "为确保是本人操作",
    "输入验证码",
    "安全验证",
    "完成验证",
    "拖动滑块",
    "使用原设备扫码",
    "身份验证",
)
_PUBLISH_BUTTON_LABELS = frozenset({"发布", "发布作品", "发布视频", "立即发布"})


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


def _dump_debug(page: Any, tag: str) -> str:
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = DEBUG_DIR / f"dy_publish_{tag}_{stamp}"
        png = str(base.with_suffix(".png"))
        try:
            page.screenshot(path=png, full_page=True)
        except Exception:
            png = ""
        try:
            body = page.inner_text("body")
        except Exception:
            body = ""
        inventory = ""
        try:
            inventory = page.evaluate(
                """() => {
                  const inputs = Array.from(document.querySelectorAll('input, textarea')).map((el) => ({
                    tag: el.tagName,
                    type: el.getAttribute('type'),
                    placeholder: el.getAttribute('placeholder'),
                    className: (el.className || '').toString().slice(0, 80),
                    visible: !!(el.offsetWidth || el.offsetHeight),
                  }));
                  const editables = Array.from(document.querySelectorAll('[contenteditable="true"]')).map((el) => ({
                    tag: el.tagName,
                    placeholder: el.getAttribute('placeholder') || el.getAttribute('data-placeholder'),
                    className: (el.className || '').toString().slice(0, 120),
                    text: (el.innerText || '').slice(0, 40),
                    visible: !!(el.offsetWidth || el.offsetHeight),
                  }));
                  return JSON.stringify({ url: location.href, inputs, editables }, null, 2);
                }"""
            )
        except Exception as exc:
            inventory = f"(inventory failed: {exc})"
        base.with_suffix(".txt").write_text(
            f"url: {page.url}\n\n{(body or '')[:4000]}\n\n--- DOM inventory ---\n{inventory}\n",
            encoding="utf-8",
        )
        return png
    except Exception:
        return ""


def _visible_keyword(page: Any, keywords: tuple[str, ...]) -> str:
    for kw in keywords:
        try:
            if page.get_by_text(kw, exact=False).first.is_visible(timeout=200):
                return kw
        except Exception:
            continue
    return ""


def _dismiss_overlays(page: Any) -> None:
    try:
        page.evaluate(
            """() => {
              document.querySelectorAll(
                '.shepherd-element, .shepherd-modal-overlay-container, [class*="mention-wrapper"]'
              ).forEach((el) => el.remove());
            }"""
        )
    except Exception:
        pass
    for label in ("我知道了", "知道了", "关闭", "完成"):
        try:
            btn = page.get_by_role("button", name=label, exact=True).first
            if btn.count() and btn.is_visible(timeout=300):
                btn.click(timeout=1500)
        except Exception:
            continue


def _pick_video_file_input(page: Any) -> Any:
    candidates = [
        page.locator("div[class^='container'] input[type='file']"),
        page.locator("input.upload-btn-input"),
        page.locator("input[type='file']"),
    ]
    for group in candidates:
        count = group.count()
        if count == 0:
            continue
        preferred = -1
        for index in range(count):
            try:
                accept = (group.nth(index).get_attribute("accept") or "").lower()
            except Exception:
                accept = ""
            if "video" in accept or ".mp4" in accept or not accept:
                preferred = index
                if "video" in accept or ".mp4" in accept:
                    break
        return group.nth(preferred if preferred >= 0 else 0)
    raise RuntimeError("未找到上传控件 input[type=file]")


def _upload_still_busy(page: Any) -> bool:
    for sel in _UPLOAD_BUSY_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible(timeout=300):
                return True
        except Exception:
            continue
    return False


def _scroll_form_top(page: Any) -> None:
    try:
        page.evaluate("() => window.scrollTo(0, 0)")
    except Exception:
        pass
    try:
        page.locator("text=基础信息").first.scroll_into_view_if_needed(timeout=1500)
    except Exception:
        pass


def _title_locator(page: Any) -> Any | None:
    for sel in _TITLE_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            try:
                if loc.is_visible(timeout=400):
                    return loc
            except Exception:
                # Some builds keep the node attached with transient visibility flags.
                return loc
        except Exception:
            continue
    return None


def _desc_locator(page: Any) -> Any | None:
    for sel in _DESC_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible(timeout=400):
                return loc
        except Exception:
            continue
    try:
        loc = page.locator("div.zone-container").first
        if loc.count() and loc.is_visible(timeout=400):
            return loc
    except Exception:
        pass
    return None


def _wait_publish_editor(page: Any, timeout_sec: int, report: Callable[[float, str], None] | None = None) -> None:
    deadline = time.time() + timeout_sec
    last_msg = ""
    while time.time() < deadline:
        url = page.url or ""
        on_post = any(marker in url for marker in _POST_URL_MARKERS)
        if not on_post:
            msg = "等待跳转到发布编辑页…"
            if report and msg != last_msg:
                report(40, msg)
                last_msg = msg
            page.wait_for_timeout(1500)
            continue

        if _upload_still_busy(page):
            msg = "视频仍在上传（检测到「取消上传」），等待完成…"
            if report and msg != last_msg:
                report(45, msg)
                last_msg = msg
            page.wait_for_timeout(2000)
            continue

        _scroll_form_top(page)
        _dismiss_overlays(page)

        try:
            page.locator('input[placeholder*="填写作品标题"]').first.wait_for(
                state="visible",
                timeout=4000,
            )
            page.wait_for_timeout(500)
            return
        except Exception:
            pass

        if _title_locator(page) is not None or _desc_locator(page) is not None:
            page.wait_for_timeout(800)
            return

        msg = "上传已完成，等待标题/描述输入框渲染…"
        if report and msg != last_msg:
            report(55, msg)
            last_msg = msg
        page.wait_for_timeout(1500)

    _dump_debug(page, "noeditor")
    raise RuntimeError(
        "上传后未出现标题/描述输入框（视频可能仍在转码，或页面改版）。"
        "请查看弹出窗口，或检查 data/douyin/debug/ 诊断截图。"
    )


def _fill_title_via_react(page: Any, text: str) -> bool:
    try:
        return bool(
            page.evaluate(
                """(value) => {
                  const input = Array.from(document.querySelectorAll('input, textarea')).find((el) => {
                    const ph = (el.getAttribute('placeholder') || '');
                    return ph.includes('填写作品标题') || ph.includes('作品标题') || ph.includes('添加作品标题');
                  });
                  if (!(input instanceof HTMLInputElement || input instanceof HTMLTextAreaElement)) return false;
                  input.focus();
                  const proto = input instanceof HTMLTextAreaElement
                    ? window.HTMLTextAreaElement.prototype
                    : window.HTMLInputElement.prototype;
                  const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                  if (setter) setter.call(input, value);
                  else input.value = value;
                  input.dispatchEvent(new Event('input', { bubbles: true }));
                  input.dispatchEvent(new Event('change', { bubbles: true }));
                  const propKey = Object.keys(input).find((k) => k.startsWith('__reactProps$'));
                  const props = propKey ? input[propKey] : null;
                  if (props?.onChange) {
                    props.onChange({ target: { value }, currentTarget: { value } });
                  }
                  return (input.value || '') === value;
                }""",
                text,
            )
        )
    except Exception:
        return False


def _read_title_value(page: Any) -> str:
    try:
        return str(
            page.evaluate(
                """() => {
                  const input = Array.from(document.querySelectorAll('input, textarea')).find((el) => {
                    const ph = (el.getAttribute('placeholder') || '');
                    return ph.includes('填写作品标题') || ph.includes('作品标题') || ph.includes('添加作品标题');
                  });
                  if (input) return input.value || '';
                  const zone = document.querySelector('div.zone-container[contenteditable="true"], div.zone-container');
                  if (zone) return (zone.innerText || '').split('\\n')[0].trim();
                  return '';
                }"""
            )
            or ""
        )
    except Exception:
        return ""


def _title_looks_filled(page: Any, expected: str) -> bool:
    current = _read_title_value(page).strip()
    if not current:
        return False
    needle = expected.strip()
    if not needle:
        return False
    return needle == current or needle[: min(8, len(needle))] in current


def _type_text(page: Any, text: str) -> None:
    try:
        page.keyboard.insert_text(text)
    except Exception:
        page.keyboard.type(text, delay=20)


def _fill_title(page: Any, title: str) -> str:
    """Fill title. Returns 'title' if dedicated input used, else 'desc'."""
    text = title.strip()[:TITLE_MAX_LEN]
    if not text:
        raise RuntimeError("标题不能为空")

    _scroll_form_top(page)
    _dismiss_overlays(page)

    deadline = time.time() + 120
    while time.time() < deadline:
        if _upload_still_busy(page):
            page.wait_for_timeout(1500)
            continue

        _scroll_form_top(page)
        loc = _title_locator(page)
        if loc is not None:
            try:
                loc.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass

            strategies = []

            def _by_fill() -> None:
                loc.click(timeout=3000)
                loc.fill("")
                loc.fill(text, timeout=5000)

            def _by_react() -> None:
                if not _fill_title_via_react(page, text):
                    raise RuntimeError("react fill failed")

            def _by_insert() -> None:
                loc.click(timeout=2000)
                page.keyboard.press("Control+A")
                page.keyboard.press("Delete")
                _type_text(page, text)

            def _by_type() -> None:
                loc.click(timeout=2000)
                page.keyboard.press("Control+A")
                page.keyboard.type(text, delay=20)

            strategies.extend((_by_fill, _by_react, _by_insert, _by_type))
            for strategy in strategies:
                try:
                    strategy()
                    page.wait_for_timeout(350)
                    if _title_looks_filled(page, text):
                        return "title"
                except Exception:
                    continue

        # Explicit dreammis-style wait for the known placeholder.
        try:
            title_input = page.locator('input[placeholder*="填写作品标题"]').first
            title_input.wait_for(state="visible", timeout=2500)
            title_input.click(timeout=3000)
            title_input.fill(text, timeout=5000)
            page.wait_for_timeout(300)
            if _title_looks_filled(page, text) or _fill_title_via_react(page, text):
                return "title"
        except Exception:
            pass

        page.wait_for_timeout(1000)

    desc = _desc_locator(page)
    if desc is not None:
        try:
            desc.click(timeout=3000)
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
            _type_text(page, text)
            return "desc"
        except Exception:
            pass

    _dump_debug(page, "notitle")
    raise RuntimeError("未能定位抖音标题输入框（上传未完成或创作者中心页面已改版）")


def _fill_desc_and_tags(page: Any, tags: str, *, prepend_title: str | None = None) -> None:
    cleaned = [
        part.strip().lstrip("#")
        for part in tags.replace("，", ",").split(",")
        if part.strip()
    ]
    editor = _desc_locator(page)
    if editor is None:
        return
    try:
        editor.click(timeout=3000)
        if prepend_title:
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
            _type_text(page, prepend_title.strip()[:TITLE_MAX_LEN])
            page.keyboard.press("Enter")
        else:
            page.keyboard.press("End")
        for tag in cleaned[:5]:
            page.keyboard.type(f" #{tag}", delay=20)
            page.keyboard.press("Space")
        page.keyboard.press("Escape")
    except Exception:
        pass


def _try_set_cover_if_required(page: Any) -> None:
    try:
        if not page.get_by_text("请设置封面后再发布").first.is_visible(timeout=500):
            return
    except Exception:
        return
    candidates = [
        page.locator('[class*="cover"] img').first,
        page.locator("text=推荐封面").first,
        page.locator("text=设置封面").first,
    ]
    for loc in candidates:
        try:
            if loc.count() and loc.is_visible(timeout=800):
                loc.click(timeout=2000)
                page.wait_for_timeout(800)
                for label in ("确定", "完成", "使用当前封面", "保存"):
                    try:
                        btn = page.get_by_role("button", name=label, exact=True).first
                        if btn.count() and btn.is_visible(timeout=500):
                            btn.click(timeout=2000)
                            break
                    except Exception:
                        continue
                return
        except Exception:
            continue


def _primary_publish_button(page: Any) -> Any | None:
    try:
        exact = page.get_by_role("button", name="发布", exact=True)
        if exact.count():
            for i in range(exact.count()):
                btn = exact.nth(i)
                try:
                    if btn.is_visible(timeout=300):
                        return btn
                except Exception:
                    continue
    except Exception:
        pass

    try:
        buttons = page.locator("button")
        count = buttons.count()
    except Exception:
        return None
    chosen = None
    for i in range(count):
        btn = buttons.nth(i)
        try:
            if not btn.is_visible(timeout=200):
                continue
            text = re.sub(r"\s+", "", (btn.inner_text() or "").strip())
            if text in _PUBLISH_BUTTON_LABELS:
                chosen = btn
        except Exception:
            continue
    return chosen


def _click_publish(page: Any) -> None:
    _dismiss_overlays(page)
    _try_set_cover_if_required(page)

    btn = None
    for _ in range(45):
        btn = _primary_publish_button(page)
        if btn is not None:
            try:
                if btn.is_enabled(timeout=500):
                    break
            except Exception:
                break
        page.wait_for_timeout(2000)
    if btn is None:
        _dump_debug(page, "nobtn")
        raise RuntimeError("未能定位「发布」按钮（创作者中心页面可能已改版）")

    try:
        btn.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    try:
        btn.click(timeout=5000, force=True)
    except Exception:
        try:
            btn.click(timeout=5000)
        except Exception as exc:
            _dump_debug(page, "clickfail")
            raise RuntimeError(f"点击发布按钮失败: {exc}") from exc

    page.wait_for_timeout(800)
    for label in ("确认发布", "确定"):
        try:
            confirm = page.get_by_role("button", name=label, exact=True).first
            if confirm.count() and confirm.is_visible(timeout=600):
                confirm.click(timeout=3000)
                break
        except Exception:
            continue


def _wait_publish_result(
    page: Any,
    timeout_sec: int,
    report: Callable[[float, str], None],
) -> dict[str, Any]:
    deadline = time.time() + max(60, timeout_sec)
    verify_notified = False
    while time.time() < deadline:
        url = page.url or ""
        if _MANAGE_URL_MARKER in url:
            return {"ok": True, "page_url": url, "message": "已跳转作品管理，发布提交成功"}

        hit = _visible_keyword(page, _SUCCESS_KEYWORDS)
        if hit:
            return {
                "ok": True,
                "page_url": url,
                "message": f"页面出现成功提示「{hit}」",
            }

        vkw = _visible_keyword(page, _VERIFY_KEYWORDS)
        if vkw:
            if not verify_notified:
                verify_notified = True
                _dump_debug(page, "verify")
                report(
                    92,
                    f"抖音要求人工验证「{vkw}」，请在弹出的浏览器窗口完成验证（短信/扫码/滑块）…",
                )
            page.wait_for_timeout(2000)
            continue

        page.wait_for_timeout(1500)

    png = _dump_debug(page, "unconfirmed")
    hint = f"（诊断截图: {png}）" if png else "（见 data/douyin/debug/）"
    if verify_notified:
        raise RuntimeError(
            "发布被抖音风控拦下，需在弹出窗口完成短信/扫码验证，但等待超时。"
            + hint
        )
    raise RuntimeError(
        "已点击发布，但未确认到成功信号。请到创作者中心「作品管理」核对是否已进入审核；"
        f"若未发出请把诊断截图发回校准选择器。{hint}"
    )


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
        browser = playwright.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            storage_state=str(auth.STORAGE_STATE_PATH),
            user_agent=auth.USER_AGENT,
            locale="zh-CN",
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()

        def _cancel_filechooser(fc: Any) -> None:
            try:
                fc.set_files([])
            except Exception:
                pass

        page.on("filechooser", _cancel_filechooser)

        try:
            report(10, "打开创作者上传页…")
            page.goto(auth.CREATOR_UPLOAD, wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_timeout(2500)
            if "passport" in (page.url or "").lower() or "/login" in (page.url or "").lower():
                _dump_debug(page, "loggedout")
                raise RuntimeError("抖音登录态失效，请重新登录")
            if not auth._looks_logged_in(page):
                _dump_debug(page, "loggedout")
                raise RuntimeError("抖音登录态失效，请重新登录")

            report(20, "选择视频文件…")
            file_input = _pick_video_file_input(page)
            file_input.set_input_files(str(video_path))

            report(35, "等待上传并进入编辑页…")
            _wait_publish_editor(page, timeout_sec, report)
            page.wait_for_timeout(800)
            _dismiss_overlays(page)

            report(70, "填写标题与话题…")
            title_target = _fill_title(page, title)
            if title_target == "title" and not _title_looks_filled(page, title):
                _fill_title_via_react(page, title[:TITLE_MAX_LEN])

            tags_value = meta.tags or str(settings.get("default_tags") or "")
            if title_target == "desc":
                _fill_desc_and_tags(page, tags_value, prepend_title=title)
            else:
                _fill_desc_and_tags(page, tags_value)

            if title_target == "title" and not _title_looks_filled(page, title):
                # Last resort: put title into description editor so publish is not empty.
                desc = _desc_locator(page)
                if desc is not None:
                    _fill_desc_and_tags(page, tags_value, prepend_title=title)
                    title_target = "desc"
                else:
                    _dump_debug(page, "title_empty")
                    raise RuntimeError("标题填写后校验为空，请查看 data/douyin/debug/ 诊断")

            page.wait_for_timeout(600)

            report(85, "点击发布…")
            _click_publish(page)

            report(90, "等待发布结果确认…")
            outcome = _wait_publish_result(page, timeout_sec, report)

            try:
                context.storage_state(path=str(auth.STORAGE_STATE_PATH))
            except Exception:
                pass

            report(100, "发布流程已确认")
            return {
                "title": title[:TITLE_MAX_LEN],
                "video_path": str(video_path),
                "page_url": outcome.get("page_url") or page.url,
                "filled_title": _read_title_value(page),
                "message": outcome.get("message")
                or "已在创作者中心提交发布（请在抖音 App/网页确认审核结果）",
            }
        except Exception:
            try:
                _dump_debug(page, "exception")
            except Exception:
                pass
            raise
        finally:
            if not headless:
                page.wait_for_timeout(1500)
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
