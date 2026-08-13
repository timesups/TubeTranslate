from __future__ import annotations

from backend.app.adapters import openai_translate as ot


def test_short_glossary_skips_common_crumbs():
    assert ot._short_glossary_dst("Also.", "zh") == "另外"
    assert ot._short_glossary_dst("Like so.", "zh") == "像这样"
    assert ot._short_glossary_dst("Apply.", "zh") == "应用"
    assert ot._short_glossary_dst("Also.", "en") is None


def test_overlong_detection_for_short_src():
    assert ot._is_overlong_translation(
        "Also.",
        "欢迎来到本期 Blender 教程，今天我们一起学习球形机器人。",
        "zh",
    )
    assert not ot._is_overlong_translation("Also.", "另外", "zh")
    assert not ot._is_overlong_translation(
        "We will extrude this face carefully.",
        "我们会小心地挤出这个面。",
        "zh",
    )


def test_compact_fallback_prefers_glossary():
    assert (
        ot._compact_fallback(
            "Like so.",
            "现在我们进入编辑模式并选择这些顶点来完成操作。",
            "zh",
        )
        == "像这样"
    )


def test_translate_sentence_uses_glossary_without_api(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("API should not be called for glossary crumbs")

    monkeypatch.setattr(ot, "_call_json", boom)
    assert ot.translate_sentence("And.", "zh", client=object(), model="x", system="sys") == "然后"
