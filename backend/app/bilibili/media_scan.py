from __future__ import annotations

import re
from pathlib import Path


VIDEO_EXTS = {".mp4", ".flv", ".avi", ".wmv", ".mov", ".webm", ".mkv"}
COVER_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
SRT_EXTS = {".srt"}


def parse_srt_text(content: str) -> str:
    """把 SRT 转成纯文本字幕内容。"""
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", content.strip())
    lines: list[str] = []
    for block in blocks:
        parts = [p.strip() for p in block.split("\n") if p.strip()]
        if not parts:
            continue
        # 跳过序号与时间轴
        text_parts: list[str] = []
        for part in parts:
            if re.fullmatch(r"\d+", part):
                continue
            if re.search(r"\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}", part):
                continue
            cleaned = re.sub(r"<[^>]+>", "", part).strip()
            if cleaned:
                text_parts.append(cleaned)
        if text_parts:
            lines.append("".join(text_parts) if _looks_like_cjk("".join(text_parts)) else " ".join(text_parts))
    return "\n".join(lines).strip()


def _looks_like_cjk(text: str) -> bool:
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk >= max(3, len(text) // 4)


def read_srt(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            return parse_srt_text(raw.decode(encoding))
        except UnicodeDecodeError:
            continue
    return parse_srt_text(raw.decode("utf-8", errors="ignore"))


def _stem_candidates(video_path: Path) -> list[str]:
    """根据视频文件名生成可能的字幕/封面 stem。"""
    stem = video_path.stem
    candidates = [stem]
    # 08_01_chapter_introduction.zh.srt → video stem 是 08_01_chapter_introduction
    return candidates


def find_cover(video_path: Path, folder: Path) -> Path | None:
    stem = video_path.stem
    for ext in COVER_EXTS:
        candidate = folder / f"{stem}{ext}"
        if candidate.exists():
            return candidate
    # 宽松匹配：同名前缀
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() in COVER_EXTS and path.stem.startswith(stem):
            return path
    return None


def find_srt(video_path: Path, folder: Path) -> Path | None:
    stem = video_path.stem
    preferred = [
        folder / f"{stem}.zh.srt",
        folder / f"{stem}.zh-CN.srt",
        folder / f"{stem}.zh_cn.srt",
        folder / f"{stem}.cn.srt",
        folder / f"{stem}.srt",
        folder / f"{stem}.en.srt",
    ]
    for path in preferred:
        if path.exists():
            return path

    matches = [
        p
        for p in folder.iterdir()
        if p.suffix.lower() == ".srt" and (p.stem == stem or p.stem.startswith(stem + ".") or p.stem.startswith(stem + "_"))
    ]
    if not matches:
        return None

    def score(path: Path) -> tuple[int, str]:
        name = path.name.lower()
        if ".zh" in name:
            return (0, name)
        if name.endswith(".srt") and path.stem == stem:
            return (1, name)
        return (2, name)

    return sorted(matches, key=score)[0]


def scan_video_folder(folder: Path) -> list[dict]:
    if not folder.exists():
        return []

    items: list[dict] = []
    videos = sorted(
        [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS],
        key=lambda p: p.name.lower(),
    )
    for video in videos:
        cover = find_cover(video, folder)
        srt = find_srt(video, folder)
        items.append(
            {
                "id": video.stem,
                "name": video.name,
                "stem": video.stem,
                "video_path": str(video.resolve()),
                "cover_path": str(cover.resolve()) if cover else None,
                "srt_path": str(srt.resolve()) if srt else None,
                "has_cover": cover is not None,
                "has_srt": srt is not None,
                "size": video.stat().st_size,
                "ready": cover is not None and srt is not None,
            }
        )
    return items
