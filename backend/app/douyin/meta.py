from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from .auth import load_settings

logger = logging.getLogger(__name__)

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _strip_fences(raw: str) -> str:
    text = raw.strip()
    if "```" in text:
        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE)
        if fenced:
            return fenced.group(1).strip()
    return text


def _parse_json_object(raw: str) -> dict[str, Any]:
    text = _strip_fences(raw)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_BLOCK_RE.search(text)
        if not match:
            raise
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("meta response must be a JSON object")
    return data


def _fallback_meta(*, filename: str, original_title: str | None, default_tags: str) -> dict[str, Any]:
    title = (original_title or "").strip()[:55] or Path(filename).stem[:55] or "配音视频"
    tags = [part.strip() for part in default_tags.replace("，", ",").split(",") if part.strip()][:5]
    return {"title": title, "tags": tags, "tag_str": ",".join(tags)}


def _generate_sync(
    *,
    filename: str,
    subtitle_text: str,
    original_title: str | None = None,
) -> dict[str, Any]:
    from .. import database
    from ..adapters.openai_translate import _client
    from ..bilibili.deepseek_meta import _chat_completion, _message_text

    settings = load_settings()
    default_tags = str(settings.get("default_tags") or "配音,翻译")
    openai_settings = database.get_openai_settings()
    api_key = str(openai_settings.get("api_key") or "").strip()
    if not api_key:
        return _fallback_meta(filename=filename, original_title=original_title, default_tags=default_tags)

    base_url = str(openai_settings.get("base_url") or "").strip()
    model = str(openai_settings.get("model") or "").strip()
    if not model:
        return _fallback_meta(filename=filename, original_title=original_title, default_tags=default_tags)

    clipped = subtitle_text.strip()
    if len(clipped) > 4000:
        clipped = clipped[:4000] + "\n…"

    system = (
        "你是抖音短视频运营助手。只输出一个 JSON 对象，不要 markdown。"
        '字段：title(string, <=55字), tags(string[], 3-5个话题词, 不要#)。'
    )
    user = (
        f"原标题：{original_title or ''}\n"
        f"文件名：{filename}\n"
        f"字幕：\n{clipped}\n\n"
        '输出 JSON：{"title":"...","tags":["..."]}'
    )
    client = _client(base_url, api_key)
    try:
        response = _chat_completion(
            client,
            model=model,
            system=system,
            user=user,
            use_json_format=True,
        )
        raw = _message_text(response)
        data = _parse_json_object(raw)
        title = str(data.get("title") or "").strip()[:55]
        tags_raw = data.get("tags") or []
        if isinstance(tags_raw, str):
            tags = [part.strip().lstrip("#") for part in tags_raw.replace("，", ",").split(",") if part.strip()]
        elif isinstance(tags_raw, list):
            tags = [str(part).strip().lstrip("#") for part in tags_raw if str(part).strip()]
        else:
            tags = []
        if not title:
            raise ValueError("empty title")
        if not tags:
            tags = [part.strip() for part in default_tags.replace("，", ",").split(",") if part.strip()]
        return {"title": title, "tags": tags[:5], "tag_str": ",".join(tags[:5])}
    except Exception:
        logger.exception("Douyin meta generation failed; using fallback")
        return _fallback_meta(filename=filename, original_title=original_title, default_tags=default_tags)


async def generate_douyin_meta(
    *,
    filename: str,
    subtitle_text: str,
    source_url: str = "",
    original_title: str | None = None,
) -> dict[str, Any]:
    del source_url
    import asyncio

    return await asyncio.to_thread(
        _generate_sync,
        filename=filename,
        subtitle_text=subtitle_text,
        original_title=original_title,
    )
