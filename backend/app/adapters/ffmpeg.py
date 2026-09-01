from __future__ import annotations

import json
import logging
import re
import subprocess
from functools import lru_cache
from pathlib import Path

from ..config import (
    ffmpeg_binary,
    ffprobe_binary,
    merge_video_crf,
    merge_video_encoder,
    merge_video_nvenc_preset,
)

log = logging.getLogger(__name__)

SUBTITLE_PUNCTUATION = {"，", ",", "；", ";", "：", ":", "。", "?", "？", "!", "！", "、"}
SUBTITLE_PROTECTED_PAIRS = {"《": "》", "（": "）", "【": "】", "「": "」", "『": "』"}
SUBTITLE_CLOSING_QUOTES = {'"', "'", "」", "』", "》", "）", "】", "\u201d", "\u2019", "]"}
SUBTITLE_MIN_FRAGMENT_LEN = 5
SUBTITLE_MIN_DURATION_MS = 200
SUBTITLE_TAIL_BUFFER_MS = 100
SUBTITLE_DURATION_FLOOR_MS = 600


SUBTITLE_FONTS = {
    "zh": "Noto Sans CJK SC",
    "en": "Arial",
}

SUBTITLE_FONT_SIZES = {
    "zh": {"portrait": 7, "landscape": 14},
    "en": {"portrait": 6, "landscape": 11},
}


def _subtitle_style(font: str, size: int, margin_v: int) -> str:
    return (
        f"FontName={font},"
        f"FontSize={size},"
        # ASS colour is &HAABBGGRR; opaque yellow.
        "PrimaryColour=&H0000FFFF,"
        "OutlineColour=&H00000000,"
        "BorderStyle=1,"
        "Outline=1,"
        "Alignment=2,"
        f"MarginV={margin_v}"
    )


def _srt_time(ms: int) -> str:
    hours = ms // 3_600_000
    ms -= hours * 3_600_000
    minutes = ms // 60_000
    ms -= minutes * 60_000
    seconds = ms // 1000
    millis = ms - seconds * 1000
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _split_protected(text: str) -> list[str]:
    segments: list[str] = []
    buf: list[str] = []
    inside = None
    for ch in text:
        if inside is None and ch in SUBTITLE_PROTECTED_PAIRS:
            inside = SUBTITLE_PROTECTED_PAIRS[ch]
            buf.append(ch)
            continue
        if inside is not None and ch == inside:
            inside = None
            buf.append(ch)
            continue
        if inside is None and ch in SUBTITLE_PUNCTUATION:
            chunk = "".join(buf).strip()
            if chunk:
                segments.append(chunk)
            buf.clear()
            continue
        buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        segments.append(tail)
    return segments


def _attach_closing_quotes(segments: list[str]) -> list[str]:
    fixed: list[str] = []
    for seg in segments:
        if seg and seg[0] in SUBTITLE_CLOSING_QUOTES and fixed:
            fixed[-1] = f"{fixed[-1]}{seg}".strip()
            continue
        fixed.append(seg.strip())
    return fixed


def _merge_short_fragments(segments: list[str]) -> list[str]:
    merged: list[str] = []
    i = 0
    while i < len(segments):
        cur = segments[i]
        if len(cur.strip()) < SUBTITLE_MIN_FRAGMENT_LEN and i + 1 < len(segments):
            segments[i + 1] = f"{cur}{segments[i + 1]}".strip()
            i += 1
            continue
        merged.append(cur)
        i += 1
    return merged


def _strip_trailing_punct(segments: list[str]) -> list[str]:
    cleaned: list[str] = []
    for item in segments:
        text = item.strip()
        if not text:
            continue
        if text.endswith(("，", ",", "。")):
            text = text[:-1]
        cleaned.append(re.sub(r"\s+", " ", text).strip())
    return cleaned


def split_subtitle_text(text: str) -> list[str]:
    original = (text or "").strip()
    if not original:
        return []
    segments = _split_protected(original)
    if not segments:
        return [original]
    segments = _attach_closing_quotes(segments)
    segments = _merge_short_fragments(segments)
    cleaned = _strip_trailing_punct(segments)
    return cleaned or [original]


def _allocate_durations(fragments: list[str], total_duration: int) -> list[int]:
    if len(fragments) == 1:
        return [total_duration]
    weights = [max(1, len(f.replace(" ", ""))) for f in fragments]
    total_weight = sum(weights)
    durations: list[int] = []
    allocated = 0
    for i, weight in enumerate(weights[:-1]):
        share = round(total_duration * weight / total_weight)
        if total_duration >= SUBTITLE_DURATION_FLOOR_MS:
            ceiling = total_duration - allocated - SUBTITLE_TAIL_BUFFER_MS
            share = max(SUBTITLE_MIN_DURATION_MS, min(share, ceiling))
        else:
            share = max(int(SUBTITLE_MIN_DURATION_MS / 2), share)
        durations.append(share)
        allocated += share
    durations.append(max(SUBTITLE_TAIL_BUFFER_MS, total_duration - allocated))
    return durations


def _segment_times(item: dict) -> tuple[int, int]:
    start = int(item.get("actual_start_time", item["start_time"]))
    end = int(item.get("actual_end_time", item["end_time"]))
    return start, end


def _dst_text(item: dict) -> str:
    return str(item.get("dst") or item.get("zh") or "").strip()


def _src_text(item: dict) -> str:
    return str(item.get("src") or "").strip()


def _looks_mostly_cjk(text: str) -> bool:
    letters = [ch for ch in text if not ch.isspace()]
    if not letters:
        return False
    cjk = sum(1 for ch in letters if "\u4e00" <= ch <= "\u9fff")
    return cjk / len(letters) >= 0.5


def _chinese_text(item: dict) -> str:
    if item.get("dst_lang") == "zh":
        return str(item.get("dst") or item.get("zh") or "").strip()
    if item.get("src_lang") == "zh":
        return str(item.get("src") or "").strip()
    return str(item.get("zh") or "").strip()


def _english_text(item: dict) -> str:
    """English-only burn-in text for the final video (no Chinese)."""
    src_lang = item.get("src_lang")
    dst_lang = item.get("dst_lang")
    if dst_lang == "en":
        return _dst_text(item)
    if src_lang == "en":
        return _src_text(item)
    if dst_lang == "zh":
        return _src_text(item)
    if src_lang == "zh":
        return _dst_text(item)

    src = _src_text(item)
    dst = _dst_text(item)
    if src and not _looks_mostly_cjk(src):
        return src
    if dst and not _looks_mostly_cjk(dst):
        return dst
    return ""


def _write_srt_lines(
    translation: list[dict],
    text_getter,
) -> list[str]:
    lines: list[str] = []
    idx = 1
    for item in translation:
        start, end = _segment_times(item)
        if end <= start:
            continue
        fragments = split_subtitle_text(text_getter(item))
        if not fragments:
            continue
        cursor = start
        for fragment, duration in zip(fragments, _allocate_durations(fragments, end - start)):
            lines.extend([str(idx), f"{_srt_time(cursor)} --> {_srt_time(cursor + duration)}", fragment, ""])
            cursor += duration
            idx += 1
    return lines


def write_srt(translation_file: Path, session: Path) -> Path:
    """Write English-only SRT used when burning subtitles into the final video."""
    data = json.loads(translation_file.read_text(encoding="utf-8"))
    translation = data["translation"]
    output_file = session / "metadata" / "subtitles.en.srt"
    lines = _write_srt_lines(translation, _english_text)
    output_file.write_text("\n".join(lines), encoding="utf-8")
    return output_file


def write_chinese_srt(translation_file: Path, session: Path) -> Path | None:
    data = json.loads(translation_file.read_text(encoding="utf-8"))
    translation = data.get("translation") or []
    lines = _write_srt_lines(translation, _chinese_text)
    if not lines:
        return None
    output_file = session / "metadata" / "subtitles.zh.srt"
    output_file.write_text("\n".join(lines), encoding="utf-8")
    return output_file


def probe_video_size(video_file: Path) -> tuple[int, int] | None:
    result = subprocess.run(
        [
            ffprobe_binary(),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            str(video_file),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None

    lines = result.stdout.strip().splitlines()
    if not lines:
        return None
    parts = lines[0].split(",", maxsplit=1)
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def get_video_orientation(video_file: Path) -> str:
    size = probe_video_size(video_file)
    if size is None:
        return "landscape"
    width, height = size
    return "portrait" if height > width else "landscape"


def subtitle_style_for_orientation(orientation: str, font: str, lang: str = "zh") -> str:
    sizes = SUBTITLE_FONT_SIZES.get(lang, SUBTITLE_FONT_SIZES["zh"])
    # Larger MarginV lifts bottom-aligned subtitles higher in the frame.
    margin_v = 100 if orientation == "portrait" else 28
    return _subtitle_style(font, size=sizes[orientation], margin_v=margin_v)


def _subtitle_filter_path(subtitle_file: Path, session: Path) -> str:
    try:
        return subtitle_file.resolve().relative_to(session.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError("Subtitle file must be inside the session directory.") from exc


def _subtitle_contains_cjk(subtitle_file: Path) -> bool:
    try:
        text = subtitle_file.read_text(encoding="utf-8")
    except OSError:
        return False
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def subtitle_filter(video_file: Path, subtitle_file: Path, session: Path) -> str:
    lang = subtitle_file.stem.rsplit(".", 1)[-1]
    # Bilingual cues often mix CJK with Latin; prefer a CJK-capable font then.
    if lang == "zh" or _subtitle_contains_cjk(subtitle_file):
        font = SUBTITLE_FONTS["zh"]
        style_lang = "zh"
    else:
        font = SUBTITLE_FONTS.get(lang, "Arial")
        style_lang = lang if lang in SUBTITLE_FONT_SIZES else "en"
    style = subtitle_style_for_orientation(
        get_video_orientation(video_file), font, style_lang
    )
    sub_path = _subtitle_filter_path(subtitle_file, session)
    return f"subtitles=filename='{sub_path}':force_style='{style}'"


@lru_cache(maxsize=1)
def _list_ffmpeg_video_encoders() -> frozenset[str]:
    result = subprocess.run(
        [ffmpeg_binary(), "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return frozenset()
    encoders: set[str] = set()
    for line in result.stdout.splitlines():
        if not line.startswith(" "):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("V"):
            encoders.add(parts[1])
    return frozenset(encoders)


def _hardware_encoder_chain() -> list[str]:
    available = _list_ffmpeg_video_encoders()
    chain: list[str] = []
    if "h264_nvenc" in available:
        chain.append("nvenc")
    if "h264_qsv" in available:
        chain.append("qsv")
    if "h264_amf" in available:
        chain.append("amf")
    return chain


def merge_video_encoder_chain(*, burn_subtitles: bool, preferred: str | None = None) -> list[str]:
    """Return ordered encoder modes to try for merge_video."""
    mode = (preferred or merge_video_encoder()).strip().lower()
    hardware = _hardware_encoder_chain()
    cpu = ["x264"]

    if mode == "auto":
        if burn_subtitles:
            return [*hardware, *cpu]
        return ["copy", *hardware, *cpu]

    if mode == "copy":
        if burn_subtitles:
            return [*hardware, *cpu]
        return ["copy", *hardware, *cpu]

    if mode in {"nvenc", "qsv", "amf", "x264"}:
        chain = [mode]
        for candidate in [*hardware, *cpu]:
            if candidate not in chain:
                chain.append(candidate)
        return chain

    return [*hardware, *cpu]


def _video_encode_args(encoder: str) -> list[str]:
    crf = str(merge_video_crf())
    if encoder == "copy":
        return ["-c:v", "copy"]
    if encoder == "x264":
        return ["-c:v", "libx264", "-preset", "fast", "-crf", crf]
    if encoder == "nvenc":
        return [
            "-c:v",
            "h264_nvenc",
            "-preset",
            merge_video_nvenc_preset(),
            "-rc",
            "vbr",
            "-cq",
            crf,
            "-b:v",
            "0",
        ]
    if encoder == "qsv":
        return ["-c:v", "h264_qsv", "-global_quality", crf]
    if encoder == "amf":
        return [
            "-c:v",
            "h264_amf",
            "-quality",
            "balanced",
            "-rc",
            "cqp",
            "-qp_i",
            crf,
            "-qp_p",
            crf,
        ]
    raise ValueError(f"Unsupported merge video encoder: {encoder}")


def _build_merge_encode_command(
    *,
    video_input: Path,
    mixed_audio_output: Path,
    final_video_output: Path,
    session_dir: Path,
    burn_subtitles: bool,
    subtitles: Path | None,
    video_encoder: str,
) -> list[str]:
    encode_cmd = [
        ffmpeg_binary(),
        "-y",
        "-i",
        str(video_input),
        "-i",
        str(mixed_audio_output),
    ]
    if burn_subtitles and subtitles is not None:
        encode_cmd.extend(
            ["-vf", subtitle_filter(video_input, subtitles, session_dir)]
        )
    encode_cmd.extend(
        [
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            *_video_encode_args(video_encoder),
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            "-shortest",
            str(final_video_output),
        ]
    )
    return encode_cmd


def _run_merge_encode(
    *,
    video_input: Path,
    mixed_audio_output: Path,
    final_video_output: Path,
    session_dir: Path,
    burn_subtitles: bool,
    subtitles: Path | None,
    encoder_chain: list[str],
) -> str:
    errors: list[str] = []
    for encoder in encoder_chain:
        if final_video_output.exists():
            final_video_output.unlink()
        command = _build_merge_encode_command(
            video_input=video_input,
            mixed_audio_output=mixed_audio_output,
            final_video_output=final_video_output,
            session_dir=session_dir,
            burn_subtitles=burn_subtitles,
            subtitles=subtitles,
            video_encoder=encoder,
        )
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            cwd=session_dir,
        )
        if result.returncode == 0 and final_video_output.exists():
            if encoder != encoder_chain[0]:
                log.warning(
                    "merge_video fell back to %s after %s failed",
                    encoder,
                    encoder_chain[0],
                )
            return encoder
        detail = (result.stderr or result.stdout or "unknown ffmpeg error").strip()
        errors.append(f"{encoder}: {detail.splitlines()[-1] if detail else 'failed'}")
        log.warning("merge_video encoder %s failed: %s", encoder, detail[-500:])

    raise RuntimeError(
        "ffmpeg merge_video failed for all encoders ("
        + ", ".join(encoder_chain)
        + "): "
        + "; ".join(errors)
    )


def extract_source_audio(video_file: Path, session: Path) -> Path:
    """Extract the original mixed audio track for ASR / TTS reference use."""
    media_dir = session / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    vocals_file = media_dir / "audio_vocals.wav"
    if vocals_file.exists():
        return vocals_file

    subprocess.run(
        [
            ffmpeg_binary(),
            "-y",
            "-i",
            str(video_file.resolve()),
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "44100",
            "-ac",
            "2",
            str(vocals_file.resolve()),
        ],
        check=True,
    )
    if not vocals_file.exists():
        raise RuntimeError("ffmpeg finished without producing media/audio_vocals.wav")
    return vocals_file


def merge_video(
    video_file: Path,
    dubbing_file: Path,
    bgm_file: Path | None,
    timings_file: Path,
    session: Path,
    *,
    replace_audio: bool = False,
) -> Path:
    tmp_dir = session / "tmp"
    media_dir = session / "media"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    media_dir.mkdir(parents=True, exist_ok=True)
    final_video = media_dir / "video_final.mp4"
    if final_video.exists():
        return final_video

    session_dir = session.resolve()
    video_input = video_file.resolve()
    dubbing_input = dubbing_file.resolve()
    burn_subtitles = get_video_orientation(video_input) != "portrait"
    subtitles = write_srt(timings_file, session) if burn_subtitles else None
    mixed_audio = tmp_dir / "audio_mixed.m4a"
    mixed_audio_output = mixed_audio.resolve()
    final_video_output = final_video.resolve()

    if replace_audio or bgm_file is None:
        subprocess.run(
            [
                ffmpeg_binary(),
                "-y",
                "-i",
                str(dubbing_input),
                "-c:a",
                "aac",
                str(mixed_audio_output),
            ],
            check=True,
        )
    else:
        bgm_input = bgm_file.resolve()
        subprocess.run(
            [
                ffmpeg_binary(),
                "-y",
                "-i",
                str(dubbing_input),
                "-i",
                str(bgm_input),
                "-filter_complex",
                "[0:a]volume=1.0[a0];[1:a]volume=0.30[a1];[a0][a1]amix=inputs=2:duration=longest:normalize=0[aout]",
                "-map",
                "[aout]",
                "-c:a",
                "aac",
                str(mixed_audio_output),
            ],
            check=True,
        )
    encoder_chain = merge_video_encoder_chain(burn_subtitles=burn_subtitles)
    used_encoder = _run_merge_encode(
        video_input=video_input,
        mixed_audio_output=mixed_audio_output,
        final_video_output=final_video_output,
        session_dir=session_dir,
        burn_subtitles=burn_subtitles,
        subtitles=subtitles,
        encoder_chain=encoder_chain,
    )
    log.info("merge_video finished with encoder %s", used_encoder)
    return final_video
