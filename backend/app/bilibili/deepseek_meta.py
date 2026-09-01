from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

from .auth import DATA_DIR

logger = logging.getLogger(__name__)

SETTINGS_PATH = DATA_DIR / "settings.json"
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def load_settings() -> dict[str, Any]:
    from ..config import REPO_ROOT

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    settings: dict[str, Any] = {
        "default_tid": 229,
        "default_tag": "配音,翻译,教程",
        "default_copyright": 1,
        "video_dir": str((REPO_ROOT / "data" / "bilibili" / "staging").resolve()),
    }
    if SETTINGS_PATH.exists():
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for key, value in data.items():
                    if value is None:
                        continue
                    # Ignore legacy DeepSeek-only fields; meta generation uses OpenAI settings.
                    if key in {"deepseek_api_key", "deepseek_model"}:
                        continue
                    settings[key] = value
        except (json.JSONDecodeError, OSError):
            pass
    return settings


def save_settings(patch: dict[str, Any]) -> dict[str, Any]:
    settings = load_settings()
    for key, value in patch.items():
        if value is None:
            continue
        if key in {"deepseek_api_key", "deepseek_model"}:
            continue
        settings[key] = value
    SETTINGS_PATH.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    return settings


def settings_public(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    data = dict(settings or load_settings())
    data.pop("deepseek_api_key", None)
    data.pop("deepseek_model", None)
    return data


def _strip_json_fences(raw: str) -> str:
    text = raw.strip()
    if "```" in text:
        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE)
        if fenced:
            return fenced.group(1).strip()
        text = _FENCE_RE.sub("", text).strip()
    return text


def _repair_json_text(text: str) -> str:
    """Best-effort fixes for common LLM JSON issues."""
    repaired = text.strip()
    # Remove trailing commas before } or ]
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    # Replace smart quotes
    repaired = repaired.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    return repaired


def _unescape_newlines_in_strings(text: str) -> str:
    """Escape raw newlines/tabs inside JSON string literals."""
    out: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                out.append(ch)
                escaped = False
                continue
            if ch == "\\":
                out.append(ch)
                escaped = True
                continue
            if ch == '"':
                out.append(ch)
                in_string = False
                continue
            if ch == "\n":
                out.append("\\n")
                continue
            if ch == "\r":
                continue
            if ch == "\t":
                out.append("\\t")
                continue
            out.append(ch)
            continue
        if ch == '"':
            in_string = True
        out.append(ch)
    return "".join(out)


def _extract_json(raw: str) -> dict[str, Any]:
    text = _strip_json_fences(raw or "")
    if not text:
        raise RuntimeError("模型返回内容为空，无法解析 JSON")

    candidates: list[str] = [text, _repair_json_text(text)]
    match = _JSON_BLOCK_RE.search(text)
    if match:
        block = match.group(0)
        candidates.extend([block, _repair_json_text(block), _unescape_newlines_in_strings(block)])
    candidates.append(_unescape_newlines_in_strings(_repair_json_text(text)))

    # Truncated object: add closing braces
    if text.lstrip().startswith("{") and text.count("{") > text.count("}"):
        candidates.append(text + ("}" * (text.count("{") - text.count("}"))))

    seen: set[str] = set()
    last_error: Exception | None = None
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(data, dict):
            return data

    preview = (raw or "")[:300].replace("\n", "\\n")
    detail = f"{last_error}" if last_error else "unknown"
    raise RuntimeError(f"模型返回内容不是合法 JSON（{detail}）；preview={preview!r}")


def _normalize_meta(data: dict[str, Any], *, filename: str) -> dict[str, Any]:
    title = str(data.get("title") or "").strip()[:80]
    desc = str(data.get("desc") or "").strip()[:2000]
    dynamic = str(data.get("dynamic") or "").strip()[:100]

    tags_raw = data.get("tag") or data.get("tags") or []
    if isinstance(tags_raw, str):
        tags = [t.strip() for t in re.split(r"[,，、\s]+", tags_raw) if t.strip()]
    elif isinstance(tags_raw, list):
        tags = [str(t).strip().lstrip("#") for t in tags_raw if str(t).strip()]
    else:
        tags = []
    tags = tags[:10]
    if not title:
        title = Path(filename).stem[:80]
    if not desc:
        raise RuntimeError("模型未生成有效简介")
    if not tags:
        tags = ["配音", "翻译", "教程"]

    return {
        "title": title,
        "desc": desc,
        "tag": tags,
        "tag_str": ",".join(tags),
        "dynamic": dynamic,
        "raw": data,
    }


def append_original_video_link(
    desc: str,
    source_url: str | None,
    *,
    max_len: int = 2000,
) -> str:
    """Append a YouTube original-video line to the generated description."""
    from ..youtube import is_youtube_url, validate_video_url

    text = (desc or "").strip()
    url = (source_url or "").strip()
    if not text or not url or not is_youtube_url(url):
        return text[:max_len]

    link = validate_video_url(url).url
    marker = f"原视频：{link}"
    if link in text or "原视频：" in text:
        return text[:max_len]

    overhead = 2 + len(marker)  # "\n\n" + marker
    body = text
    if len(body) + overhead > max_len:
        body = body[: max(0, max_len - overhead)].rstrip()
    if not body:
        return marker[:max_len]
    return f"{body}\n\n{marker}"


def _fallback_meta(
    *,
    filename: str,
    subtitle_text: str,
    original_title: str | None = None,
) -> dict[str, Any]:
    stem = (original_title or "").strip()[:80] or Path(filename).stem[:80] or "配音视频"
    lines = [line.strip() for line in subtitle_text.splitlines() if line.strip()]
    summary = " ".join(lines[:12]).strip()
    if len(summary) > 600:
        summary = summary[:600] + "…"
    desc = (
        f"本视频为《{stem}》的中文配音版本。\n\n"
        f"内容概要：{summary or '详见视频与字幕。'}\n\n"
        "适合对该主题感兴趣的观众学习参考。"
    )
    return _normalize_meta(
        {
            "title": stem,
            "desc": desc,
            "tag": ["配音", "翻译", "教程", "学习"],
            "dynamic": f"分享《{stem}》中文配音版",
        },
        filename=filename,
    )


def _build_prompts(
    *,
    filename: str,
    subtitle_text: str,
    original_title: str | None = None,
) -> tuple[str, str, str]:
    source_title = (original_title or "").strip()
    title_block = f"原视频标题：{source_title}\n\n" if source_title else ""
    title_rule = (
        "title: 中文标题，不超过80字；优先参考原视频标题的主题、关键信息和表达方式，"
        "结合字幕内容改写成适合 B 站的中文标题，不要机械直译，也不要偏离原标题主题；"
        if source_title
        else "title: 中文标题，不超过80字；"
    )
    context_hint = "原视频标题和字幕内容" if source_title else "字幕内容"
    system_prompt = (
        f"你是 B 站视频投稿助手。根据{context_hint}生成符合哔哩哔哩投稿规范的元数据。"
        "必须只输出一个 JSON 对象，不要 markdown 代码块，不要额外解释。"
        "字段："
        'title(string), desc(string), tag(string[]), dynamic(string)。'
        f"{title_rule}"
        "desc: 中文简介 200-800 字，可用 \\n 表示换行；"
        "tag: 3-10 个标签，不要带 #；"
        "dynamic: 不超过50字。"
    )
    user_prompt = (
        f"{title_block}"
        f"视频文件名：{filename}\n\n"
        f"字幕内容：\n{subtitle_text}\n\n"
        '只输出 JSON：{"title":"...","desc":"...","tag":["..."],"dynamic":"..."}'
    )
    retry_prompt = (
        "上一次输出无法解析为 JSON。请重新输出，且只能是合法 JSON 对象，"
        "不要包含思考过程、Markdown 或其它文字。"
        f"\n\n{title_block}视频文件名：{filename}\n字幕：\n{subtitle_text[:2000]}"
    )
    return system_prompt, user_prompt, retry_prompt


def _message_text(response: Any) -> str:
    try:
        message = response.choices[0].message
    except (IndexError, AttributeError, TypeError):
        return ""
    content = getattr(message, "content", None)
    if isinstance(content, str) and content.strip():
        return content
    # Some SDKs return multipart content blocks.
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            else:
                text = getattr(item, "text", None)
                if text:
                    parts.append(str(text))
        joined = "\n".join(part for part in parts if part).strip()
        if joined:
            return joined
    # DeepSeek thinking models may put usable text in reasoning_content.
    reasoning = getattr(message, "reasoning_content", None)
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning
    return content if isinstance(content, str) else ""


def _chat_completion(client: Any, *, model: str, system: str, user: str, use_json_format: bool) -> Any:
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
        # deepseek-v4 thinks by default; JSON mode then often returns empty/"{}".
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    if use_json_format:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        return client.chat.completions.create(**kwargs)
    except Exception:
        kwargs.pop("extra_body", None)
        return client.chat.completions.create(**kwargs)


def _generate_sync(
    *,
    filename: str,
    subtitle_text: str,
    original_title: str | None = None,
) -> dict[str, Any]:
    from .. import database
    from ..adapters.openai_translate import _client

    openai_settings = database.get_openai_settings()
    api_key = str(openai_settings.get("api_key") or "").strip()
    if not api_key:
        raise RuntimeError("未配置翻译 API Key，请先在设置中填写 OpenAI 兼容接口")

    base_url = str(openai_settings.get("base_url") or "").strip()
    model = str(openai_settings.get("model") or "").strip()
    if not model:
        raise RuntimeError("未配置翻译模型，请先在设置中选择模型")

    clipped = subtitle_text.strip()
    if len(clipped) > 6000:
        clipped = clipped[:6000] + "\n…（字幕已截断）"

    system_prompt, user_prompt, retry_prompt = _build_prompts(
        filename=filename,
        subtitle_text=clipped,
        original_title=original_title,
    )

    client = _client(base_url, api_key)
    last_error: Exception | None = None
    use_json_format = True

    for attempt in range(3):
        system = system_prompt if attempt == 0 else (
            "只输出合法 JSON 对象，不要其它内容。"
        )
        user = user_prompt if attempt == 0 else retry_prompt
        try:
            try:
                response = _chat_completion(
                    client,
                    model=model,
                    system=system,
                    user=user,
                    use_json_format=use_json_format,
                )
            except Exception as format_exc:  # noqa: BLE001
                # Some providers reject response_format; retry without it once.
                if use_json_format:
                    logger.info("response_format unsupported, retrying without it: %s", format_exc)
                    use_json_format = False
                    response = _chat_completion(
                        client,
                        model=model,
                        system=system,
                        user=user,
                        use_json_format=False,
                    )
                else:
                    raise
        except Exception as exc:  # noqa: BLE001
            last_error = RuntimeError(f"翻译 API 调用失败：{exc}")
            continue

        content = _message_text(response)
        try:
            data = _extract_json(content)
            return _normalize_meta(data, filename=filename)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.warning(
                "bilibili meta parse failed attempt=%s preview=%r",
                attempt + 1,
                (content or "")[:200],
            )
            continue

    logger.error("bilibili meta generation fell back after parse failures: %s", last_error)
    return _fallback_meta(
        filename=filename,
        subtitle_text=clipped,
        original_title=original_title,
    )


async def generate_bilibili_meta(
    *,
    filename: str,
    subtitle_text: str,
    source_url: str | None = None,
    original_title: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    # api_key / model kept for call-site compatibility; OpenAI settings are authoritative.
    del api_key, model
    meta = await asyncio.to_thread(
        _generate_sync,
        filename=filename,
        subtitle_text=subtitle_text,
        original_title=original_title,
    )
    if source_url:
        meta = dict(meta)
        meta["desc"] = append_original_video_link(meta["desc"], source_url)
    return meta
