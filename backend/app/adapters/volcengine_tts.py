from __future__ import annotations

import base64
import io
import json
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

import httpx
from pydub import AudioSegment

from .. import database

DEFAULT_ENDPOINT = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
DEFAULT_CONCURRENCY = 4
STREAM_END_CODE = 20000000
# HTTP unidirectional TTS commonly enforces ~1024 UTF-8 bytes; keep a margin.
MAX_TTS_TEXT_CHARS = 300
MAX_TTS_TEXT_BYTES = 900
_HARD_BREAK_RE = re.compile(r"(?<=[。！？!?；;…])")
_SOFT_BREAK_RE = re.compile(r"(?<=[，,、：:——])|(?<=\s)")


def _tts_text(item: dict) -> str:
    text = item.get("dst") or item.get("zh", "")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("target text must be a non-empty string")
    text = text.replace("\n", " ")
    return re.sub(r"\s+", " ", text)


def _text_within_limit(
    text: str,
    *,
    max_chars: int = MAX_TTS_TEXT_CHARS,
    max_bytes: int = MAX_TTS_TEXT_BYTES,
) -> bool:
    return len(text) <= max_chars and len(text.encode("utf-8")) <= max_bytes


def _pack_fragments(
    fragments: list[str],
    *,
    max_chars: int,
    max_bytes: int,
) -> list[str]:
    packed: list[str] = []
    buf = ""
    for fragment in fragments:
        piece = fragment.strip()
        if not piece:
            continue
        candidate = f"{buf}{piece}" if buf else piece
        if buf and not _text_within_limit(candidate, max_chars=max_chars, max_bytes=max_bytes):
            packed.append(buf)
            buf = piece
        else:
            buf = candidate
    if buf:
        packed.append(buf)
    return packed


def _hard_cut(text: str, *, max_chars: int, max_bytes: int) -> list[str]:
    chunks: list[str] = []
    remaining = text
    while remaining:
        if _text_within_limit(remaining, max_chars=max_chars, max_bytes=max_bytes):
            chunks.append(remaining)
            break
        cut = min(max_chars, len(remaining))
        while cut > 1 and len(remaining[:cut].encode("utf-8")) > max_bytes:
            cut -= 1
        if cut <= 0:
            cut = 1
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    return chunks


def _split_tts_text(
    text: str,
    *,
    max_chars: int = MAX_TTS_TEXT_CHARS,
    max_bytes: int = MAX_TTS_TEXT_BYTES,
) -> list[str]:
    cleaned = text.strip()
    if not cleaned:
        return []
    if _text_within_limit(cleaned, max_chars=max_chars, max_bytes=max_bytes):
        return [cleaned]

    for pattern in (_HARD_BREAK_RE, _SOFT_BREAK_RE):
        fragments = [part for part in pattern.split(cleaned) if part.strip()]
        if len(fragments) <= 1:
            continue
        packed = _pack_fragments(fragments, max_chars=max_chars, max_bytes=max_bytes)
        if all(_text_within_limit(part, max_chars=max_chars, max_bytes=max_bytes) for part in packed):
            return packed
        # Recurse on any still-oversized packed piece.
        result: list[str] = []
        for part in packed:
            result.extend(
                _split_tts_text(part, max_chars=max_chars, max_bytes=max_bytes)
                if not _text_within_limit(part, max_chars=max_chars, max_bytes=max_bytes)
                else [part]
            )
        if result:
            return result

    return _hard_cut(cleaned, max_chars=max_chars, max_bytes=max_bytes)


def _force_split_text(text: str) -> list[str]:
    cleaned = text.strip()
    if len(cleaned) < 2:
        raise RuntimeError(
            f"Volcengine TTS text still exceeds limit after splitting: {cleaned!r}"
        )
    half_chars = max(1, len(cleaned) // 2)
    half_bytes = max(1, len(cleaned.encode("utf-8")) // 2)
    parts = _split_tts_text(cleaned, max_chars=half_chars, max_bytes=half_bytes)
    if len(parts) < 2:
        mid = max(1, len(cleaned) // 2)
        parts = [cleaned[:mid], cleaned[mid:]]
    return [part for part in parts if part.strip()]


def _resolve_resource_id(resource_id: str, speaker: str) -> str:
    configured = resource_id.strip()
    # Cloned speakers (S_*) must use the seed-icl resource family.
    if speaker.startswith("S_"):
        if not configured or configured.startswith("seed-tts"):
            return "seed-icl-2.0"
        return configured
    if configured:
        return configured
    if "_uranus_" in speaker or speaker.startswith("saturn_"):
        return "seed-tts-2.0"
    return "seed-tts-1.0"


def _build_additions(speaker: str, resource_id: str) -> str | None:
    additions: dict[str, object] = {}
    if resource_id.startswith("seed-icl-2") or speaker.startswith("S_"):
        additions["model_type"] = 4
    if not additions:
        return None
    return json.dumps(additions, ensure_ascii=False)


def _auth_headers(settings: dict[str, str], resource_id: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "X-Api-Resource-Id": resource_id,
        "X-Api-Request-Id": str(uuid.uuid4()),
    }
    api_key = settings.get("api_key", "").strip()
    if api_key:
        headers["X-Api-Key"] = api_key
        return headers

    app_id = settings.get("app_id", "").strip()
    access_key = settings.get("access_key", "").strip()
    if not app_id or not access_key:
        raise RuntimeError(
            "Volcengine TTS is not configured. Set api_key, or both app_id and access_key "
            "in Settings / environment."
        )
    headers["X-Api-App-Id"] = app_id
    headers["X-Api-Access-Key"] = access_key
    return headers


def _parse_stream_audio(response_text: str) -> bytes:
    chunks: list[bytes] = []
    for raw_line in response_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid Volcengine TTS stream chunk: {line[:200]}") from exc

        code = payload.get("code")
        if code == STREAM_END_CODE:
            break
        if code not in (0, None):
            message = payload.get("message") or payload.get("msg") or str(payload)
            raise RuntimeError(f"Volcengine TTS failed ({code}): {message}")

        data = payload.get("data")
        if isinstance(data, str) and data:
            chunks.append(base64.b64decode(data))

    if not chunks:
        raise RuntimeError("Volcengine TTS returned no audio data.")
    return b"".join(chunks)


def _concurrency_from(settings: dict[str, str]) -> int:
    raw = str(settings.get("concurrency") or "").strip()
    if not raw or not all("0" <= char <= "9" for char in raw):
        return DEFAULT_CONCURRENCY
    concurrency = int(raw)
    if concurrency < 1 or concurrency > 200:
        return DEFAULT_CONCURRENCY
    return concurrency


def _is_text_limit_error(exc: Exception) -> bool:
    message = str(exc)
    return "40402003" in message or "TTSExceededTextLimit" in message


def _request_speech(text: str, settings: dict[str, str]) -> AudioSegment:
    speaker = settings.get("speaker", "").strip()
    if not speaker:
        raise RuntimeError("Volcengine TTS speaker is not configured.")

    resource_id = _resolve_resource_id(settings.get("resource_id", ""), speaker)
    endpoint = (settings.get("endpoint") or DEFAULT_ENDPOINT).strip()
    sample_rate = int(settings.get("sample_rate") or "24000")
    speech_rate = int(settings.get("speech_rate") or "0")
    uid = (settings.get("uid") or "youdub-webui").strip()

    req_params: dict[str, object] = {
        "text": text,
        "speaker": speaker,
        "audio_params": {
            "format": "mp3",
            "sample_rate": sample_rate,
            "speech_rate": speech_rate,
        },
    }
    additions = _build_additions(speaker, resource_id)
    if additions:
        req_params["additions"] = additions

    body = {"user": {"uid": uid}, "req_params": req_params}
    headers = _auth_headers(settings, resource_id)

    with httpx.Client(timeout=120.0) as client:
        response = client.post(endpoint, headers=headers, json=body)
        response.raise_for_status()
        audio_bytes = _parse_stream_audio(response.text)

    return AudioSegment.from_file(io.BytesIO(audio_bytes), format="mp3")


def _synthesize_chunk(text: str, settings: dict[str, str]) -> AudioSegment:
    try:
        return _request_speech(text, settings)
    except RuntimeError as exc:
        if not _is_text_limit_error(exc):
            raise
        parts = _force_split_text(text)
        combined = _synthesize_chunk(parts[0], settings)
        for part in parts[1:]:
            combined += _synthesize_chunk(part, settings)
        return combined


def synthesize_speech(text: str, settings: dict[str, str] | None = None) -> AudioSegment:
    resolved = settings or database.get_volcengine_tts_settings()
    chunks = _split_tts_text(text)
    if not chunks:
        raise ValueError("target text must be a non-empty string")
    combined = _synthesize_chunk(chunks[0], resolved)
    for chunk in chunks[1:]:
        combined += _synthesize_chunk(chunk, resolved)
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

    resolved = settings or database.get_volcengine_tts_settings()
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
            f"Prepared {completed}/{total} Volcengine TTS clips",
        )
    if not pending:
        if progress_callback:
            progress_callback(100, f"Prepared {total}/{total} Volcengine TTS clips")
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
                    f"Prepared {completed}/{total} Volcengine TTS clips (x{concurrency})",
                )

    return output_dir
