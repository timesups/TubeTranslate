from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "bilibili"
COOKIE_PATH = DATA_DIR / "cookies.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_cookies() -> dict[str, str]:
    ensure_data_dir()
    if not COOKIE_PATH.exists():
        return {}
    try:
        data = json.loads(COOKIE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if isinstance(data, dict) and "cookies" in data:
        return {str(k): str(v) for k, v in data["cookies"].items()}
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items()}
    return {}


def save_cookies(cookies: dict[str, str], extra: dict[str, Any] | None = None) -> None:
    ensure_data_dir()
    payload: dict[str, Any] = {"cookies": cookies}
    if extra:
        payload.update(extra)
    COOKIE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_cookies() -> None:
    if COOKIE_PATH.exists():
        COOKIE_PATH.unlink()


def parse_cookie_string(raw: str) -> dict[str, str]:
    """从浏览器 Cookie 字符串或 JSON 中解析关键字段。"""
    raw = raw.strip()
    if not raw:
        return {}

    if raw.startswith("{"):
        data = json.loads(raw)
        if "cookies" in data and isinstance(data["cookies"], dict):
            data = data["cookies"]
        return {str(k): str(v) for k, v in data.items()}

    cookies: dict[str, str] = {}
    for part in re.split(r";\s*", raw):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        cookies[key.strip()] = value.strip()
    return cookies


def cookies_from_set_cookie(headers: httpx.Headers) -> dict[str, str]:
    result: dict[str, str] = {}
    for header in headers.get_list("set-cookie"):
        first = header.split(";", 1)[0]
        if "=" in first:
            key, value = first.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def cookies_from_login_url(url: str) -> dict[str, str]:
    """部分成功响应会把 cookie 放在回调 url 的 query 里。"""
    query = parse_qs(urlparse(url).query)
    mapping = {
        "DedeUserID": "DedeUserID",
        "DedeUserID__ckMd5": "DedeUserID__ckMd5",
        "SESSDATA": "SESSDATA",
        "bili_jct": "bili_jct",
    }
    out: dict[str, str] = {}
    for src, dst in mapping.items():
        values = query.get(src)
        if values:
            out[dst] = values[0]
    return out


def build_client(cookies: dict[str, str] | None = None) -> httpx.AsyncClient:
    cookies = cookies or load_cookies()
    return httpx.AsyncClient(
        cookies=cookies,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": "https://member.bilibili.com/",
            "Origin": "https://member.bilibili.com",
        },
        timeout=60.0,
        follow_redirects=True,
    )


async def fetch_nav(cookies: dict[str, str] | None = None) -> dict[str, Any]:
    async with build_client(cookies) as client:
        resp = await client.get("https://api.bilibili.com/x/web-interface/nav")
        resp.raise_for_status()
        return resp.json()


async def get_login_status() -> dict[str, Any]:
    cookies = load_cookies()
    if not cookies.get("SESSDATA"):
        return {"logged_in": False, "message": "未登录"}

    data = await fetch_nav(cookies)
    if data.get("code") != 0 or not data.get("data", {}).get("isLogin"):
        return {"logged_in": False, "message": "登录已失效，请重新登录"}

    info = data["data"]
    return {
        "logged_in": True,
        "uname": info.get("uname"),
        "mid": info.get("mid"),
        "face": info.get("face"),
        "level": info.get("level_info", {}).get("current_level"),
    }


async def save_cookie_login(raw: str) -> dict[str, Any]:
    cookies = parse_cookie_string(raw)
    required = ["SESSDATA", "bili_jct"]
    missing = [k for k in required if not cookies.get(k)]
    if missing:
        raise ValueError(f"缺少必要 Cookie：{', '.join(missing)}")

    status_payload = await fetch_nav(cookies)
    if status_payload.get("code") != 0 or not status_payload.get("data", {}).get("isLogin"):
        raise ValueError("Cookie 无效或已过期")

    save_cookies(cookies)
    info = status_payload["data"]
    return {
        "logged_in": True,
        "uname": info.get("uname"),
        "mid": info.get("mid"),
        "face": info.get("face"),
    }


async def create_qrcode() -> dict[str, str]:
    async with build_client({}) as client:
        resp = await client.get(
            "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("code") != 0:
            raise RuntimeError(payload.get("message") or "获取二维码失败")
        data = payload["data"]
        return {"url": data["url"], "qrcode_key": data["qrcode_key"]}


async def poll_qrcode(qrcode_key: str) -> dict[str, Any]:
    async with build_client({}) as client:
        resp = await client.get(
            "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
            params={"qrcode_key": qrcode_key},
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("code") != 0:
            raise RuntimeError(payload.get("message") or "轮询二维码失败")

        data = payload["data"]
        status_code = data.get("code")
        result: dict[str, Any] = {
            "code": status_code,
            "message": data.get("message") or "",
        }

        if status_code == 0:
            cookies = cookies_from_set_cookie(resp.headers)
            if not cookies.get("SESSDATA"):
                cookies.update(cookies_from_login_url(data.get("url") or ""))
            # 合并 client 已有 cookie
            cookies.update({k: v for k, v in client.cookies.items()})
            if not cookies.get("SESSDATA") or not cookies.get("bili_jct"):
                raise RuntimeError("扫码成功但未拿到 Cookie，请改用 Cookie 登录")
            save_cookies(
                cookies,
                extra={"refresh_token": data.get("refresh_token")},
            )
            nav = await fetch_nav(cookies)
            info = nav.get("data") or {}
            result.update(
                {
                    "logged_in": True,
                    "uname": info.get("uname"),
                    "mid": info.get("mid"),
                    "face": info.get("face"),
                }
            )
        return result
