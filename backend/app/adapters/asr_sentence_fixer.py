from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


# Merge short English ASR fragments into readable sentence units before translate/TTS.
MAX_MERGE_GAP_MS = 600
MAX_MERGED_DURATION_MS = 10_000
MAX_MERGED_WORDS = 50
SHORT_FRAGMENT_WORDS = 4

_TERMINAL_RE = re.compile(r"""[.!?…](?:["'`”’)\]]+)?\s*$""")
_LEADING_WORD_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)?")

# Next-utterance tokens that usually continue the previous clause.
_CONTINUATION_STARTERS = frozenset(
    {
        "and",
        "or",
        "but",
        "so",
        "that",
        "which",
        "who",
        "whom",
        "whose",
        "to",
        "of",
        "in",
        "on",
        "at",
        "for",
        "with",
        "from",
        "as",
        "by",
        "into",
        "onto",
        "about",
        "over",
        "under",
        "than",
        "then",
        "because",
        "while",
        "when",
        "where",
        "if",
        "unless",
        "until",
        "though",
        "although",
        "whether",
        "like",
        "just",
        "also",
        "even",
        "still",
        "yet",
        "nor",
        "across",
        "through",
        "between",
        "among",
        "after",
        "before",
        "during",
        "without",
        "within",
        "via",
        "per",
        "vs",
        "versus",
        "is",
        "are",
        "was",
        "were",
        "been",
        "being",
        "be",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "can",
        "could",
        "will",
        "would",
        "should",
        "might",
        "must",
        "not",
        "don't",
        "doesn't",
        "didn't",
        "won't",
        "can't",
        "isn't",
        "aren't",
        "wasn't",
        "weren't",
        "it's",
        "that's",
        "there's",
        "here's",
        "we're",
        "they're",
        "you're",
        "i'm",
        "a",
        "an",
        "the",
        "this",
        "these",
        "those",
        "their",
        "our",
        "your",
        "my",
        "his",
        "her",
        "its",
        "all",
        "some",
        "any",
        "more",
        "most",
        "such",
        "really",
        "actually",
        "basically",
        "literally",
        "probably",
        "honestly",
        "completely",
        "totally",
        "very",
        "much",
        "many",
        "few",
        "lot",
        "lots",
        "kind",
        "sort",
        "type",
        "types",
    }
)

# Tag questions / acknowledgements that belong to the previous sentence.
_TRAILING_FILLERS = frozenset(
    {
        "right",
        "right?",
        "right.",
        "you know",
        "you know?",
        "okay?",
        "ok?",
        "yeah?",
        "yep?",
    }
)

# Discourse markers that introduce the next sentence.
_LEADING_FILLERS = frozenset(
    {
        "again",
        "again,",
        "again.",
        "so",
        "so,",
        "well",
        "well,",
        "i mean",
        "i mean,",
        "okay",
        "okay,",
        "okay.",
        "ok",
        "ok,",
        "ok.",
        "yeah",
        "yeah,",
        "yeah.",
        "yep",
        "yep,",
        "uh",
        "uh,",
        "um",
        "um,",
        "ah",
        "ah,",
        "oh",
        "oh,",
        "like,",
        "honestly",
        "honestly,",
        "basically",
        "basically,",
        "you know,",
    }
)


def _start_pad(idx: int, utts: list, start_pad: int, end_pad: int, min_gap: int) -> int:
    orig_start = utts[idx]["start_time"]
    if idx == 0:
        return max(0, orig_start - start_pad)

    prev_end = utts[idx - 1]["end_time"]
    gap = orig_start - prev_end
    total = start_pad + end_pad

    if gap >= total + min_gap:
        return orig_start - start_pad
    if gap > min_gap:
        share = int((gap - min_gap) * start_pad / total)
        return orig_start - share
    return prev_end + gap // 2


def _end_pad(idx: int, utts: list, duration: int, start_pad: int, end_pad: int, min_gap: int) -> int:
    orig_end = utts[idx]["end_time"]
    if idx == len(utts) - 1:
        return min(duration, orig_end + end_pad) if duration else orig_end + end_pad

    next_start = utts[idx + 1]["start_time"]
    gap = next_start - orig_end
    total = start_pad + end_pad

    if gap >= total + min_gap:
        return orig_end + end_pad
    if gap > min_gap:
        share = int((gap - min_gap) * end_pad / total)
        return orig_end + share
    return orig_end + gap // 2


def _apply_padding(utts: list, duration: int, start_pad: int, end_pad: int) -> list:
    if not utts:
        return utts

    min_gap = 50
    result = []
    for idx in range(len(utts)):
        new_start = _start_pad(idx, utts, start_pad, end_pad, min_gap)
        new_end = _end_pad(idx, utts, duration, start_pad, end_pad, min_gap)
        clamped_end = min(duration, new_end) if duration else new_end
        result.append({
            **utts[idx],
            "start_time": max(0, new_start),
            "end_time": clamped_end,
        })
    return result


def _normalize(utterances: list) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for utt in utterances:
        text = str(utt.get("text") or "").strip()
        if not text:
            continue
        item: dict[str, Any] = {
            "text": text,
            "start_time": int(utt["start_time"]),
            "end_time": int(utt["end_time"]),
        }
        words = utt.get("words")
        if isinstance(words, list) and words:
            item["words"] = words
        speaker = utt.get("speaker")
        if speaker is not None:
            item["speaker"] = speaker
        normalized.append(item)
    return normalized


def _word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)?", text))


def _has_strong_terminal(text: str) -> bool:
    return bool(_TERMINAL_RE.search((text or "").rstrip()))


def _normalize_filler(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def _is_trailing_filler(text: str) -> bool:
    cleaned = _normalize_filler(text)
    if cleaned in _TRAILING_FILLERS:
        return True
    return _word_count(cleaned) <= 2 and cleaned.rstrip(".?!,") in {"right", "okay", "ok", "yeah", "yep"}


def _is_leading_filler(text: str) -> bool:
    cleaned = _normalize_filler(text)
    if cleaned in _LEADING_FILLERS:
        return True
    return _word_count(cleaned) <= 2 and cleaned.rstrip(".?!,") in {
        "again",
        "so",
        "well",
        "okay",
        "ok",
        "yeah",
        "yep",
        "uh",
        "um",
        "ah",
        "oh",
    }


def _leading_word(text: str) -> str:
    match = _LEADING_WORD_RE.search(text or "")
    if not match:
        return ""
    return match.group(0).lower().replace("’", "'")


def _looks_like_continuation(text: str) -> bool:
    stripped = (text or "").lstrip()
    if not stripped:
        return False
    # Mid-clause ASR fragments often restart with a lowercase letter.
    first_alpha = next((ch for ch in stripped if ch.isalpha()), "")
    if first_alpha and first_alpha.islower():
        return True
    return _leading_word(stripped) in _CONTINUATION_STARTERS


def _join_texts(left: str, right: str) -> str:
    left = left.strip()
    right = right.strip()
    if not left:
        return right
    if not right:
        return left
    if left.endswith("-") and not left.endswith("--"):
        return f"{left[:-1]}{right}"
    return f"{left} {right}"


def _merge_pair(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {
        "text": _join_texts(str(left["text"]), str(right["text"])),
        "start_time": int(left["start_time"]),
        "end_time": int(right["end_time"]),
    }
    left_words = left.get("words")
    right_words = right.get("words")
    if isinstance(left_words, list) or isinstance(right_words, list):
        merged["words"] = [*(left_words or []), *(right_words or [])]
    speaker = left.get("speaker")
    if speaker is None:
        speaker = right.get("speaker")
    if speaker is not None:
        merged["speaker"] = speaker
    return merged


def _within_merge_limits(left: dict[str, Any], right: dict[str, Any]) -> bool:
    gap = int(right["start_time"]) - int(left["end_time"])
    if gap > MAX_MERGE_GAP_MS:
        return False
    duration = int(right["end_time"]) - int(left["start_time"])
    if duration > MAX_MERGED_DURATION_MS:
        return False
    words = _word_count(str(left["text"])) + _word_count(str(right["text"]))
    if words > MAX_MERGED_WORDS:
        return False
    return True


def _should_merge(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if not _within_merge_limits(left, right):
        return False

    left_text = str(left["text"])
    right_text = str(right["text"])

    # ".... topology." + "right?" → keep the tag with the previous sentence.
    if _is_trailing_filler(right_text):
        return True

    # "Again," + "you'll see examples." → attach opener to the following clause.
    if _is_leading_filler(left_text):
        return True

    # Completed sentence: keep the boundary.
    if _has_strong_terminal(left_text):
        return False

    # Incomplete left clause continued by next fragment.
    if _looks_like_continuation(right_text):
        return True

    # Ultra-short fragments without terminal punctuation are usually choppy ASR.
    if _word_count(left_text) <= SHORT_FRAGMENT_WORDS:
        return True

    return False


def merge_english_utterances(utterances: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Greedy left-to-right merge of over-segmented English ASR utterances."""
    if not utterances:
        return []

    merged: list[dict[str, Any]] = []
    current = dict(utterances[0])
    for nxt in utterances[1:]:
        if _should_merge(current, nxt):
            current = _merge_pair(current, nxt)
            continue
        merged.append(current)
        current = dict(nxt)
    merged.append(current)
    return merged


def _should_merge_language(language: str) -> bool:
    lang = (language or "en").strip().lower()
    return lang.startswith("en")


def fix_asr_sentences(
    asr_file: Path,
    session: Path,
    start_pad: int = 100,
    end_pad: int = 300,
    language: str = "en",
) -> Path:
    output_file = session / "metadata" / "asr_fixed.json"
    if output_file.exists():
        return output_file

    data = json.loads(Path(asr_file).read_text(encoding="utf-8"))
    utterances = data["result"]["utterances"]
    duration = data.get("audio_info", {}).get("duration", 0)

    new_utts = _normalize(utterances)
    if not new_utts:
        raise RuntimeError("ASR result has no utterances.")

    if _should_merge_language(language):
        new_utts = merge_english_utterances(new_utts)

    # Rebuild full text from merged utterances for downstream debugging.
    joined_text = " ".join(u["text"] for u in new_utts)

    padded = _apply_padding(new_utts, duration, start_pad, end_pad)
    payload = {
        "audio_info": data.get("audio_info", {}),
        "result": {"text": joined_text or data["result"].get("text", ""), "utterances": padded},
    }
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_file
