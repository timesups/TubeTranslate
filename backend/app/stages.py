from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StageSpec:
    name: str
    label: str


STAGES: tuple[StageSpec, ...] = (
    StageSpec("download", "Download"),
    StageSpec("separate", "Demucs"),
    StageSpec("asr", "Whisper"),
    StageSpec("asr_fix", "Split sentences"),
    StageSpec("translate", "Translate"),
    StageSpec("split_audio", "Split audio"),
    StageSpec("tts", "VoxCPM"),
    StageSpec("merge_audio", "Merge audio"),
    StageSpec("merge_video", "Merge video"),
    StageSpec("bilibili_meta", "Bilibili metadata"),
    StageSpec("bilibili_publish", "Bilibili publish"),
)


STAGE_NAMES = tuple(stage.name for stage in STAGES)
