from __future__ import annotations

import io
import json
import logging
import re
import threading
import time
import xml.sax.saxutils
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

import httpx
from pydub import AudioSegment

from .. import database

DEFAULT_REGION = "eastasia"
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
DEFAULT_LOCALE = "zh-CN"
DEFAULT_OUTPUT_FORMAT = "audio-24khz-48kbitrate-mono-mp3"
DEFAULT_CONCURRENCY = 4
DEFAULT_SPEECH_RATE = "0"
# Azure REST allows long SSML; keep a soft per-request ceiling for subtitle clips.
MAX_TTS_TEXT_CHARS = 1500
_USER_AGENT = "youdub-webui"
_HARD_BREAK_RE = re.compile(r"(?<=[。！？!?；;…])")
_SOFT_BREAK_RE = re.compile(r"(?<=[，,、：:——])|(?<=\s)")
# Azure may return HTTP 200 with an empty body for punctuation-only text, or for
# scripts the selected neural voice cannot synthesize (e.g. Tibetan with zh-CN).
_SPEAKABLE_RE = re.compile(r"\w", re.UNICODE)
_LATIN_RE = re.compile(r"[0-9A-Za-z\u00C0-\u024F]")
_CJK_RE = re.compile(r"[\u3040-\u30FF\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]")
_HANGUL_RE = re.compile(r"[\u1100-\u11FF\u3130-\u318F\uAC00-\uD7AF]")
_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")
_MIN_AUDIO_BYTES = 32
_REQUEST_ATTEMPTS = 3
_RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
_SILENT_CLIP_MS = 250
logger = logging.getLogger(__name__)


class AzureTtsError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def _tts_text(item: dict) -> str:
    text = item.get("dst") or item.get("zh", "")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("target text must be a non-empty string")
    text = text.replace("\n", " ")
    return re.sub(r"\s+", " ", text)


def _voice_lang(voice: str, locale: str) -> str:
    raw = (locale or "").strip() or _locale_from_voice(voice)
    return raw.split("-", 1)[0].lower()


def _is_speakable(text: str, *, voice: str = DEFAULT_VOICE, locale: str = "") -> bool:
    """Return True when the clip has characters the configured voice can synthesize."""
    if not _SPEAKABLE_RE.search(text):
        return False
    lang = _voice_lang(voice, locale)
    # Latin digits/letters are accepted broadly (code-switching / English lines).
    if _LATIN_RE.search(text):
        return True
    if lang in {"zh", "ja", "yue"} or voice.lower().startswith(("zh-", "ja-")):
        return bool(_CJK_RE.search(text))
    if lang == "ko" or voice.lower().startswith("ko-"):
        return bool(_HANGUL_RE.search(text) or _CJK_RE.search(text))
    if lang in {"ru", "uk", "bg"} or voice.lower().startswith(("ru-", "uk-", "bg-")):
        return bool(_CYRILLIC_RE.search(text))
    # Default neural voices in this app are zh/en; reject exotic-only scripts.
    return bool(_CJK_RE.search(text) or _HANGUL_RE.search(text) or _CYRILLIC_RE.search(text))


def _silent_clip(*, frame_rate: int = 24000) -> AudioSegment:
    return AudioSegment.silent(duration=_SILENT_CLIP_MS, frame_rate=frame_rate)


def _locale_from_voice(voice: str, fallback: str = DEFAULT_LOCALE) -> str:
    parts = voice.strip().split("-")
    if len(parts) >= 2 and 2 <= len(parts[0]) <= 3 and 2 <= len(parts[1]) <= 3:
        return f"{parts[0]}-{parts[1]}"
    return fallback


def resolve_endpoint(settings: dict[str, str]) -> str:
    configured = (settings.get("endpoint") or "").strip().rstrip("/")
    if configured:
        return configured
    region = (settings.get("region") or DEFAULT_REGION).strip() or DEFAULT_REGION
    return f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"


def resolve_voices_endpoint(settings: dict[str, str]) -> str:
    configured = (settings.get("endpoint") or "").strip().rstrip("/")
    if configured:
        if configured.endswith("/cognitiveservices/v1"):
            return f"{configured[: -len('/cognitiveservices/v1')]}/cognitiveservices/voices/list"
        return f"{configured}/voices/list"
    region = (settings.get("region") or DEFAULT_REGION).strip() or DEFAULT_REGION
    return f"https://{region}.tts.speech.microsoft.com/cognitiveservices/voices/list"


def _speech_rate_attr(raw: str) -> str | None:
    value = (raw or "").strip()
    if not value or value == "0":
        return None
    if value.endswith("%"):
        return value
    if re.fullmatch(r"[+-]?\d+", value):
        return f"{int(value):+d}%"
    return value


def build_ssml(text: str, *, voice: str, locale: str, speech_rate: str) -> str:
    escaped = xml.sax.saxutils.escape(text)
    rate = _speech_rate_attr(speech_rate)
    inner = escaped if rate is None else f'<prosody rate="{xml.sax.saxutils.escape(rate)}">{escaped}</prosody>'
    return (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        f'xml:lang="{xml.sax.saxutils.escape(locale)}">'
        f'<voice name="{xml.sax.saxutils.escape(voice)}">{inner}</voice></speak>'
    )


def _text_within_limit(text: str, *, max_chars: int = MAX_TTS_TEXT_CHARS) -> bool:
    return len(text) <= max_chars


def _pack_fragments(fragments: list[str], *, max_chars: int) -> list[str]:
    packed: list[str] = []
    buf = ""
    for fragment in fragments:
        piece = fragment.strip()
        if not piece:
            continue
        candidate = f"{buf}{piece}" if buf else piece
        if buf and not _text_within_limit(candidate, max_chars=max_chars):
            packed.append(buf)
            buf = piece
        else:
            buf = candidate
    if buf:
        packed.append(buf)
    return packed


def _hard_cut(text: str, *, max_chars: int) -> list[str]:
    return [text[i : i + max_chars] for i in range(0, len(text), max_chars)] or [text]


def _split_tts_text(text: str, *, max_chars: int = MAX_TTS_TEXT_CHARS) -> list[str]:
    cleaned = text.strip()
    if not cleaned:
        return []
    if _text_within_limit(cleaned, max_chars=max_chars):
        return [cleaned]

    for pattern in (_HARD_BREAK_RE, _SOFT_BREAK_RE):
        fragments = [part for part in pattern.split(cleaned) if part.strip()]
        if len(fragments) <= 1:
            continue
        packed = _pack_fragments(fragments, max_chars=max_chars)
        result: list[str] = []
        for part in packed:
            if _text_within_limit(part, max_chars=max_chars):
                result.append(part)
            else:
                result.extend(_split_tts_text(part, max_chars=max_chars))
        if result:
            return result
    return _hard_cut(cleaned, max_chars=max_chars)


def _concurrency_from(settings: dict[str, str]) -> int:
    raw = str(settings.get("concurrency") or "").strip()
    if not raw or not all("0" <= char <= "9" for char in raw):
        return DEFAULT_CONCURRENCY
    concurrency = int(raw)
    if concurrency < 1 or concurrency > 200:
        return DEFAULT_CONCURRENCY
    return concurrency


def _auth_headers(settings: dict[str, str], *, output_format: str) -> dict[str, str]:
    subscription_key = (settings.get("subscription_key") or "").strip()
    if not subscription_key:
        raise RuntimeError(
            "Azure TTS is not configured. Set subscription_key in Settings / environment."
        )
    return {
        "Ocp-Apim-Subscription-Key": subscription_key,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": output_format,
        "User-Agent": _USER_AGENT,
    }


def list_voices(*, region: str = "", subscription_key: str = "", endpoint: str = "") -> list[str]:
    settings = {
        "region": region,
        "subscription_key": subscription_key,
        "endpoint": endpoint,
    }
    headers = {
        "Ocp-Apim-Subscription-Key": (subscription_key or "").strip(),
        "User-Agent": _USER_AGENT,
    }
    if not headers["Ocp-Apim-Subscription-Key"]:
        raise ValueError("Azure TTS subscription key is not configured.")
    url = resolve_voices_endpoint(settings)
    with httpx.Client(timeout=60.0) as client:
        response = client.get(url, headers=headers)
        response.raise_for_status()
        payload = response.json()
    voices: list[str] = []
    seen: set[str] = set()
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            short_name = str(item.get("ShortName") or item.get("shortName") or "").strip()
            if short_name and short_name not in seen:
                seen.add(short_name)
                voices.append(short_name)
    return voices


def _response_detail(response: httpx.Response) -> str:
    content_type = (response.headers.get("content-type") or "").strip()
    request_id = (
        response.headers.get("x-request-id")
        or response.headers.get("apim-request-id")
        or response.headers.get("request-id")
        or ""
    ).strip()
    body = (response.text or "").strip().replace("\n", " ")[:500]
    parts = [f"status={response.status_code}"]
    if content_type:
        parts.append(f"content-type={content_type}")
    if request_id:
        parts.append(f"request-id={request_id}")
    if body:
        parts.append(f"body={body}")
    elif response.content is not None:
        parts.append(f"bytes={len(response.content)}")
    return ", ".join(parts)


def _decode_audio(audio_bytes: bytes, output_format: str) -> AudioSegment:
    fmt = "mp3" if "mp3" in output_format.lower() else None
    if fmt:
        return AudioSegment.from_file(io.BytesIO(audio_bytes), format=fmt)
    return AudioSegment.from_file(io.BytesIO(audio_bytes))


def _request_speech_once(text: str, settings: dict[str, str]) -> AudioSegment:
    voice = (settings.get("voice") or DEFAULT_VOICE).strip() or DEFAULT_VOICE
    locale = (settings.get("locale") or "").strip() or _locale_from_voice(voice)
    output_format = (
        (settings.get("output_format") or DEFAULT_OUTPUT_FORMAT).strip() or DEFAULT_OUTPUT_FORMAT
    )
    speech_rate = settings.get("speech_rate") or DEFAULT_SPEECH_RATE
    ssml = build_ssml(text, voice=voice, locale=locale, speech_rate=speech_rate)
    endpoint = resolve_endpoint(settings)
    headers = _auth_headers(settings, output_format=output_format)

    with httpx.Client(timeout=120.0) as client:
        response = client.post(endpoint, headers=headers, content=ssml.encode("utf-8"))
        content_type = (response.headers.get("content-type") or "").lower()
        if response.status_code >= 400:
            detail = _response_detail(response)
            raise AzureTtsError(
                f"Azure TTS failed ({detail})",
                retryable=response.status_code in _RETRY_STATUSES,
            )

        audio_bytes = response.content or b""
        if len(audio_bytes) < _MIN_AUDIO_BYTES:
            detail = _response_detail(response)
            preview = text if len(text) <= 80 else f"{text[:77]}..."
            raise AzureTtsError(
                "Azure TTS returned no audio data "
                f"({detail}; voice={voice}; locale={locale}; text={preview!r}). "
                "Check region/key match, voice availability, and speakable subtitle text.",
                retryable=True,
            )

        if content_type and "audio" not in content_type and "octet-stream" not in content_type:
            detail = _response_detail(response)
            raise AzureTtsError(f"Azure TTS returned a non-audio response ({detail})")

    return _decode_audio(audio_bytes, output_format)


def _request_speech(text: str, settings: dict[str, str]) -> AudioSegment:
    last_error: Exception | None = None
    for attempt in range(1, _REQUEST_ATTEMPTS + 1):
        try:
            return _request_speech_once(text, settings)
        except AzureTtsError as exc:
            last_error = exc
            if not exc.retryable or attempt >= _REQUEST_ATTEMPTS:
                raise
            delay = min(2.0 * attempt, 6.0)
            logger.warning(
                "Azure TTS attempt %s/%s failed (%s); retrying in %.1fs",
                attempt,
                _REQUEST_ATTEMPTS,
                exc,
                delay,
            )
            time.sleep(delay)
    assert last_error is not None
    raise last_error


def synthesize_speech(text: str, settings: dict[str, str] | None = None) -> AudioSegment:
    resolved = settings or database.get_azure_tts_settings()
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("target text must be a non-empty string")
    voice = (resolved.get("voice") or DEFAULT_VOICE).strip() or DEFAULT_VOICE
    locale = (resolved.get("locale") or "").strip() or _locale_from_voice(voice)
    if not _is_speakable(cleaned, voice=voice, locale=locale):
        # Punctuation-only or scripts the voice cannot synthesize (e.g. Tibetan with zh-CN)
        # cause Azure to return HTTP 200 with an empty audio body.
        preview = cleaned if len(cleaned) <= 80 else f"{cleaned[:77]}..."
        logger.warning(
            "Skipping Azure TTS for unsupported text with voice=%s locale=%s: %r",
            voice,
            locale,
            preview,
        )
        return _silent_clip()
    chunks = _split_tts_text(cleaned)
    if not chunks:
        raise ValueError("target text must be a non-empty string")

    def _synthesize_chunk(chunk: str) -> AudioSegment:
        if not _is_speakable(chunk, voice=voice, locale=locale):
            return _silent_clip()
        try:
            return _request_speech(chunk, resolved)
        except AzureTtsError as exc:
            if "no audio data" not in str(exc):
                raise
            # Persistent empty audio for an otherwise speakable clip: keep the
            # pipeline moving instead of failing the entire TTS stage.
            preview = chunk if len(chunk) <= 80 else f"{chunk[:77]}..."
            logger.warning(
                "Azure TTS returned empty audio after retries; using silence for %r (%s)",
                preview,
                exc,
            )
            return _silent_clip()

    combined = _synthesize_chunk(chunks[0])
    for chunk in chunks[1:]:
        combined += _synthesize_chunk(chunk)
    return combined


def generate_tts(
    translation_file: Path,
    session: Path,
    progress_callback: Callable[[int, str], None] | None = None,
    settings: dict[str, str] | None = None,
) -> Path:
    output_dir = session / "segments" / "tts"
    output_dir.mkdir(parents=True, exist_ok=True)
    data = json.loads(translation_file.read_text(encoding="utf-8"))
    items = data["translation"]
    total = len(items)
    if total == 0:
        if progress_callback:
            progress_callback(100, "No TTS clips to generate")
        return output_dir

    resolved = settings or database.get_azure_tts_settings()
    concurrency = _concurrency_from(resolved)
    pending: list[tuple[int, dict]] = []
    completed = 0
    for index, item in enumerate(items, start=1):
        output_file = output_dir / f"{index:04d}.wav"
        if output_file.exists():
            completed += 1
        else:
            pending.append((index, item))

    if progress_callback and completed:
        progress_callback(
            round(completed / total * 100),
            f"Prepared {completed}/{total} Azure TTS clips",
        )
    if not pending:
        if progress_callback:
            progress_callback(100, f"Prepared {total}/{total} Azure TTS clips")
        return output_dir

    progress_lock = threading.Lock()

    def synthesize_one(index: int, item: dict) -> None:
        audio = synthesize_speech(_tts_text(item), resolved)
        audio.export(output_dir / f"{index:04d}.wav", format="wav")

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(synthesize_one, index, item): index for index, item in pending
        }
        for future in as_completed(futures):
            future.result()
            if not progress_callback:
                continue
            with progress_lock:
                completed += 1
                progress = round(completed / total * 100)
                progress_callback(
                    progress,
                    f"Prepared {completed}/{total} Azure TTS clips (x{concurrency})",
                )

    return output_dir
