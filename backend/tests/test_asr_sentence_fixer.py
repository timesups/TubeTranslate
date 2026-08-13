from __future__ import annotations

import json

import pytest

from backend.app.adapters import asr_sentence_fixer


def _utt(text: str, start: int, end: int) -> dict:
    return {"text": text, "start_time": start, "end_time": end}


def _write_asr(tmp_path, utterances: list, duration: int = 10000, text: str = "") -> tuple:
    session = tmp_path / "session"
    (session / "metadata").mkdir(parents=True)
    asr_file = session / "metadata" / "asr.json"
    payload = {
        "audio_info": {"duration": duration},
        "result": {"text": text, "utterances": utterances},
    }
    asr_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return asr_file, session


def test_fix_asr_sentences_passes_through_utterances(tmp_path):
    utts = [_utt("Hello world.", 100, 1200), _utt("How are you?", 1500, 2800)]
    asr_file, session = _write_asr(tmp_path, utts)

    fixed = asr_sentence_fixer.fix_asr_sentences(asr_file, session, start_pad=50, end_pad=100)
    out = json.loads(fixed.read_text(encoding="utf-8"))["result"]["utterances"]

    assert [u["text"] for u in out] == ["Hello world.", "How are you?"]


def test_fix_asr_sentences_drops_empty_text(tmp_path):
    utts = [_utt("Hello.", 0, 500), _utt("   ", 600, 800), _utt("World.", 900, 1500)]
    asr_file, session = _write_asr(tmp_path, utts)

    fixed = asr_sentence_fixer.fix_asr_sentences(asr_file, session)
    out = json.loads(fixed.read_text(encoding="utf-8"))["result"]["utterances"]

    assert [u["text"] for u in out] == ["Hello.", "World."]


def test_fix_asr_sentences_applies_padding_within_gap(tmp_path):
    # Large gap keeps fragments separate so padding behavior stays observable.
    utts = [_utt("Alpha done.", 1000, 2000), _utt("Beta done.", 3000, 4000)]
    asr_file, session = _write_asr(tmp_path, utts, duration=5000)

    fixed = asr_sentence_fixer.fix_asr_sentences(asr_file, session, start_pad=100, end_pad=300)
    out = json.loads(fixed.read_text(encoding="utf-8"))["result"]["utterances"]

    assert out[0]["start_time"] == 900
    assert out[0]["end_time"] == 2300
    assert out[1]["start_time"] == 2900
    assert out[1]["end_time"] == 4300


def test_fix_asr_sentences_clamps_to_duration(tmp_path):
    utts = [_utt("Only one sentence.", 100, 4900)]
    asr_file, session = _write_asr(tmp_path, utts, duration=5000)

    fixed = asr_sentence_fixer.fix_asr_sentences(asr_file, session, start_pad=200, end_pad=500)
    out = json.loads(fixed.read_text(encoding="utf-8"))["result"]["utterances"]

    assert out[0]["start_time"] == 0  # 100 - 200 -> clamp 0
    assert out[0]["end_time"] == 5000


def test_fix_asr_sentences_raises_when_empty(tmp_path):
    asr_file, session = _write_asr(tmp_path, [_utt("  ", 0, 100)])

    with pytest.raises(RuntimeError):
        asr_sentence_fixer.fix_asr_sentences(asr_file, session)


def test_fix_asr_sentences_reuses_cache(tmp_path):
    utts = [_utt("hi there.", 0, 500)]
    asr_file, session = _write_asr(tmp_path, utts)

    first = asr_sentence_fixer.fix_asr_sentences(asr_file, session)
    first.write_text('{"already": true}', encoding="utf-8")
    second = asr_sentence_fixer.fix_asr_sentences(asr_file, session)

    assert json.loads(second.read_text(encoding="utf-8")) == {"already": True}


def test_merge_english_utterances_joins_mid_clause_fragments():
    utts = [
        _utt("to really be", 100, 400),
        _utt("a 3D artist", 420, 800),
        _utt("that understands topology.", 820, 1400),
        _utt("And as always,", 1800, 2200),
        _utt("check my courses.", 2220, 2600),
    ]
    merged = asr_sentence_fixer.merge_english_utterances(utts)
    assert [u["text"] for u in merged] == [
        "to really be a 3D artist that understands topology.",
        "And as always, check my courses.",
    ]
    assert merged[0]["start_time"] == 100
    assert merged[0]["end_time"] == 1400


def test_merge_english_utterances_keeps_sentence_boundary():
    utts = [
        _utt("This is done.", 0, 500),
        _utt("Next topic starts here.", 700, 1400),
    ]
    merged = asr_sentence_fixer.merge_english_utterances(utts)
    assert len(merged) == 2


def test_merge_english_utterances_absorbs_filler():
    utts = [
        _utt("Look at the density.", 0, 800),
        _utt("right?", 820, 950),
        _utt("Again,", 1000, 1200),
        _utt("you'll see examples.", 1220, 1800),
    ]
    merged = asr_sentence_fixer.merge_english_utterances(utts)
    assert [u["text"] for u in merged] == [
        "Look at the density. right?",
        "Again, you'll see examples.",
    ]


def test_merge_english_utterances_respects_gap_limit():
    utts = [
        _utt("All of this will be", 0, 500),
        _utt("completely available.", 1400, 1900),  # gap 900 > 800
    ]
    merged = asr_sentence_fixer.merge_english_utterances(utts)
    assert len(merged) == 2


def test_merge_english_utterances_allows_small_pause():
    utts = [
        _utt("All of this will be", 0, 500),
        _utt("completely available.", 1200, 1800),  # gap 700 <= 800
    ]
    merged = asr_sentence_fixer.merge_english_utterances(utts)
    assert [u["text"] for u in merged] == ["All of this will be completely available."]


def test_merge_english_utterances_absorbs_soft_period_fragments():
    utts = [
        _utt("And.", 0, 200),
        _utt("also.", 220, 400),
        _utt("we extrude this face.", 420, 1200),
        _utt("Like so.", 1220, 1500),
        _utt("Apply.", 1520, 1800),
        _utt("Next we add the bevel.", 2000, 2800),
    ]
    merged = asr_sentence_fixer.merge_english_utterances(utts)
    assert [u["text"] for u in merged] == [
        "And. also. we extrude this face. Like so. Apply.",
        "Next we add the bevel.",
    ]


def test_fix_asr_sentences_merges_english(tmp_path):
    utts = [
        _utt("All of this will be", 1000, 1400),
        _utt("completely available.", 1450, 2000),
        _utt("I'll put the link", 2100, 2500),
        _utt("in the description below.", 2520, 3100),
    ]
    asr_file, session = _write_asr(tmp_path, utts, duration=4000)

    fixed = asr_sentence_fixer.fix_asr_sentences(
        asr_file, session, language="en", start_pad=0, end_pad=0
    )
    out = json.loads(fixed.read_text(encoding="utf-8"))["result"]["utterances"]
    assert [u["text"] for u in out] == [
        "All of this will be completely available.",
        "I'll put the link in the description below.",
    ]


def test_fix_asr_sentences_skips_merge_for_chinese(tmp_path):
    utts = [
        _utt("这是一句", 0, 400),
        _utt("被切开的中文", 420, 900),
    ]
    asr_file, session = _write_asr(tmp_path, utts)

    fixed = asr_sentence_fixer.fix_asr_sentences(
        asr_file, session, language="zh", start_pad=0, end_pad=0
    )
    out = json.loads(fixed.read_text(encoding="utf-8"))["result"]["utterances"]
    assert [u["text"] for u in out] == ["这是一句", "被切开的中文"]


def test_merge_preserves_words_when_present():
    utts = [
        {
            "text": "to really be",
            "start_time": 0,
            "end_time": 300,
            "words": [{"text": "to"}, {"text": "really"}, {"text": "be"}],
        },
        {
            "text": "a artist.",
            "start_time": 320,
            "end_time": 700,
            "words": [{"text": "a"}, {"text": "artist."}],
        },
    ]
    merged = asr_sentence_fixer.merge_english_utterances(utts)
    assert len(merged) == 1
    assert len(merged[0]["words"]) == 5
