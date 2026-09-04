from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "douyin"
STORAGE_STATE_PATH = DATA_DIR / "storage_state.json"
SETTINGS_PATH = DATA_DIR / "settings.json"
USER_DATA_DIR = DATA_DIR / "browser_profile"

CREATOR_HOME = "https://creator.douyin.com/"
CREATOR_UPLOAD = "https://creator.douyin.com/creator-micro/content/upload"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

_login_lock = threading.Lock()
_login_session: dict[str, Any] = {
    "active": False,
    "started_at": 0.0,
    "message": "",
    "logged_in": False,
    "error": None,
}


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def has_storage_state() -> bool:
    return STORAGE_STATE_PATH.exists() and STORAGE_STATE_PATH.stat().st_size > 20


def clear_storage_state() -> None:
    if STORAGE_STATE_PATH.exists():
        STORAGE_STATE_PATH.unlink()


def load_settings() -> dict[str, Any]:
    ensure_data_dir()
    settings: dict[str, Any] = {
        "default_tags": "配音,翻译",
        "headless_publish": False,
        "publish_timeout_sec": 600,
    }
    if SETTINGS_PATH.exists():
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                settings.update({k: v for k, v in data.items() if v is not None})
        except (json.JSONDecodeError, OSError):
            pass
    return settings


def save_settings(patch: dict[str, Any]) -> dict[str, Any]:
    settings = load_settings()
    for key, value in patch.items():
        if value is None:
            continue
        settings[key] = value
    SETTINGS_PATH.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    return settings


def settings_public(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    return dict(settings or load_settings())


def login_session_status() -> dict[str, Any]:
    return {
        "active": bool(_login_session["active"]),
        "message": str(_login_session.get("message") or ""),
        "logged_in": bool(_login_session.get("logged_in")),
        "error": _login_session.get("error"),
        "has_storage_state": has_storage_state(),
    }


def _set_login_session(**kwargs: Any) -> None:
    _login_session.update(kwargs)


def _looks_logged_in(page: Any) -> bool:
    url = (page.url or "").lower()
    if "login" in url or "passport" in url:
        return False
    try:
        cookies = page.context.cookies()
    except Exception:
        cookies = []
    names = {str(cookie.get("name") or "") for cookie in cookies}
    if {"sessionid", "sessionid_ss", "sid_tt", "uid_tt"} & names:
        # Still on a creator page is a stronger signal.
        if "creator.douyin.com" in url and "login" not in url:
            return True
    # Fallback: upload entry visible.
    try:
        if page.locator("text=发布视频").first.is_visible(timeout=500):
            return True
    except Exception:
        pass
    try:
        if page.locator("text=高清发布").first.is_visible(timeout=500):
            return True
    except Exception:
        pass
    return False


def get_login_status(*, headed_probe: bool = False) -> dict[str, Any]:
    """Check whether a saved Playwright storage state looks logged in."""
    if not has_storage_state():
        return {"logged_in": False, "uname": "", "has_storage_state": False}

    if not headed_probe:
        # Cheap check: storage state exists and contains session cookies.
        try:
            data = json.loads(STORAGE_STATE_PATH.read_text(encoding="utf-8"))
            cookies = data.get("cookies") if isinstance(data, dict) else None
            names = {
                str(cookie.get("name") or "")
                for cookie in (cookies or [])
                if isinstance(cookie, dict)
            }
            logged_in = bool({"sessionid", "sessionid_ss", "sid_tt", "uid_tt"} & names)
            return {
                "logged_in": logged_in,
                "uname": "",
                "has_storage_state": True,
            }
        except (json.JSONDecodeError, OSError):
            return {"logged_in": False, "uname": "", "has_storage_state": False}

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            storage_state=str(STORAGE_STATE_PATH),
            user_agent=USER_AGENT,
            locale="zh-CN",
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()
        try:
            page.goto(CREATOR_HOME, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2000)
            logged_in = _looks_logged_in(page)
        except Exception:
            logged_in = False
        finally:
            context.close()
            browser.close()
    return {"logged_in": logged_in, "uname": "", "has_storage_state": True}


def start_interactive_login(*, timeout_sec: int = 300) -> dict[str, Any]:
    """Open a headed Chromium window for QR login; save storage_state on success."""
    if not _login_lock.acquire(blocking=False):
        return login_session_status()

    def worker() -> None:
        try:
            _set_login_session(
                active=True,
                started_at=time.time(),
                message="正在打开抖音创作者中心，请在弹出的浏览器中扫码登录…",
                logged_in=False,
                error=None,
            )
            from playwright.sync_api import sync_playwright

            ensure_data_dir()
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=False)
                context = browser.new_context(
                    user_agent=USER_AGENT,
                    locale="zh-CN",
                    viewport={"width": 1440, "height": 900},
                )
                page = context.new_page()
                page.goto(CREATOR_HOME, wait_until="domcontentloaded", timeout=90_000)
                deadline = time.time() + max(60, timeout_sec)
                while time.time() < deadline:
                    if _looks_logged_in(page):
                        context.storage_state(path=str(STORAGE_STATE_PATH))
                        _set_login_session(
                            active=False,
                            logged_in=True,
                            message="抖音登录成功，已保存登录态",
                            error=None,
                        )
                        context.close()
                        browser.close()
                        return
                    _set_login_session(
                        message="等待扫码登录…请在浏览器窗口完成登录",
                    )
                    page.wait_for_timeout(2000)
                _set_login_session(
                    active=False,
                    logged_in=False,
                    message="登录超时，请重试",
                    error="login_timeout",
                )
                context.close()
                browser.close()
        except Exception as exc:
            _set_login_session(
                active=False,
                logged_in=False,
                message=f"登录失败: {exc}",
                error=str(exc),
            )
        finally:
            _login_lock.release()

    threading.Thread(target=worker, daemon=True).start()
    return login_session_status()


def logout() -> None:
    clear_storage_state()
    _set_login_session(
        active=False,
        logged_in=False,
        message="已退出抖音登录",
        error=None,
    )
