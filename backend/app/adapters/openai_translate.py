from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError
from pydantic import BaseModel, Field, ValidationError

from ..sources import SourceConfig
from ._translate_prompts import PREPROCESS_PROMPT, TRANSLATE_RULES
from .openai_client import normalize_openai_base_url

log = logging.getLogger(__name__)

API_SETTING_KEYS = ("base_url", "api_key", "model")
PREPROCESS_RETRY = 2
TRANSLATE_RETRY = 3
DESCRIPTION_LIMIT = 500
DEFAULT_CONCURRENCY = 8
MAX_EFFECTIVE_CONCURRENCY = 16
REQUEST_TIMEOUT_SECONDS = 60.0


class HotwordItem(BaseModel):
    src: str
    dst: str


class CorrectionItem(BaseModel):
    wrong: str
    correct: str


class PreprocessResponse(BaseModel):
    summary: str = ""
    hotwords: list[HotwordItem] = Field(default_factory=list)
    corrections: list[CorrectionItem] = Field(default_factory=list)


class TranslationItem(BaseModel):
    dst: str


def list_models(*, base_url: str, api_key: str) -> list[str]:
    if not api_key:
        raise ValueError("OpenAI API key is not configured.")
    client = _client(base_url, api_key)
    response = client.models.list()
    seen: set[str] = set()
    models: list[str] = []
    for item in response.data:
        model_id = getattr(item, "id", "")
        if model_id and model_id not in seen:
            seen.add(model_id)
            models.append(model_id)
    return models


def _client(base_url: str, api_key: str) -> OpenAI:
    if not api_key:
        raise ValueError("OpenAI API key is not configured.")
    return OpenAI(
        api_key=api_key,
        base_url=normalize_openai_base_url(base_url),
        timeout=REQUEST_TIMEOUT_SECONDS,
        max_retries=0,
    )


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)
_DST_STRING_RE = re.compile(r'"dst"\s*:\s*("(?:\\.|[^"\\])*")', re.DOTALL)


def _strip_json_fences(raw: str) -> str:
    text = raw.strip()
    if not text.startswith("```"):
        return text
    text = re.sub(r"^```(?:json)?\s*", "", text, count=1, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_json(raw: str) -> dict[str, Any]:
    text = _strip_json_fences(raw)
    candidates: list[str] = [text]
    match = _JSON_BLOCK_RE.search(text)
    if match:
        candidates.append(match.group(0))
    # Models occasionally truncate the closing brace: '{"dst": "两个，"'
    if text.startswith("{") and "}" not in text:
        candidates.append(text + "}")

    seen: set[str] = set()
    last_error: json.JSONDecodeError | None = None
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

    dst_match = _DST_STRING_RE.search(text)
    if dst_match:
        try:
            return {"dst": json.loads(dst_match.group(1))}
        except json.JSONDecodeError as exc:
            last_error = exc

    detail = ""
    if last_error is not None:
        detail = f"{last_error.msg}; "
    raise json.JSONDecodeError(
        f"no JSON object found; {detail}len={len(raw)}; raw[:300]={raw[:300]!r}; raw[-200:]={raw[-200:]!r}",
        raw,
        0,
    )


def _call_json(client: OpenAI, model: str, system: str, user: str) -> dict[str, Any]:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    raw = response.choices[0].message.content or "{}"
    return _extract_json(raw)


def _format_terms(items: list, fmt: str, empty: str) -> str:
    if not items:
        return empty
    return "\n".join(fmt.format(**item.model_dump()) for item in items)


def _meta_view(meta: dict[str, Any]) -> dict[str, str]:
    description = (meta.get("description") or "").strip()
    if len(description) > DESCRIPTION_LIMIT:
        description = description[:DESCRIPTION_LIMIT] + "..."
    return {
        "title": str(meta.get("title") or "").strip() or "(unknown)",
        "uploader": str(meta.get("uploader") or "").strip() or "(unknown)",
        "description": description or "(none)",
    }


def _is_retryable_translate_error(exc: Exception) -> bool:
    if isinstance(exc, (json.JSONDecodeError, ValidationError, ValueError)):
        return True
    if isinstance(exc, (APITimeoutError, APIConnectionError, RateLimitError, TimeoutError, OSError)):
        return True
    if isinstance(exc, APIStatusError) and exc.status_code in {408, 429, 500, 502, 503, 504}:
        return True
    return False


def preprocess(
    full_text: str,
    meta: dict[str, Any],
    source: SourceConfig,
    *,
    base_url: str,
    api_key: str,
    model: str,
) -> PreprocessResponse:
    user = PREPROCESS_PROMPT.format(
        src_language_name=source.asr_language_name,
        dst_language_name=source.target_language_name,
        full_text=full_text,
        **_meta_view(meta),
    )
    client = _client(base_url, api_key)
    last_error: Exception | None = None
    for attempt in range(PREPROCESS_RETRY + 1):
        try:
            data = _call_json(client, model, "You output strict JSON only.", user)
            return PreprocessResponse.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = exc
            log.warning("preprocess attempt %d failed: %s", attempt + 1, exc)
        except (APITimeoutError, APIConnectionError, RateLimitError, APIStatusError, TimeoutError, OSError) as exc:
            last_error = exc
            log.warning("preprocess attempt %d transport failed: %s", attempt + 1, exc)
            time.sleep(min(2**attempt, 8))
    log.error("preprocess gave up, returning empty: %s", last_error)
    return PreprocessResponse()


def _translate_system(source: SourceConfig, meta: dict[str, Any], pre: PreprocessResponse) -> str:
    rules = TRANSLATE_RULES[source.target_language]
    return rules.format(
        summary=pre.summary or "(none)",
        hotwords=_format_terms(pre.hotwords, "{src} -> {dst}", "(none)"),
        corrections=_format_terms(pre.corrections, "{wrong} -> {correct}", "(none)"),
        **_meta_view(meta),
    )


def _post_process(text: str, target_language: str) -> str:
    cleaned = text.strip()
    if target_language == "zh":
        cleaned = cleaned.replace("——", "，")
    return cleaned


def translate_sentence(
    text: str,
    target_language: str,
    client: OpenAI,
    model: str,
    system: str,
) -> str:
    last_error: Exception | None = None
    for attempt in range(TRANSLATE_RETRY):
        try:
            data = _call_json(client, model, system, text)
            item = TranslationItem.model_validate(data)
            if not item.dst.strip():
                raise ValueError("empty dst")
            return _post_process(item.dst, target_language)
        except Exception as exc:
            last_error = exc
            if not _is_retryable_translate_error(exc) or attempt + 1 >= TRANSLATE_RETRY:
                break
            log.warning("translate attempt %d failed for %r: %s", attempt + 1, text[:60], exc)
            time.sleep(min(2**attempt, 8))
    raise RuntimeError(f"translate_sentence failed after {TRANSLATE_RETRY} attempts: {last_error}")


def translate_batch(
    texts: list[str],
    source: SourceConfig,
    meta: dict[str, Any],
    pre: PreprocessResponse,
    *,
    base_url: str,
    api_key: str,
    model: str,
    concurrency: int = DEFAULT_CONCURRENCY,
    progress_callback: Callable[[int, str], None] | None = None,
) -> list[str]:
    if not texts:
        return []
    workers = max(1, min(int(concurrency), MAX_EFFECTIVE_CONCURRENCY))
    if workers != concurrency:
        log.info(
            "translate_batch: clamping concurrency %s -> %s",
            concurrency,
            workers,
        )
    system = _translate_system(source, meta, pre)
    client = _client(base_url, api_key)
    total = len(texts)
    log.info("translate_batch: %d sentences, concurrency=%d", total, workers)
    if progress_callback:
        progress_callback(0, f"Translating 0/{total} sentences")

    results: list[str | None] = [None] * total
    done = 0
    lock = threading.Lock()

    def work(index: int, text: str) -> tuple[int, str]:
        return index, translate_sentence(text, source.target_language, client, model, system)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, index, text) for index, text in enumerate(texts)]
        for future in as_completed(futures):
            index, dst = future.result()
            results[index] = dst
            with lock:
                done += 1
                current = done
            if progress_callback:
                progress = round(current / total * 100)
                progress_callback(progress, f"Translated {current}/{total} sentences")

    return [item if item is not None else "" for item in results]


def _read_meta(session: Path) -> dict[str, Any]:
    info_file = session / "metadata" / "ytdlp_info.json"
    if not info_file.exists():
        return {}
    return json.loads(info_file.read_text(encoding="utf-8"))


def _speaker(utt: dict[str, Any]) -> str:
    additions = utt.get("additions") or {}
    if isinstance(additions, dict):
        return str(additions.get("speaker") or "1")
    return "1"


def _full_text(data: dict[str, Any], texts: list[str]) -> str:
    raw = data.get("result", {}).get("text") or ""
    if raw.strip():
        return raw
    return " ".join(texts)


def preprocess_artifact_path(session: Path) -> Path:
    return session / "metadata" / "translation_preprocess.json"


def write_preprocess_artifact(session: Path, pre: PreprocessResponse) -> Path:
    path = preprocess_artifact_path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pre.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_preprocess_artifact(session: Path) -> PreprocessResponse | None:
    path = preprocess_artifact_path(session)
    if not path.exists():
        return None
    return PreprocessResponse.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _concurrency_from(settings: dict[str, str]) -> int:
    raw = str(settings.get("translate_concurrency") or "").strip()
    if not raw or not all("0" <= char <= "9" for char in raw):
        return DEFAULT_CONCURRENCY
    concurrency = int(raw)
    if concurrency < 1 or concurrency > 200:
        return DEFAULT_CONCURRENCY
    return min(concurrency, MAX_EFFECTIVE_CONCURRENCY)


def translate_asr(
    asr_file: Path,
    session: Path,
    settings: dict[str, str],
    source: SourceConfig,
    progress_callback: Callable[[int, str], None] | None = None,
) -> Path:
    output_file = session / "metadata" / f"translation.{source.target_language}.json"
    if output_file.exists():
        return output_file

    data = json.loads(asr_file.read_text(encoding="utf-8"))
    utterances = data["result"]["utterances"]
    texts = [u["text"].strip() for u in utterances]
    full_text = _full_text(data, texts)
    meta = _read_meta(session)

    api = {key: settings[key] for key in API_SETTING_KEYS if key in settings}
    pre = load_preprocess_artifact(session)
    if pre is None:
        if progress_callback:
            progress_callback(0, "Preprocessing translation context")
        pre = preprocess(full_text, meta, source, **api)
        write_preprocess_artifact(session, pre)
        log.info("Wrote translation preprocess artifact to %s", preprocess_artifact_path(session))
    else:
        log.info("Reusing translation preprocess artifact from %s", preprocess_artifact_path(session))
    dst_list = translate_batch(
        texts,
        source,
        meta,
        pre,
        **api,
        concurrency=_concurrency_from(settings),
        progress_callback=progress_callback,
    )

    translation = [
        {
            "src": text,
            "dst": dst,
            "src_lang": source.asr_language,
            "dst_lang": source.target_language,
            "start_time": utt["start_time"],
            "end_time": utt["end_time"],
            "speaker": _speaker(utt),
        }
        for text, dst, utt in zip(texts, dst_list, utterances)
    ]
    output_file.write_text(
        json.dumps({"translation": translation}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if progress_callback:
        progress_callback(100, f"Translated {len(translation)}/{len(translation)} sentences")
    return output_file
